#!/usr/bin/env python3
"""GFS v16 (NODD indexed GRIB) and experimental v17 (local GRIB2) -> native 0.25° Zarr.

This pipeline intentionally does not use the 3-km Lambert conversion path, and
has no observations/analysis ingest. Requires Python stdlib and wgrib2 only.
"""
from __future__ import annotations

import argparse
from array import array
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import zlib

# Limit wgrib2 thread contention before importing the shared converter.
for _k in ('OMP_NUM_THREADS', 'OMP_THREAD_LIMIT', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
    os.environ[_k] = '1'

import perf_monitor
import multi_model_to_zarr_wcoss2 as shared
import gfs_profiles as profiles
import sounding_profiles as sounding

core = shared.core
MODELS = ('gfs_v16', 'gfs_v17')
DIFFERENCE_MODELS = ('gfs_diff_v17_v16', 'gfs_diff_v16_analysis', 'gfs_diff_v17_analysis')
CATALOG_MODELS = MODELS + DIFFERENCE_MODELS
HOURS = tuple(range(0, 241, 3))
# Lon is deliberately 0..360 to make one contiguous subgrid across the dateline.
WEST, EAST, SOUTH, NORTH, STEP = 160.0, 310.0, 5.0, 75.0, 0.25
NX = int(round((EAST - WEST) / STEP)) + 1
NY = int(round((NORTH - SOUTH) / STEP)) + 1
NPTS = NX * NY
FIELD_BYTES = NPTS * 4
V16_BASE = 'https://noaa-gfs-bdp-pds.s3.amazonaws.com'
V17_BASE = '/lfs/h2/emc/gfstemp/emc.global/comroot/retrov17_01_realtime'
STORES = re.compile(r'^(gfs_v16|gfs_v17)_(\d{8})_(00|06|12|18)00\.zarr$')
CHUNKS = re.compile(r'^(\d+)\.0\.0$')
# These products are not valid as 3-hour-resolution GFS fields.
EXCLUDE = frozenset(('mxuphl', 'mxuphl_run', 'shear01', 'shear06'))
DERIVED = frozenset(('wspd10', 'wspd300', 'wspd250', 'ptype'))
# Only APCP and true interval snowfall fields are converted to run totals.
# WEASD/SNOD are state fields and must NOT be accumulated in time.
ACCUM = frozenset(('apcp', 'frozr', 'frzr', 'tsnowp', 'asnow'))
FIELDS = {k: v for k, v in shared.FIELDS.items()
          if k not in EXCLUDE and k not in ('shru01', 'shrv01', 'shru06', 'shrv06')}
# GFS 0.25° mostly has these in its main pgrb2/pres_a product. All are optional.
# SCP and STP are omitted from shared.FIELDS, for every model group.
# GFS bulk shear and missing SRH are derived from native isobaric winds/heights.
DERIVED_KINEMATICS = ('shear01','shear06','shru01','shrv01','shru06','shrv06','srh01','srh03')
for _name in DERIVED_KINEMATICS:
    FIELDS.setdefault(_name, shared.FIELDS[_name])

# AI/GEFS comparison shares this *existing* GFSv16 catalog and F000 analysis.
# Add only the additional native pressure levels; old Zarr fields and plots
# remain unchanged, with missing GRIB records explicitly absent.
for _pressure in (250,):
    for _prefix, _short, _units in (('h', 'HGT', 'gpm'), ('u', 'UGRD', 'm s-1'), ('v', 'VGRD', 'm s-1')):
        FIELDS[f'{_prefix}{_pressure}'] = dict(long_name=f'{_pressure}-hPa {_prefix}',
            short=_short,level=f'{_pressure} mb',units=_units,product='pres_a')
FIELDS['t1000'] = dict(long_name='1000-hPa temperature', short='TMP',
    level='1000 mb',units='K',product='pres_a')
FIELDS['h1000'] = dict(long_name='1000-hPa geopotential height',short='HGT',
    level='1000 mb',units='gpm',product='pres_a')
for _component, _short in (('u1000', 'UGRD'), ('v1000', 'VGRD')):
    FIELDS[_component] = dict(long_name='1000-hPa '+_component[0].upper()+' wind',
        short=_short, level='1000 mb', units='m s-1', product='pres_a')
FIELDS['wspd250'] = dict(long_name='250-hPa wind speed',short='DERIVED',
    level='250 mb',units='m s-1',product='derived')


def v16_url(date: str, cycle: str, hour: int, base: str = V16_BASE) -> str:
    return f'{base.rstrip("/")}/gfs.{date}/{cycle}/atmos/gfs.t{cycle}z.pgrb2.0p25.f{hour:03d}'


def v17_path(date: str, cycle: str, hour: int, base: str = V17_BASE) -> Path:
    return Path(base) / f'gfs.{date}' / cycle / 'products/atmos/grib2/0p25' / f'gfs.t{cycle}z.pres_a.0p25.f{hour:03d}.grib2'


def supplemental_profile_source(model, primary_source):
    """Optional companion GRIB2 holding additional native pressure levels."""
    if model == 'gfs_v16':
        return primary_source.replace('.pgrb2.0p25.', '.pgrb2b.0p25.')
    # Experimental layouts differ; use pres_b only if locally present.
    return Path(str(primary_source).replace('.pres_a.0p25.', '.pres_b.0p25.'))


def additional_profile_records(primary, supplemental):
    """Select only absent native (variable, pressure) pairs; never replace primary."""
    missing = {k: row for k, row in profiles.select(supplemental).items() if k not in primary}
    # Ground-height is available in the primary file and must never be overridden.
    return missing


def native_profile_coverage(primary, supplemental=()):
    """Summarize native sounding records from both products, including RH moisture.

    Report a level as dewpoint-capable only when a temperature record and
    SPFH, DPT, or RH are available at that *same* native pressure. This is an
    inventory audit, not a promise that the underlying samples are nonmissing.
    """
    merged = dict(primary)
    merged.update(additional_profile_records(primary, supplemental))
    levels = {
        component: [p for p in profiles.PRESSURES if (component, p) in merged]
        for component in ('t', 'q', 'u', 'v', 'z', 'rh', 'td_native')
    }
    levels['td_capable'] = [p for p in profiles.PRESSURES
        if ('t', p) in merged and any((moisture, p) in merged
                                     for moisture in ('q', 'td_native', 'rh'))]
    return levels


def parse_idx(text: str, total_size: int):
    """S3 .idx and wgrib2 -s both start with record-number:byte-offset."""
    return core.parse_idx(text, total_size)


def choose_records(rows, hour):
    """Match native shortName AND exact level; never substitute neighboring levels."""
    selected = {}
    for name in FIELDS:
        if name in DERIVED or name in ('shear01','shear06','shru01','shrv01','shru06','shrv06') or (hour == 0 and name in ACCUM):
            continue
        rec = shared.choose_field(rows, name, hour, fields=FIELDS)
        if rec is None:
            continue
        if name in ACCUM:
            interval = shared.parse_acc_interval(rec['text'], hour)
            if not isinstance(interval, tuple) or interval[1] != hour or not 0 <= interval[0] < hour or interval[1] - interval[0] < 3:
                # Some records are averages/unknown intervals, not period totals.
                continue
        else:
            interval = None
        selected[name] = (rec, interval)
    return selected


def native_grid_attrs():
    return {
        'GRIB_gridType': 'regular_ll', 'GRIB_Nx': NX, 'GRIB_Ny': NY,
        'GRIB_DxInDegrees': STEP, 'GRIB_DyInDegrees': STEP,
        'GRIB_longitudeOfFirstGridPointInDegrees': WEST,
        'GRIB_latitudeOfFirstGridPointInDegrees': SOUTH,
        'GRIB_longitudeOfLastGridPointInDegrees': EAST,
        'GRIB_latitudeOfLastGridPointInDegrees': NORTH,
    }


def validate_grid_description(grid: str):
    """Fail closed if -small_grib did not crop or if the native spacing changed."""
    shape = re.search(r'\(\s*(\d+)\s*x\s*(\d+)\s*\)', grid)
    if shape is None or (int(shape.group(1)), int(shape.group(2))) != (NX, NY):
        raise ValueError(f'Unexpected cropped GFS grid, expected {NX}x{NY}: {grid[:600]}')
    lat = re.search(r'lat\s+(-?\d+(?:\.\d+)?)\s+to\s+(-?\d+(?:\.\d+)?)\s+by\s+(-?\d+(?:\.\d+)?)', grid, re.I)
    lon = re.search(r'lon\s+(-?\d+(?:\.\d+)?)\s+to\s+(-?\d+(?:\.\d+)?)\s+by\s+(-?\d+(?:\.\d+)?)', grid, re.I)
    if lat is None or lon is None:
        raise ValueError('Missing latitude/longitude bounds in cropped GFS grid: ' + grid[:600])
    lat_start, lat_end, dy = map(float, lat.groups())
    lon_start, lon_end, dx = map(float, lon.groups())
    # GRIB's first/last point may follow north-to-south scan order; the IEEE
    # output is explicitly converted to WE:SN below. Also accept -50° = 310°E.
    if not (abs(min(lat_start,lat_end)-SOUTH)<.005 and
            abs(max(lat_start,lat_end)-NORTH)<.005 and abs(abs(dy)-STEP)<.005 and
            abs((lon_start % 360)-WEST)<.005 and
            abs((lon_end % 360)-EAST)<.005 and abs(abs(dx)-STEP)<.005):
        raise ValueError('Cropped GFS domain does not match the configured un-interpolated grid')


@perf_monitor.timed_task('gfs_local_acquisition', include_metrics=False)
def local_selected_grib(source: Path, rows, selection, target: Path):
    """Copy complete message ranges from a local v17 GRIB2, no external GRIB tools."""
    targets = sorted((v[0] for v in selection.values()), key=lambda row: row['msg'])
    with source.open('rb') as stream, target.open('wb') as dest:
        for rec in targets:
            n = rec['end'] - rec['offset'] + 1
            if n <= 0:
                raise ValueError('Invalid local GRIB2 byte range')
            stream.seek(rec['offset'])
            data = stream.read(n)
            if len(data) != n:
                raise IOError('Short local GRIB2 record read')
            dest.write(data)
    return targets


@perf_monitor.timed_task('gfs_range_acquisition', include_metrics=False)
def fetch_v16_grib(client, url, rows, selection, target: Path):
    reqs = [{'record': rec} for rec, _ in selection.values()]
    reqs.sort(key=lambda x: x['record']['msg'])
    core.download_partial(client, url, reqs, target)
    return [r['record'] for r in reqs]


@perf_monitor.timed_task('gfs_grib_decode', include_metrics=False)
def cropped_ieee(wgrib2, selected: Path, cropped: Path, ieee: Path, count: int):
    # -small_grib preserves the input grid points and native spacing. -new_grid
    # must NOT be used: it may interpolate the data and would inflate output.
    subprocess.run([wgrib2, str(selected), '-small_grib', f'{WEST:g}:{EAST:g}',
                    f'{SOUTH:g}:{NORTH:g}', str(cropped)], check=True,
                   stdout=subprocess.DEVNULL)
    if not cropped.is_file() or cropped.stat().st_size < 16:
        raise RuntimeError('wgrib2 did not produce a cropped GRIB2 file')
    proc = subprocess.run([wgrib2, str(cropped), '-d', '1', '-grid'], check=True,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    validate_grid_description(proc.stdout)
    core.decode_to_ieee(wgrib2, cropped, ieee)
    if ieee.stat().st_size != count * FIELD_BYTES:
        raise RuntimeError(f'Unexpected IEEE output bytes ({ieee.stat().st_size} != {count*FIELD_BYTES}); crop mismatch')


def arrfloat(raw):
    out = array('f')
    out.frombytes(raw)
    if sys.byteorder != 'little':
        out.byteswap()
    if len(out) != NPTS:
        raise ValueError('Invalid regional GFS float array')
    return out


def packfloat(data):
    if sys.byteorder != 'little':
        data.byteswap()
    return data.tobytes()


def combine_accums(prior: bytes, current: bytes):
    a, b = arrfloat(prior), arrfloat(current)
    return packfloat(array('f', (x + y if math.isfinite(x) and math.isfinite(y) else math.nan
                                for x, y in zip(a, b))))


def derive_wind_speed(u, v):
    return shared.decode_wspd(u, v)


def derive_ptype(raws):
    a = {k: arrfloat(v) for k, v in raws.items()}
    out = bytearray(NPTS)
    for i in range(NPTS):
        flags = (a['crain'][i] > .5, a['csnow'][i] > .5,
                 a['cfrzr'][i] > .5, a['cicep'][i] > .5)
        n = sum(flags)
        out[i] = 5 if n > 1 else next((j + 1 for j, hit in enumerate(flags) if hit), 0)
    return bytes(out)


def create_store(store: Path, model: str, date: str, cycle: str, compression: int):
    store.mkdir(parents=True, exist_ok=True)
    init = shared.iso_init(date, cycle)
    attrs = {
        'model': 'GFS v16' if model == 'gfs_v16' else 'GFS v17',
        'model_id': model, 'domain': 'North America / Alaska / Hawaii / Puerto Rico',
        'grid_spacing_degrees': STEP, 'grid_order': 'WE:SN',
        'native_grid': True, 'regridded': False,
        'init_time': init.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'source': 'NODD 0.25-degree GFS GRIB2' if model == 'gfs_v16' else 'Locally available GFS v17 pres_a 0.25-degree GRIB2',
        'viewer_hint': 'regular latlon; 0..360 longitude; WE:SN',
        'dimensions': {'valid_time': 241, 'y': NY, 'x': NX},
        **native_grid_attrs(),
    }
    if (store / '.zattrs').is_file():
        previous = json.loads((store / '.zattrs').read_text())
        if previous.get('model_id') != model or previous.get('init_time') != attrs['init_time'] or any(
                previous.get(k) != v for k, v in native_grid_attrs().items()):
            raise ValueError('Existing GFS store metadata incompatible with selected domain; refusing overwrite')
    else:
        core.json_write(store / '.zgroup', {'zarr_format': 2})
        core.json_write(store / '.zattrs', attrs)
    core.ensure_array(store, 'processed', (241,), (241,), '|u1', 0,
                      {'_ARRAY_DIMENSIONS': ['valid_time']}, compression)
    coordinates = [
        ('valid_time', '<i8', core.pack_int64_le(int((init + dt.timedelta(hours=h)).timestamp()) for h in range(241)), 'seconds since 1970-01-01'),
        ('forecast_hour', '<i2', core.pack_int16_le(range(241)), 'hours'),
        ('latitude', '<f4', packfloat(array('f', (SOUTH + STEP * i for i in range(NY)))), 'degrees_north'),
        ('longitude', '<f4', packfloat(array('f', (WEST + STEP * i for i in range(NX)))), 'degrees_east'),
    ]
    for name, dtype, data, units in coordinates:
        length = 241 if name in ('valid_time', 'forecast_hour') else NY if name == 'latitude' else NX
        dimension = 'valid_time' if length == 241 and name in ('valid_time','forecast_hour') else 'y' if name == 'latitude' else 'x'
        core.ensure_array(store, name, (length,), (length,), dtype, None,
                          {'_ARRAY_DIMENSIONS': [dimension], 'units': units}, compression)
        if not (store / name / '0').is_file():
            core.write_compressed_chunk(store / name / '0', data, compression)
    if not (store / 'processed' / '0').exists():
        core.write_compressed_chunk(store / 'processed' / '0', bytes(241), compression)


def ensure_field(store, name, compression):
    spec = FIELDS.get(name, {})
    dtype = '|u1' if name == 'ptype' else '<f4'
    attrs = {'_ARRAY_DIMENSIONS': ['valid_time', 'y', 'x'],
             'long_name': spec.get('long_name', 'Precipitation type' if name == 'ptype' else name),
             'units': spec.get('units', '1'),
             'GRIB_shortName': spec.get('short', 'DERIVED'),
             'GRIB_level': spec.get('level', 'derived')}
    if name in DERIVED_KINEMATICS:
        attrs['provenance'] = ('Native GRIB2 when available; derived from native vertical profiles otherwise'
                               if name.startswith('srh') else 'Derived from native isobaric winds and AGL heights')
    core.ensure_array(store, name, (241, NY, NX), (1, NY, NX), dtype,
                      0 if name == 'ptype' else 'NaN', attrs, compression)


def read_status(store):
    return shared.read_processed(store, 241)


def manifest_path(work_root, store):
    return work_root / 'gfs_manifests' / (store.name + '.json')


def load_manifest(work_root, store):
    path = manifest_path(work_root, store)
    if path.is_file():
        return json.loads(path.read_text())
    return {'schema': 1, 'store': store.name, 'hours': {}}


def write_manifest(work_root, store, manifest):
    core.json_write(manifest_path(work_root, store), manifest)


@perf_monitor.timed_task('gfs_forecast_hour', lambda a,k: '%s %s%s F%03d' % (a[0], a[1], a[2], a[3]))
def process_hour(model, date, cycle, hour, v17_root, v16_root, wgrib2, timeout, retries, profile_enabled=True, profile_only=False):
    """Worker phase: I/O and decoding only. Coordinator commits chunks atomically."""
    client = core.HTTPClient(timeout=timeout, retries=retries)
    with tempfile.TemporaryDirectory(prefix=f'gfs_{model}_{date}_{cycle}_{hour:03d}_') as temp:
        work = Path(temp)
        selected, cropped, ieee = (work / s for s in ('selected.grib2', 'cropped.grib2', 'regional.ieee'))
        if model == 'gfs_v16':
            source = v16_url(date, cycle, hour, v16_root)
            try:
                text = client.text(source + '.idx')
                size = client.size(source)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
                return {'hour': hour, 'status': 'unavailable', 'reason': str(exc)}
            rows = parse_idx(text, size)
        else:
            source = v17_path(date, cycle, hour, v17_root)
            if not source.is_file() or source.stat().st_size == 0:
                return {'hour': hour, 'status': 'unavailable', 'reason': 'v17 local GRIB2 not yet present'}
            result = subprocess.run([wgrib2, str(source), '-s'], check=True, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            rows = parse_idx(result.stdout, source.stat().st_size)
        requested = choose_records(rows, hour)
        if profile_only:
            # Existing map hour is intact. Reuse only near-surface/map inputs
            # needed to complete the missing sounding and shear diagnostics.
            requested = {k:v for k,v in requested.items() if k in
                         ('psfc','t2m','dpt2m','u10','v10','ustm','vstm','srh01','srh03')}
        profile_requests = profiles.select(rows) if profile_enabled else {}
        if not requested and not profile_requests:
            return {'hour': hour, 'status': 'unavailable', 'reason': 'no supported GFS fields in inventory'}
        # A single native message can satisfy a map and a profile field.
        # Fetch it just once and dispatch the one decoded array to both.
        by_message = {}
        for name,(rec,interval) in requested.items():
            by_message.setdefault(rec['msg'],{'row':rec,'map':[],'profile':[]})['map'].append((name,interval))
        for key,rec in profile_requests.items():
            by_message.setdefault(rec['msg'],{'row':rec,'map':[],'profile':[]})['profile'].append(key)
        unique = {str(msg):(item['row'],None) for msg,item in by_message.items()}
        ordered = (fetch_v16_grib(client, source, rows, unique, selected) if model == 'gfs_v16'
                   else local_selected_grib(source, rows, unique, selected))
        cropped_ieee(wgrib2, selected, cropped, ieee, len(ordered))
        # The message order in the regional binary is identical to ordered.
        raw = {}
        profile_raw = {}
        with ieee.open('rb') as inp:
            for row in ordered:
                data = inp.read(FIELD_BYTES)
                if len(data) != FIELD_BYTES:
                    raise IOError('Unexpected EOF from cropped GFS IEEE data')
                item = by_message[row['msg']]
                for name,interval in item['map']:
                    raw[name] = (data,interval)
                for key in item['profile']:
                    profile_raw[key] = data
        # The primary pgrb2/pres_a inventory is not a complete pressure-level
        # sounding. Fetch optional companion native records *only* for levels
        # missing from the primary inventory. Map fields remain primary-only.
        supplement_count = 0
        if profile_enabled:
            extra_source = supplemental_profile_source(model, source)
            try:
                if model == 'gfs_v16':
                    extra_text = client.text(extra_source + '.idx')
                    extra_size = client.size(extra_source)
                    extra_rows = parse_idx(extra_text, extra_size)
                elif extra_source.is_file() and extra_source.stat().st_size:
                    inv = subprocess.run([wgrib2, str(extra_source), '-s'], check=True,
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    extra_rows = parse_idx(inv.stdout, extra_source.stat().st_size)
                else:
                    extra_rows = []
                missing = additional_profile_records(profile_requests, extra_rows)
                if missing:
                    extra_selection = {str(i): (rec, None) for i, rec in enumerate(missing.values())}
                    extra_selected, extra_cropped, extra_ieee = (
                        work / f'supplement_{suffix}' for suffix in ('selected.grib2', 'cropped.grib2', 'regional.ieee'))
                    extra_ordered = (fetch_v16_grib(client, extra_source, extra_rows, extra_selection, extra_selected)
                        if model == 'gfs_v16' else local_selected_grib(extra_source, extra_rows, extra_selection, extra_selected))
                    cropped_ieee(wgrib2, extra_selected, extra_cropped, extra_ieee, len(extra_ordered))
                    keys_by_msg = {rec['msg']: key for key, rec in missing.items()}
                    with extra_ieee.open('rb') as inp:
                        for row in extra_ordered:
                            data = inp.read(FIELD_BYTES)
                            if len(data) != FIELD_BYTES:
                                raise IOError('Unexpected EOF in supplemental GFS pressure profile')
                            profile_raw[keys_by_msg[row['msg']]] = data
                    supplement_count = len(extra_ordered)
                coverage = native_profile_coverage(profile_requests, extra_rows)
                logging.info('%s F%03d native profile coverage after companion: '
                             'T=%d Td-capable=%d U=%d V=%d HGT=%d of %d levels; '
                             'supplemental RH=%d',
                             model, hour, len(coverage['t']), len(coverage['td_capable']),
                             len(coverage['u']), len(coverage['v']), len(coverage['z']),
                             len(profiles.PRESSURES), len(coverage['rh']))
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError,
                    subprocess.CalledProcessError) as exc:
                logging.warning('%s F%03d supplemental pressure levels unavailable: %s', model, hour, exc)
        return {'hour': hour, 'status': 'complete', 'fields': raw,
                'profile_fields':profile_raw, 'profile_only':profile_only,
                'source': str(source), 'selected': len(ordered), 'supplemental_profile_records': supplement_count}


@perf_monitor.timed_task('gfs_zarr_commit', lambda a,k: '%s F%03d' % (a[0].name, a[2]['hour']))
def commit_hour(store, work, result, compression):
    hour = result['hour']
    raw = result['fields']
    committed = []
    pending = {}
    # Raw intervals are persisted so that later backfill can reconstruct totals.
    for name, (data, interval) in raw.items():
        if name in ACCUM:
            start, end = interval
            # Keep the actual interval separately; never label interval-only GRIB as run total.
            interval_path = store / '.accum_intervals' / name / f'{hour}.zlib'
            core.write_compressed_chunk(interval_path, data, compression)
            pending[name] = [start, end]
            if start != 0:
                previous = store / name / f'{start}.0.0'
                if not previous.is_file():
                    continue
                data = combine_accums(zlib.decompress(previous.read_bytes()), data)
        ensure_field(store, name, compression)
        core.write_compressed_chunk(store / name / f'{hour}.0.0', data, compression)
        committed.append(name)
    from upper_air_moisture import derive_dewpoint_bytes
    for pressure in (700,850,925):
        name=f'td{pressure}'
        if name not in raw and f'q{pressure}' in raw and f't{pressure}' in raw:
            ensure_field(store,name,compression)
            core.write_compressed_chunk(store/name/f'{hour}.0.0',
                derive_dewpoint_bytes(raw[f'q{pressure}'][0],raw[f't{pressure}'][0],pressure),compression)
            committed.append(name)
    for output, u, v in (('wspd10', 'u10', 'v10'), ('wspd300', 'u300', 'v300'), ('wspd250', 'u250', 'v250')):
        if u in raw and v in raw:
            ensure_field(store, output, compression)
            core.write_compressed_chunk(store / output / f'{hour}.0.0',
                                        derive_wind_speed(raw[u][0], raw[v][0]), compression)
            committed.append(output)
    if all(name in raw for name in ('crain', 'csnow', 'cfrzr', 'cicep')):
        ensure_field(store, 'ptype', compression)
        core.write_compressed_chunk(store / 'ptype' / f'{hour}.0.0',
                                    derive_ptype({k: raw[k][0] for k in ('crain', 'csnow', 'cfrzr', 'cicep')}), compression)
        committed.append('ptype')
    return committed, pending


def recover_pending_accums(store, manifest, compression):
    changed = False
    for hour_text, entry in sorted(manifest['hours'].items(), key=lambda item: int(item[0])):
        hour = int(hour_text)
        for name, interval in entry.get('pending', {}).items():
            target = store / name / f'{hour}.0.0'
            if target.is_file():
                continue
            start, end = interval
            if end != hour:
                continue
            staged = store / '.accum_intervals' / name / f'{hour}.zlib'
            earlier = store / name / f'{start}.0.0'
            if not staged.is_file() or (start != 0 and not earlier.is_file()):
                continue
            data = zlib.decompress(staged.read_bytes())
            if start:
                data = combine_accums(zlib.decompress(earlier.read_bytes()), data)
            ensure_field(store, name, compression)
            core.write_compressed_chunk(target, data, compression)
            entry.setdefault('fields', []).append(name)
            entry['fields'] = sorted(set(entry['fields']))
            changed = True
    return changed


def scan_stores(output_root, protected=(), retention_days=7, explicitly_saved_cases=None):
    if explicitly_saved_cases is None:
        explicitly_saved_cases = frozenset(protected)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)
    results = {m: [] for m in CATALOG_MODELS}
    for store in sorted(list(output_root.glob('gfs_v*.zarr')) + list(output_root.glob('gfs_diff_*.zarr'))):
        match = re.fullmatch(r'(gfs_v16|gfs_v17|gfs_diff_v17_v16|gfs_diff_v16_analysis|gfs_diff_v17_analysis)_(\d{8})_(00|06|12|18)00\.zarr', store.name)
        if not match or store.is_symlink() or not store.is_dir():
            continue
        model, day, cc = match.groups()
        if shared.iso_init(day, cc) < cutoff and store.name not in protected and not (model.startswith('gfs_diff_') and any(f'{m}_{day}_{cc}00.zarr' in protected for m in MODELS)):
            continue
        try:
            attrs = json.loads((store / '.zattrs').read_text())
            complete = {i for i, state in enumerate(read_status(store)) if state}
            if not complete:
                continue
            variables = []
            for path in sorted(store.iterdir()):
                if not path.is_dir() or not (path / '.zarray').is_file() or path.name in ('processed', 'forecast_hour', 'valid_time', 'latitude', 'longitude'):
                    continue
                md = json.loads((path / '.zarray').read_text())
                hours = sorted(int(m.group(1)) for child in path.iterdir()
                               if child.is_file() and (m := CHUNKS.fullmatch(child.name)) and int(m.group(1)) in complete)
                if hours:
                    variables.append({'name': path.name, 'shape': md['shape'], 'chunks': md['chunks'],
                                      'dtype': md['dtype'], 'compressor': md['compressor'],
                                      'attrs': json.loads((path / '.zattrs').read_text()),
                                      'available_hours': hours})
            if variables:
                results[model].append({'id': store.stem, 'path': store.name,
                                       'model': attrs['model'], 'domain': attrs['domain'],
                                       'init_time': attrs['init_time'], 'grid': native_grid_attrs(),
                                       'variables': variables, 'is_case': store.name in protected and store.name in explicitly_saved_cases})
        except (OSError, ValueError, KeyError, zlib.error) as exc:
            logging.warning('Skipping incomplete GFS catalog store %s: %s', store, exc)
    for model in CATALOG_MODELS:
        results[model].sort(key=lambda e: e['init_time'], reverse=True)
        core.json_write(output_root / f'catalog_{model}.json', {
            'schema': 'rrfs-zarr-viewer-1',
            'generated_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
            'datasets': results[model]})
    return results


def explicit_case_stores(path):
    import json
    from retention_policy import ALL_PROTECTED_MODELS
    cfg=json.loads(Path(path).read_text(encoding='utf-8'))
    result=set()
    for item in cfg.get('protected_cycles', []):
        if isinstance(item,str):
            date,cycle=item.split('_');models=ALL_PROTECTED_MODELS
        else:
            date,cycle=item['date'],item['cycle'];models=item.get('models',ALL_PROTECTED_MODELS)
        for model in models:
            result.add(f'{model}_{date}_{cycle}00.zarr')
    return frozenset(result)


def clean_local(output, work, protected, days=7, dry_run=False, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=days)
    removed = []
    for root, pattern in ((output, 'gfs_v*.zarr'),(output,'gfs_diff_*.zarr'),(output,'sounding_gfs_v*.zarr'),
                          (work / 'gfs_manifests', 'gfs_v*.zarr.json'),
                          (work / '.published', 'gfs_v*.zarr.json'),
                          (work / '.published', 'sounding_gfs_v*.zarr.json'),
                          (work, 'gfs_v*.tar'),(work,'sounding_gfs_v*.tar')):
        if root.is_symlink() or not root.is_dir():
            continue
        for candidate in sorted(root.glob(pattern)):
            stem = candidate.name[:-5] if candidate.name.endswith('.json') else candidate.name
            if stem.endswith('.delta.tar'):
                stem = stem[:-10] + '.zarr'
            elif stem.endswith('.tar'):
                stem = stem[:-4] + '.zarr'
            profile_stem=stem.startswith('sounding_')
            match = re.fullmatch(r'(?:gfs_v16|gfs_v17|gfs_diff_v17_v16|gfs_diff_v16_analysis|gfs_diff_v17_analysis)_(\d{8})_(00|06|12|18)00\.zarr', stem[9:] if profile_stem else stem)
            if (not match or candidate.is_symlink() or
                    shared.iso_init(match.group(1), match.group(2)) >= cutoff or
                    ((stem[9:] if profile_stem else stem) in protected or (stem.startswith('gfs_diff_') and any(f'{m}_{match.group(1)}_{match.group(2)}00.zarr' in protected for m in MODELS)))):
                continue
            removed.append(str(candidate))
            if not dry_run:
                if candidate.is_dir():
                    shutil.rmtree(candidate)
                elif candidate.is_file():
                    candidate.unlink()
    return removed


@perf_monitor.timed_task('gfs_catalog_publication')
def publish_catalogs_only(output, work, host, remote_dir):
    """Catalog updates after cleanup; upload only the two GFS catalog files."""
    import shlex
    import publish_transport
    shared.validate_publisher(host, remote_dir)
    for model in CATALOG_MODELS:
        source = output / f'catalog_{model}.json'
        if not source.is_file():
            continue
        target = f'.{source.name}.{os.getpid()}.incoming'
        publish_transport.run(['scp', str(source), f'{host}:{remote_dir}/{target}'], check=True)
        publish_transport.run(['ssh', host,
                               'cd ' + shlex.quote(remote_dir) + ' && mv -f ' +
                               shlex.quote(target) + ' ' + shlex.quote(source.name)], check=True)


@perf_monitor.timed_task('gfs_store_publication')
def publish_stores(output, work, host, remote_dir, paths):
    shared.validate_publisher(host, remote_dir)
    for store in paths:
        match = re.fullmatch(r'(gfs_v16|gfs_v17|gfs_diff_v17_v16|gfs_diff_v16_analysis|gfs_diff_v17_analysis)_(\d{8})_(00|06|12|18)00\.zarr', store.name)
        if not match: raise ValueError('Unexpected GFS publication store: '+store.name)
        model = match.group(1)
        shared._publish_delta(store, output / f'catalog_{model}.json', work, host, remote_dir)


@perf_monitor.timed_task('gfs_remote_cleanup')
def cleanup_remote(host, remote_dir, protected, days, dry_run=False):
    """Delegate to a narrow allowlist; prevent directory traversal/symlink deletion."""
    import shlex
    import publish_transport
    shared.validate_publisher(host, remote_dir)
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime('%Y%m%d%H')
    script = r'''import pathlib, re, shutil, sys
root=pathlib.Path(sys.argv[1]); cutoff=sys.argv[2]; dry=sys.argv[3]=='1'; protected=set(sys.argv[4].split(','))
pattern=re.compile(r'^(?:sounding_)?(gfs_v16|gfs_v17|gfs_diff_v17_v16|gfs_diff_v16_analysis|gfs_diff_v17_analysis)_(\d{8})_(00|06|12|18)00\.zarr$')
stale=re.compile(r'^\.(?:sounding_)?(gfs_v16|gfs_v17|gfs_diff_v17_v16|gfs_diff_v16_analysis|gfs_diff_v17_analysis)_(\d{8})_(00|06|12|18)00(?:\.delta)?\.tar\.incoming$')
if root.name!='data' or root.is_symlink() or not root.is_dir(): raise SystemExit('unsafe GFS remote root')
for p in root.iterdir():
 m=pattern.fullmatch(p.name)
 if m and not p.is_symlink() and (m.group(2)+m.group(3))<cutoff and (p.name[9:] if p.name.startswith('sounding_') else p.name) not in protected and not (m.group(1).startswith('gfs_diff_') and any(f'{model}_{m.group(2)}_{m.group(3)}00.zarr' in protected for model in ('gfs_v16','gfs_v17'))) and p.is_dir():
  print(('WOULD REMOVE ' if dry else 'REMOVE ')+str(p))
  if not dry:shutil.rmtree(p)
 m2=stale.fullmatch(p.name)
 if m2 and p.is_file() and not p.is_symlink():
  # Expire interrupted transfers only after BOTH their cycle and actual
  # modification time are older than the configured cutoff.
  import datetime as dt
  stamp=dt.datetime.fromtimestamp(p.stat().st_mtime,dt.timezone.utc).strftime('%Y%m%d%H')
  if (m2.group(2)+m2.group(3))<cutoff and stamp<cutoff:
   print(('WOULD REMOVE ' if dry else 'REMOVE ')+str(p))
   if not dry:p.unlink()
'''
    command = 'cd '+shlex.quote(remote_dir)+' && flock -w 120 .evu_profiles_catalog.lock python3 - ' + ' '.join(shlex.quote(arg) for arg in
                  (remote_dir, cutoff, '1' if dry_run else '0', ','.join(sorted(protected))))
    subprocess.run(['ssh', host, command], input=script, text=True, check=True)


def parse(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--date', required=True, help='UTC YYYYMMDD')
    p.add_argument('--cycle', required=True, choices=('00', '06', '12', '18'))
    p.add_argument('--models', default='all', help='gfs_v16,gfs_v17, or all')
    p.add_argument('--hours', default=None, help='3-hourly forecast hours (e.g. 0-240)')
    p.add_argument('--workers', type=int, default=4)
    p.add_argument('--max-hour-jobs', type=int, default=0, help='Maximum eligible lead jobs per model; 0 processes all available missing hours')
    p.add_argument('--output-root', type=Path, default=Path('./zarr'))
    p.add_argument('--work-root', type=Path, default=Path('./work'))
    p.add_argument('--wgrib2', default=None)
    p.add_argument('--v16-base-url', default=V16_BASE)
    p.add_argument('--v17-base-dir', default=V17_BASE)
    p.add_argument('--compression-level', type=int, default=3)
    p.add_argument('--timeout', type=int, default=90)
    p.add_argument('--retries', type=int, default=2)
    p.add_argument('--no-publish', action='store_true')
    p.add_argument('--skip-marine', action='store_true', help='Skip optional GFSv16/v17 common wave ingest')
    p.add_argument('--wave-base-dir', default=V17_BASE, help='Local v17 root containing wave/gridded/global.0p16')
    p.add_argument('--skip-profiles', action='store_true', help='Disable native-grid sounding generation for this run')
    p.add_argument('--publish-host', default=core.DEFAULT_PUBLISH_HOST)
    p.add_argument('--publish-dir', default=core.DEFAULT_PUBLISH_DIR)
    p.add_argument('--retention-days', type=int, default=7)
    p.add_argument('--protected-cycles-file', type=Path, default=Path(__file__).with_name('protected_cycles.json'))
    p.add_argument('--cleanup-only', action='store_true')
    p.add_argument('--cleanup-dry-run', action='store_true')
    p.add_argument('--inventory-only', action='store_true')
    p.add_argument('--no-remote-cleanup', action='store_true')
    p.add_argument('--retry-failed-minutes', type=int, default=30)
    p.add_argument('--force-hours', action='store_true', help='Reprocess already completed hours')
    p.add_argument('--verbose', action='store_true')
    a = p.parse_args(argv)
    choices = MODELS if a.models == 'all' else tuple(x.strip() for x in a.models.split(','))
    if not choices or any(x not in MODELS for x in choices):
        p.error('--models must be gfs_v16,gfs_v17, or all')
    a.models = tuple(dict.fromkeys(choices))
    if a.hours is not None:
        try:
            bits = set()
            for part in a.hours.split(','):
                if '-' in part:
                    start, stop = map(int, part.split('-', 1))
                    if start > stop or start % 3 or stop % 3:
                        raise ValueError('hour range must be ascending multiples of three')
                    bits.update(range(start, stop + 1, 3))
                else:
                    bits.add(int(part))
            if not bits or not bits.issubset(set(HOURS)):
                raise ValueError('unsupported hour')
            a.hours = tuple(sorted(bits))
        except (ValueError, TypeError):
            p.error('--hours must be 3-hourly hours in 0..240')
    else:
        a.hours = HOURS
    if a.workers<1:
        p.error('--workers must be positive')
    if a.max_hour_jobs<0:
        p.error('--max-hour-jobs must be non-negative (0 means all eligible hours)')
    if not 1 <= a.retention_days <= 366 or not 0 <= a.compression_level <= 9 or not 1 <= a.retry_failed_minutes <= 1440:
        p.error('invalid retention/compression configuration')
    try:
        shared.iso_init(a.date, a.cycle)
        a.wgrib2 = core.find_wgrib2(a.wgrib2) if not a.cleanup_only else a.wgrib2
        a.protected = shared.load_protected_cycles(a.protected_cycles_file)
    except (RuntimeError, ValueError, OSError) as exc:
        p.error(str(exc))
    return a


@perf_monitor.monitored('gfs_conversion')
def run(argv=None):
    # Configure logging BEFORE parse(): load_protected_cycles() calls
    # logging.info(), which can install the default WARNING-level handler.
    # Calling basicConfig() after that silently leaves all inventory INFO
    # messages suppressed. Preserve caller-supplied handlers where present.
    log_args = sys.argv[1:] if argv is None else argv
    log_level = logging.DEBUG if '--verbose' in log_args else logging.INFO
    logging.basicConfig(level=log_level,
                        format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger().setLevel(log_level)
    a = parse(argv)
    perf_monitor.record_event('run_config', workflow='gfs_conversion', date=a.date, cycle=a.cycle,
        models=list(a.models), selected_hours=len(a.hours), workers=a.workers,
        max_hour_jobs=a.max_hour_jobs, inventory_only=a.inventory_only,
        publish=not a.no_publish, profiles=not a.skip_profiles)
    out, work = a.output_root.expanduser().resolve(), a.work_root.expanduser().resolve()
    if len(out.parts) < 3 or len(work.parts) < 3 or out == work or out.is_symlink() or work.is_symlink():
        raise ValueError('Unsafe processing output/work root')
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    touched = []
    touched_profiles = []
    if not a.cleanup_only:
        for model in a.models:
            store = out / f'{model}_{a.date}_{a.cycle}00.zarr'
            if not a.inventory_only:
                create_store(store, model, a.date, a.cycle, a.compression_level)
            state = read_status(store) if store.is_dir() else bytearray(241)
            profile_store = out / f'sounding_{model}_{a.date}_{a.cycle}00.zarr'
            def has_profile(hour):
                return (profile_store / '.hours' / f'{hour:03d}.json').is_file()
            manifest = load_manifest(work, store)
            now = dt.datetime.now(dt.timezone.utc)
            def eligible(hour):
                if a.force_hours:
                    return True
                if state[hour] and (a.skip_profiles or has_profile(hour)):
                    return False
                old = manifest['hours'].get(str(hour), {})
                if old.get('status') not in ('unavailable','failed','profile_missing'):
                    return True
                try:
                    last = dt.datetime.fromisoformat(old['checked_utc'])
                    return now - last >= dt.timedelta(minutes=a.retry_failed_minutes)
                except (ValueError, KeyError, TypeError):
                    return True
            candidates = [h for h in a.hours if eligible(h)]
            if a.max_hour_jobs:
                candidates = candidates[:a.max_hour_jobs]
            logging.info('%s %s%s selected %d/%d 3-hourly leads', model, a.date, a.cycle, len(candidates), len(a.hours))
            perf_monitor.record_event('work_selection', workflow='gfs_conversion', model=model,
                eligible_hours=len(candidates), requested_hours=len(a.hours),
                already_complete_or_deferred=len(a.hours)-len(candidates))
            if a.inventory_only and not candidates:
                logging.warning('%s inventory: no eligible leads; use --force-hours to inspect already completed or recently checked hours', model)
            if a.inventory_only:
                for hour in candidates[:2]:
                    if model == 'gfs_v16':
                        text = core.HTTPClient(timeout=a.timeout, retries=a.retries).text(v16_url(a.date,a.cycle,hour,a.v16_base_url)+'.idx')
                    else:
                        path = v17_path(a.date,a.cycle,hour,a.v17_base_dir)
                        if not path.is_file():
                            logging.info('%s F%03d: missing local file', model,hour)
                            continue
                        text = subprocess.run([a.wgrib2,str(path),'-s'],check=True,text=True,stdout=subprocess.PIPE).stdout
                    rows = parse_idx(text, 10**12)
                    chosen = choose_records(rows, hour)
                    logging.info('%s F%03d has %d matched fields: %s',model,hour,len(chosen),','.join(chosen))
                    if not a.skip_profiles:
                        native=profiles.select(rows)
                        counts={v:sum(1 for k in native if k[0]==v) for v in ('t','q','u','v','z')}
                        logging.info('%s F%03d primary profile records: %s; surface terrain height=%s',model,hour,
                                     counts,('yes' if ('z_sfc','surface') in native else 'no'))
                        extra_source = supplemental_profile_source(model, v16_url(a.date,a.cycle,hour,a.v16_base_url)
                            if model=='gfs_v16' else v17_path(a.date,a.cycle,hour,a.v17_base_dir))
                        try:
                            if model=='gfs_v16':
                                client=core.HTTPClient(timeout=a.timeout,retries=a.retries)
                                extra_rows=parse_idx(client.text(extra_source+'.idx'),10**12)
                            elif extra_source.is_file():
                                extra_rows=parse_idx(subprocess.run([a.wgrib2,str(extra_source),'-s'],
                                    check=True,text=True,stdout=subprocess.PIPE).stdout,10**12)
                            else: extra_rows=[]
                            added=additional_profile_records(native,extra_rows)
                            coverage=native_profile_coverage(native,extra_rows)
                            combined={v:len(coverage[v]) for v in counts}
                            logging.info('%s F%03d combined native profile records: %s; additional=%d; '
                                         'Td-capable=%d/%d (RH supplemental=%d)',
                                model,hour,combined,len(added),len(coverage['td_capable']),
                                len(profiles.PRESSURES),len(coverage['rh']))
                        except (urllib.error.HTTPError,urllib.error.URLError,TimeoutError,OSError,
                                subprocess.CalledProcessError) as exc:
                            logging.warning('%s F%03d optional companion inventory unavailable: %s',model,hour,exc)
                continue
            with ThreadPoolExecutor(max_workers=a.workers) as pool:
                futures = {pool.submit(process_hour,model,a.date,a.cycle,h,
                                       a.v17_base_dir,a.v16_base_url,a.wgrib2,a.timeout,a.retries,
                                       not a.skip_profiles, bool(state[h] and not a.force_hours)):h
                           for h in candidates}
                for future in as_completed(futures):
                    hour = futures[future]
                    try:
                        result = future.result()
                        if result['status'] != 'complete':
                            logging.info('%s F%03d %s: %s',model,hour,result['status'],result.get('reason'))
                            manifest['hours'][str(hour)] = {'status':result['status'],'checked_utc':dt.datetime.now(dt.timezone.utc).isoformat()}
                            continue
                        committed, pending = commit_hour(store,work,result,a.compression_level)
                        derived_names=[]
                        profile_written=False
                        if not a.skip_profiles and result.get('profile_fields'):
                            pstore,derived = profiles.commit(out,model,a.date,a.cycle,hour,
                                result['profile_fields'],result['fields'],NX,NY,native_grid_attrs(),a.compression_level)
                            if pstore is not None:
                                touched_profiles.append(pstore)
                                profile_written=True
                                for name,data in derived.items():
                                    # Native HLCY always takes precedence, and
                                    # only physical AGL reconstructions are written.
                                    if name in result['fields']:
                                        continue
                                    ensure_field(store,name,a.compression_level)
                                    core.write_compressed_chunk(store/name/f'{hour}.0.0',data,a.compression_level)
                                    derived_names.append(name)
                                profiles.mark_hour_complete(pstore,hour,result['profile_fields'],derived_names)
                                logging.info('%s F%03d: native-grid sounding written; %d derived shear/SRH fields',model,hour,len(derived_names))
                            else:
                                logging.warning('%s F%03d: no native pressure temperatures; profile will be retried',model,hour)
                        committed.extend(derived_names)
                        original=manifest['hours'].get(str(hour),{}) if result.get('profile_only') else {}
                        manifest['hours'][str(hour)] = {'status':('complete' if a.skip_profiles or profile_written else 'profile_missing'),
                            'checked_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
                            'fields':sorted(set(original.get('fields',[]))|set(committed)),
                            'pending':{**original.get('pending',{}),**pending},
                            'derived_fields':derived_names,
                            'source':result['source']}
                        if committed:
                            state[hour] = 1
                            shared.write_processed(store, state, a.compression_level)
                        touched.append(store)
                        logging.info('%s F%03d: %d fields written, profile=%s',model,hour,len(committed),
                                     'disabled' if a.skip_profiles else 'complete' if profile_written else 'pending')
                    except Exception as exc:
                        logging.exception('%s F%03d processing failed: %s',model,hour,exc)
                        manifest['hours'][str(hour)] = {'status':'failed','error':str(exc),
                                                        'checked_utc':dt.datetime.now(dt.timezone.utc).isoformat()}
                    finally:
                        write_manifest(work,store,manifest)
            if recover_pending_accums(store,manifest,a.compression_level):
                touched.append(store)
                write_manifest(work,store,manifest)
    if a.inventory_only:
        return 0
    # Wave readiness is independent of atmospheric completion for BOTH versions.
    # Only common operational wave fields are selected from their inventories.
    if not a.skip_marine:
        import gfs_marine
        try:
            touched.extend(gfs_marine.ingest(sys.modules[__name__], out, work, a.date, a.cycle,
                a.hours, a.v16_base_url, a.wave_base_dir, a.wgrib2, a.workers,
                a.compression_level, a.force_hours, a.models))
        except Exception:
            logging.exception('Nonfatal GFS marine ingest problem')
    # Native-grid difference stores are incremental and regenerated whenever a
    # late forecast or valid-time F000 analysis becomes available.
    import gfs_differences
    changed_differences = gfs_differences.compute(out, a.date, a.cycle, a.hours,
                                                  a.compression_level, force=a.force_hours)
    touched.extend(changed_differences)
    # A newly published v16 F000 unlocks verifying comparisons for forecasts
    # initialized up to ten days earlier. Only revisit their matching lead,
    # avoiding an expensive 81-hour rescan of each prior GFS cycle.
    current_store = out / f'gfs_v16_{a.date}_{a.cycle}00.zarr'
    if (current_store / 'processed' / '0').is_file() and read_status(current_store)[0]:
        current_init = shared.iso_init(a.date, a.cycle)
        for lead in range(6, 241, 6):
            prior = current_init - dt.timedelta(hours=lead)
            day, cyc = prior.strftime('%Y%m%d'), prior.strftime('%H')
            if any((out / f'{m}_{day}_{cyc}00.zarr').is_dir() for m in MODELS):
                touched.extend(gfs_differences.compute(out, day, cyc, (lead,), a.compression_level))
    removed = clean_local(out,work,a.protected,a.retention_days,dry_run=a.cleanup_dry_run)
    if removed:
        logging.info('%s local retention candidates: %s',len(removed),removed[:15])
    if not a.cleanup_dry_run:
        scan_stores(out,a.protected,a.retention_days,explicit_case_stores(a.protected_cycles_file))
        # Shared profile catalog includes the other operational models.
        sounding.refresh_catalog(out)
    if not a.no_publish and not a.cleanup_dry_run:
        from publish_transport import session
        with session():
            if touched:
                publish_stores(out,work,a.publish_host,a.publish_dir,list(dict.fromkeys(touched)))
            # Profile stores are delta-published independently and cataloged
            # only after the chunks are confirmed transferred.
            profile_candidates = set(touched_profiles)
            if not a.skip_profiles:
                for model in a.models:
                    pstore=out/f'sounding_{model}_{a.date}_{a.cycle}00.zarr'
                    if (pstore/'.hours').is_dir() and any((pstore/'.hours').glob('*.json')):
                        profile_candidates.add(pstore)
            for pstore in sorted(profile_candidates):
                sounding.web_permissions(pstore)
                shared.publish_one(pstore,None,work,a.publish_host,a.publish_dir,publish_mode='auto')
            # Also refresh catalog after a retention-only sweep, so deleted
            # profile stores cannot linger as stale remote entries.
            catalog=sounding.refresh_catalog(out,published_only=True,published_work=work,
                destination=work/'catalog_profiles.json')
            import profile_catalog_publish
            profile_catalog_publish.publish(catalog,a.publish_host,a.publish_dir)
            publish_catalogs_only(out,work,a.publish_host,a.publish_dir)
            if not a.no_remote_cleanup:
                cleanup_remote(a.publish_host,a.publish_dir,a.protected,a.retention_days)
    elif not a.no_publish and a.cleanup_dry_run and not a.no_remote_cleanup:
        cleanup_remote(a.publish_host,a.publish_dir,a.protected,a.retention_days,dry_run=True)
    return 0


if __name__ == '__main__':
    sys.exit(run())
