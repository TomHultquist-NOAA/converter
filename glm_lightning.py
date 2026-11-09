#!/usr/bin/env python3
"""Collect public GOES GLM-L2-LCFA FLASH centroids into hourly GeoJSON.

An optional, observation-only workflow; no forecast fields are processed.
Requires the existing netCDF4 Python package to read Level-2 NetCDF4.
GOES-18 covers the western sector, GOES-19 the eastern sector; the
longitude split avoids double counting in their overlapping views.
Only hours with adequate granule coverage are published; missing source files never produce fabricated empty hours.
"""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import nullcontext
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import publish_transport
import perf_monitor

UTC = dt.timezone.utc
SCHEMA = 'omd-evu-glm-flashes-1'
BBOX = (-125.0, 24.0, -66.0, 50.0)
# The split prevents overlapping GOES-18/GOES-19 flash detections from
# appearing twice, without implying cloud-to-ground strike locations.
SATELLITES = {18: (-125.0, -105.0), 19: (-105.0, -66.0)}
FILE_RE = re.compile(r'OR_GLM-L2-LCFA_G(18|19)_s(\d{13,14})_e(\d{13,14})_c\d{13,14}\.nc$')
MAX_FILE_BYTES = 16_000_000


def utc_label(t):
    return t.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def valid_hour(value):
    return dt.datetime.strptime(value, '%Y%m%d%H').replace(tzinfo=UTC)


def request_bytes(url, limit=MAX_FILE_BYTES, timeout=35):
    req = Request(url, headers={'User-Agent': 'EVU-GLM-Collector/1.0', 'Accept': '*/*'})
    started = time.monotonic()
    with urlopen(req, timeout=timeout) as response:
        length = int(response.headers.get('Content-Length') or 0)
        if length > limit:
            raise ValueError('GLM source exceeds configured size limit')
        data = response.read(limit + 1)
    perf_monitor.add_metric('glm_download_bytes', len(data))
    perf_monitor.add_metric('glm_download_seconds', time.monotonic() - started)
    if len(data) > limit:
        raise ValueError('GLM source exceeds configured size limit')
    return data


def list_hour(satellite, hour):
    prefix = f'GLM-L2-LCFA/{hour:%Y}/{hour:%j}/{hour:%H}/'
    keys = []
    token = None
    while True:
        query = {'list-type': '2', 'prefix': prefix, 'max-keys': '1000'}
        if token:
            query['continuation-token'] = token
        xml = ET.fromstring(request_bytes(
            f'https://noaa-goes{satellite}.s3.amazonaws.com/?{urlencode(query)}',
            limit=5_000_000, timeout=40))
        def child(node, name):
            return next((elem for elem in node if elem.tag.rsplit('}', 1)[-1] == name), None)
        for contents in xml:
            if contents.tag.rsplit('}', 1)[-1] != 'Contents':
                continue
            item = child(contents, 'Key')
            if item is not None and item.text and FILE_RE.search(item.text):
                keys.append(item.text)
        flag = child(xml, 'IsTruncated')
        if flag is None or flag.text != 'true':
            break
        next_token = child(xml, 'NextContinuationToken')
        if next_token is None or not next_token.text:
            raise ValueError('Truncated GLM source listing lacks continuation token')
        token = next_token.text
    return keys


def decoded(values, attrs):
    """Decode numeric NetCDF/HDF5 packed arrays using CF scale and offset."""
    scale = float(attrs.get('scale_factor', 1))
    offset = float(attrs.get('add_offset', 0))
    fill = attrs.get('_FillValue')
    missing = attrs.get('missing_value')
    def scalar(v):
        # numpy scalars have .item(), without needing explicit numpy import.
        return v.item() if hasattr(v, 'item') else v
    fill = scalar(fill) if fill is not None else None
    missing = scalar(missing) if missing is not None else None
    result = []
    for raw in values:
        v = scalar(raw)
        if v is None or v == fill or v == missing:
            result.append(None)
        else:
            result.append(float(v) * scale + offset)
    return result


def time_units_origin(value):
    if isinstance(value, bytes):
        value = value.decode('ascii')
    if not isinstance(value, str):
        raise ValueError('GLM flash time units missing')
    match = re.match(r'^seconds since (\d{4}-\d\d-\d\d)[ T](\d\d:\d\d:\d\d)(?:\.\d+)?(?:Z)?$', value)
    if not match:
        raise ValueError('Unrecognized GLM flash time units')
    return dt.datetime.fromisoformat(match.group(1) + 'T' + match.group(2)).replace(tzinfo=UTC)


def extract_flashes(data, satellite, start, end):
    """Parse one 20-second Level-2 GLM payload using the existing netCDF4 package."""
    def process(read):
        lat, la = read('flash_lat')
        lon, lo = read('flash_lon')
        times, ta = read('flash_time_offset_of_first_event')
        if len(lat) != len(lon) or len(lat) != len(times):
            raise ValueError('GLM flash arrays have unequal lengths')
        zero = time_units_origin(ta.get('units'))
        result = []
        xmin, xmax = SATELLITES[satellite]
        for latitude, longitude, sec in zip(decoded(lat, la), decoded(lon, lo), decoded(times, ta)):
            if any(v is None or not math.isfinite(v) for v in (latitude, longitude, sec)):
                continue
            if not (BBOX[1] <= latitude <= BBOX[3] and xmin <= longitude < xmax):
                continue
            observed = zero + dt.timedelta(seconds=sec)
            if not (start < observed <= end):
                continue
            result.append({'type': 'Feature',
                           'geometry': {'type': 'Point', 'coordinates': [round(longitude, 4), round(latitude, 4)]},
                           'properties': {'time': utc_label(observed), 'satellite': 'GOES-' + str(satellite)}})
        return result
    try:
        from netCDF4 import Dataset
    except ImportError as exc:
        raise RuntimeError('GLM collection requires netCDF4 in the existing Python environment') from exc
    def parse_dataset(file):
        def read(name):
            var = file.variables[name]
            var.set_auto_maskandscale(False)
            return var[:], {key: var.getncattr(key) for key in var.ncattrs()}
        return process(read)

    # Prefer the netCDF4 in-memory reader to avoid writing hundreds of small
    # granules to disk. Older netcdf-c builds may lack read-memory support, so
    # fall back to a short-lived local file without introducing an h5py need.
    try:
        dataset = Dataset('glm.nc', mode='r', memory=data)
    except (TypeError, ValueError, RuntimeError, OSError):
        fd, filename = tempfile.mkstemp(prefix='glm_', suffix='.nc')
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
            with Dataset(filename, mode='r') as file:
                return parse_dataset(file)
        finally:
            try:
                os.unlink(filename)
            except OSError:
                pass
    with dataset:
        return parse_dataset(dataset)


def goes_timestamp(value):
    """Parse the YYYYDDDHHMMSS portion of a GOES filename timestamp."""
    if not isinstance(value, str) or len(value) not in (13, 14) or not value.isdigit():
        raise ValueError('Invalid GOES filename timestamp')
    # Operational GLM filenames use YYYYDDDHHMMSS with an optional tenth
    # second digit. Preserve that tenth to avoid dropping a boundary granule
    # whose end is, for example, 17:00:00.4.
    stamp = dt.datetime.strptime(value[:13], '%Y%j%H%M%S').replace(tzinfo=UTC)
    if len(value) == 14:
        stamp += dt.timedelta(microseconds=int(value[13]) * 100000)
    return stamp


def granule_overlaps(key, start, end):
    match = FILE_RE.search(key)
    if not match:
        return False
    try:
        granule_start = goes_timestamp(match.group(2))
        granule_end = goes_timestamp(match.group(3))
    except ValueError:
        return False
    return granule_start < end and granule_end > start


def download_one(key, satellite):
    """Download one granule only; never call NetCDF-C from a worker thread."""
    perf_monitor.add_metric('glm_granule_download_count', 1)
    return request_bytes(f'https://noaa-goes{satellite}.s3.amazonaws.com/{key}')


@perf_monitor.timed_task('glm_hour', lambda a, k: a[0].strftime('%Y%m%d%H'))
def collect_hour(hour, *, workers=4, min_files=150):
    end = hour
    start = end - dt.timedelta(hours=1)
    if end + dt.timedelta(minutes=10) > dt.datetime.now(UTC):
        raise ValueError('GLM hour not yet complete (10-minute collection delay)')
    all_features = []
    coverage = {}
    # Include two hourly prefixes for a source granule that starts just
    # before the beginning of the hour but contains flashes within it.
    for satellite in SATELLITES:
        keys = set(list_hour(satellite, start.replace(minute=0, second=0)) +
                   list_hour(satellite, (start - dt.timedelta(minutes=1)).replace(minute=0, second=0)))
        # Keep only granules overlapping the requested 60-minute interval. The
        # prior-hour prefix contributes at most its boundary granule(s), not an
        # entire extra hour of downloads.
        wanted = sorted(key for key in keys if granule_overlaps(key, start, end))
        # Minimum file count ensures a missing/late hour is never mistaken for
        # a genuine zero-lightning hour. Most complete hours have 180 files.
        current = [key for key in wanted if f'/{start:%Y}/{start:%j}/{start:%H}/' in key]
        coverage[str(satellite)] = len(current)
        if len(current) < min_files:
            raise ValueError(f'GOES-{satellite} has only {len(current)} GLM granules; retry later')
        # netCDF-C (and therefore netCDF4-python) is not thread-safe. Keep
        # network transfers concurrent, but perform every Dataset open/read/
        # close serially in this collector thread. A previous implementation
        # parsed each payload inside download_one(), which meant the four
        # download threads could enter netCDF-C simultaneously and abort in
        # nc4_nc4f_list_add. Bound submitted futures so downloaded payloads
        # waiting for the serial parser cannot grow without limit.
        worker_count = max(1, workers)  # No hard cap; downloads only, NetCDF parsing remains serial.
        key_iter = iter(wanted)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            pending = {}
            for _ in range(worker_count):
                try:
                    key = next(key_iter)
                except StopIteration:
                    break
                pending[executor.submit(download_one, key, satellite)] = key

            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    key = pending.pop(future)
                    payload = future.result()
                    parse_started = time.monotonic()
                    all_features.extend(extract_flashes(payload, satellite, start, end))
                    perf_monitor.add_metric('glm_netcdf_parse_seconds',
                                            time.monotonic() - parse_started)
                    del payload
                    try:
                        next_key = next(key_iter)
                    except StopIteration:
                        continue
                    pending[executor.submit(download_one, next_key, satellite)] = next_key
    # Sort to generate predictable JSON and facilitate timestamp validation.
    all_features.sort(key=lambda f: (f['properties']['time'], f['properties']['satellite'], *f['geometry']['coordinates']))
    return {'type': 'FeatureCollection', 'schema': SCHEMA,
            'valid_time': utc_label(end), 'window_start': utc_label(start),
            'window_end': utc_label(end), 'source': 'GOES GLM-L2-LCFA flash centroids',
            'source_file_counts': coverage, 'features': all_features}


def save_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.incoming', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, separators=(',', ':'))
            stream.write('\n')
        # mkstemp() creates mode 0600 regardless of umask. These GeoJSON
        # products are intended for public web reading, so fix permissions
        # before atomically installing the completed file.
        os.chmod(filename, 0o644)
        os.replace(filename, path)
    finally:
        if os.path.exists(filename):
            os.unlink(filename)


def publish(path, host, destination, remote_ready=False):
    if not re.fullmatch(r'[\w.@-]+', host) or not re.fullmatch(r'/[\w./-]+', destination):
        raise ValueError('Unsafe GLM publishing destination')
    target = destination.rstrip('/') + '/lightning'
    remote_tmp = '.' + path.name + '.incoming'
    if not remote_ready:
        # A restrictive remote umask can create this directory as 0700.
        publish_transport.run(['ssh', host, 'mkdir -p ' + target + ' && chmod 755 ' + target],
                              check=True, timeout=90)
    publish_transport.run(['scp', '-q', str(path), host + ':' + target + '/' + remote_tmp], check=True, timeout=90)
    # Explicitly correct the remote staging file before its atomic rename.
    # This also repairs cached 0600 files on subsequent hourly republication.
    publish_transport.run(['ssh', host, 'chmod 644 ' + target + '/' + remote_tmp +
                           ' && mv ' + target + '/' + remote_tmp + ' ' + target + '/' + path.name],
                          check=True, timeout=90)
    return True


@perf_monitor.monitored('glm_lightning')
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root', type=Path, default=Path('zarr'))
    parser.add_argument('--date', help='UTC day YYYYMMDD (requires --hour)')
    parser.add_argument('--hour', type=int, default=None, help='UTC valid hour')
    parser.add_argument('--start', help='First UTC valid hour YYYYMMDDHH')
    parser.add_argument('--end', help='Last UTC valid hour YYYYMMDDHH inclusive')
    parser.add_argument('--lookback-hours', type=int, default=2)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--min-files', type=int, default=170)
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--force', action='store_true', help='Redownload already saved complete UTC hours')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--publish-host', default='thultquist@rzdm.ncep.noaa.gov')
    parser.add_argument('--publish-dir', default='/home/people/emc/www/htdocs/users/verification_restricted/test/trh/dynamic_viewer/data')
    args = parser.parse_args(argv)
    perf_monitor.record_event('run_config', workflow='glm_lightning',
        workers=args.workers, lookback_hours=args.lookback_hours,
        min_files=args.min_files, publish=bool(args.publish), force=bool(args.force))
    if args.workers < 1:
        parser.error('--workers must be positive')
    if not 100 <= args.min_files <= 180:
        parser.error('--min-files must be 100–180')
    if bool(args.start) != bool(args.end):
        parser.error('--start and --end must be supplied together')
    if args.start:
        try:
            first, last = valid_hour(args.start), valid_hour(args.end)
        except ValueError:
            parser.error('Invalid --start/--end (expected YYYYMMDDHH)')
        if last < first or (last-first).total_seconds() > 7*86400:
            parser.error('Backfill range must be 1–169 consecutive UTC hours')
        hours = [first + dt.timedelta(hours=i) for i in range(int((last-first).total_seconds()//3600)+1)]
    elif args.date is not None:
        if args.hour is None or not 0 <= args.hour <= 23:
            parser.error('--date requires --hour 0–23')
        try:
            first = dt.datetime.strptime(args.date, '%Y%m%d').replace(tzinfo=UTC, hour=args.hour)
        except ValueError:
            parser.error('--date must be YYYYMMDD')
        hours = [first]
    else:
        if not 1 <= args.lookback_hours <= 24:
            parser.error('--lookback-hours must be 1–24')
        latest = (dt.datetime.now(UTC) - dt.timedelta(minutes=10)).replace(minute=0,second=0,microsecond=0)
        hours = [latest-dt.timedelta(hours=n) for n in range(args.lookback_hours)]
    errors = 0
    ready = False
    with publish_transport.session() if args.publish and not args.dry_run else nullcontext():
        for hour in hours:
            path = args.output_root / 'lightning' / f'{hour:%Y%m%d%H}.json'
            if args.dry_run:
                print('GLM lightning valid hour', hour.strftime('%Y%m%d%H'), '->', path)
                continue
            try:
                cached = False
                if path.is_file() and not args.force:
                    try:
                        with path.open('r', encoding='utf-8') as stream:
                            candidate = json.load(stream)
                        if (candidate.get('schema') == SCHEMA and
                                candidate.get('valid_time') == utc_label(hour) and
                                candidate.get('type') == 'FeatureCollection' and
                                isinstance(candidate.get('features'), list)):
                            value = candidate
                            cached = True
                    except (OSError, ValueError, TypeError, AttributeError):
                        pass
                if not cached:
                    value = collect_hour(hour, workers=args.workers, min_files=args.min_files)
                    save_atomic(path, value)
                print('GLM lightning', hour.strftime('%Y%m%d%H'), 'flashes', len(value['features']),
                      'granules', value['source_file_counts'], '(cached)' if cached else '(collected)', flush=True)
                if args.publish:
                    ready = publish(path, args.publish_host, args.publish_dir, remote_ready=ready)
            except (OSError, ValueError, KeyError, RuntimeError, HTTPError, URLError,
                    subprocess.CalledProcessError, ET.ParseError) as exc:
                print('GLM lightning', hour.strftime('%Y%m%d%H'), 'incomplete or failed:', str(exc)[:200],
                      file=sys.stderr)
                errors += 1
    return int(errors > 0)


if __name__ == '__main__':
    sys.exit(main())
