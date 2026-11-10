#!/usr/bin/env python3
"""Experimental, read-only weather-event discovery for EVU Zarr v2 catalogs.

Python >=3.6, standard library only. Nothing is automatically published or
preserved; this scanner never edits a model store or invokes a converter.

The local reader is deliberately separated from the detectors. A future
cloud-backed adapter can implement read_hour() without changing the event feed
or the meteorological tests.
"""
from __future__ import print_function

import argparse
from array import array
from collections import OrderedDict, Counter
from datetime import datetime, timedelta, timezone
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import zlib

SCHEMA = 'evu-event-discovery-preview-2'
MPS_TO_KT = 1.9438444924406
DEFAULT_MODELS = ('rrfs', 'hrrr', 'refs_mean', 'gfs_v16', 'gfs_v17',
                  'aigfs', 'aigefs_mean', 'hgefs_mean', 'gefs_mean')
DEFAULT_THRESHOLDS = {
    'precip_6h_mm': 40.0,
    'snow_6h_cm': 15.0,
    'gust_kt': 45.0,
    'wind_10m_kt': 40.0,
    'low_mslp_hpa': 985.0,
    'cape_jkg': 2500.0,
    'uh_m2s2': 100.0,
    'gfs_mslp_difference_hpa': 7.0,
    'gfs_500hpa_height_difference_dm': 7.0,
    'gfs_850hpa_temperature_difference_k': 5.0,
}
CATEGORY_TITLES = {
    'heavy_precipitation': 'Heavy 6-hour precipitation forecast',
    'heavy_snowfall': 'Heavy 6-hour snowfall forecast',
    'strong_wind': 'Strong surface wind forecast',
    'deep_low': 'Deep surface low forecast',
    'high_cape': 'High convective instability forecast',
    'strong_updraft': 'Strong updraft-helicity signal',
    'model_disagreement': 'GFS version disagreement',
}
MODEL_SETS = {
    'rrfs': 'rrfs', 'hrrr': 'rrfs', 'refs_mean': 'rrfs',
    'gfs_v16': 'gfs', 'gfs_v17': 'gfs',
    'aigfs': 'ai', 'aigefs_mean': 'ai', 'hgefs_mean': 'ai',
    'gefs_mean': 'ai',
}

# These approximate geographic envelopes are INTERSECTED with each published
# model grid, not substituted for it. A bounding rectangle alone would include
# large sections of remote Pacific/Atlantic Ocean on the GFS/AI native grids.
# Coordinates in polygons and rectangles are [longitude, latitude].
# Keep all polygons away from the antimeridian; western Aleutians are separate.
DEFAULT_REGION_PROFILES = {
    'rrfs': [
        {'name': 'CONUS and nearby areas', 'polygon': [
            [-132, 52], [-132, 42], [-129, 33], [-123, 27], [-116, 22],
            [-105, 20], [-94, 20], [-84, 22], [-77, 26], [-68, 34],
            [-63, 40], [-64, 49], [-74, 55], [-99, 55], [-121, 54],
        ]},
    ],
    'gfs': [
        {'name': 'Mainland North America and coastal waters', 'polygon': [
            [-175, 61], [-174, 68], [-161, 75], [-145, 78], [-115, 79],
            [-88, 82], [-66, 82], [-49, 76], [-48, 60], [-47, 49],
            [-56, 42], [-65, 31], [-69, 23], [-74, 18], [-78, 16],
            [-82, 12], [-87, 12], [-94, 14], [-103, 15], [-111, 21], [-119, 27],
            [-127, 32], [-133, 43], [-145, 53], [-162, 52],
        ]},
        # Retain Central America and adjoining waters without opening the
        # southern Caribbean to thousands of kilometres of remote-ocean CAPE.
        {'name': 'Central America and nearby coastal waters', 'polygon': [
            [-94, 17], [-87, 18], [-81, 13], [-77, 10],
            [-77, 7], [-84, 7], [-90, 12], [-94, 14],
        ]},
        {'name': 'Western Aleutians', 'bbox': [170, 50, 180, 59]},
        {'name': 'Hawaii and nearby waters', 'bbox': [-163, 17, -153, 24]},
        {'name': 'Puerto Rico and nearby waters', 'bbox': [-69, 16, -62, 21]},
        {'name': 'Greenland', 'polygon': [
            [-64, 58], [-58, 76], [-45, 85], [-22, 83], [-34, 58],
        ]},
    ],
}
# AI and GFS products use equivalent geographic selection, although the
# published grid/field availability may differ for each individual model.
DEFAULT_REGION_PROFILES['ai'] = [dict(a) for a in DEFAULT_REGION_PROFILES['gfs']]


def point_in_polygon(lon, lat, vertices):
    # Ray-casting with boundary inclusion (a selected grid cell on a polygon
    # edge should not disappear). Only simple, non-dateline polygons supported.
    inside = False
    previous = vertices[-1]
    for current in vertices:
        x1, y1 = previous
        x2, y2 = current
        cross = (lon - x1) * (y2 - y1) - (lat - y1) * (x2 - x1)
        if abs(cross) < 1e-9 and min(x1, x2) <= lon <= max(x1, x2) and min(y1, y2) <= lat <= max(y1, y2):
            return True
        if (y1 > lat) != (y2 > lat):
            at_x = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < at_x:
                inside = not inside
        previous = current
    return inside


class GeographicPolicy(object):
    """The same model-group geographic rules apply to coarse AND fine scans.

    Each region intersects the published grid, so a detection can never be
    generated where a model lacks spatial or temporal field coverage.
    """
    def __init__(self, profiles=None):
        profiles = profiles or DEFAULT_REGION_PROFILES
        self.profiles = {}
        for group in ('rrfs', 'gfs', 'ai'):
            regions = profiles.get(group)
            if not isinstance(regions, list) or not regions:
                raise ValueError('missing geographic profile for ' + group)
            accepted = []
            for region in regions:
                if not isinstance(region, dict) or not isinstance(region.get('name'), str) or not region['name'].strip():
                    raise ValueError('invalid region name for ' + group)
                has_box = 'bbox' in region
                has_poly = 'polygon' in region
                if has_box == has_poly:
                    raise ValueError('region must have exactly one bbox or polygon')
                key = 'bbox' if has_box else 'polygon'
                coords = region[key]
                if key == 'bbox':
                    if not isinstance(coords, list) or len(coords) != 4:
                        raise ValueError('invalid region bbox')
                    coords = [self.validate_number(x) for x in coords]
                    if not (-180 <= coords[0] < coords[2] <= 180 and -90 <= coords[1] < coords[3] <= 90):
                        raise ValueError('region bbox must be west,south,east,north without dateline crossing')
                else:
                    if not isinstance(coords, list) or not (3 <= len(coords) <= 100):
                        raise ValueError('invalid region polygon')
                    coords = [self.validate_vertex(v) for v in coords]
                accepted.append({'name': region['name'].strip(), key: coords})
            self.profiles[group] = accepted

    @staticmethod
    def validate_number(value):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError('geographic coordinates must be finite numbers')
        return float(value)

    @classmethod
    def validate_vertex(cls, vertex):
        if not isinstance(vertex, list) or len(vertex) != 2:
            raise ValueError('polygon vertex must be [longitude,latitude]')
        lon, lat = [cls.validate_number(x) for x in vertex]
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError('invalid polygon coordinate')
        return [lon, lat]

    def region(self, model, lat, lon):
        if not (-90 <= lat <= 90) or not math.isfinite(lat) or not math.isfinite(lon):
            return None
        lon = normalized_longitude(lon)
        for area in self.profiles[MODEL_SETS[model]]:
            if 'bbox' in area:
                west, south, east, north = area['bbox']
                if west <= lon <= east and south <= lat <= north:
                    return area['name']
            elif point_in_polygon(lon, lat, area['polygon']):
                return area['name']
        return None

    def permits(self, model, lat, lon):
        return self.region(model, lat, lon) is not None


def load_geographic_policy(path):
    if path is None:
        return GeographicPolicy()
    with Path(path).open('r') as f:
        return GeographicPolicy(json.load(f))


def utcnow():
    return datetime.now(timezone.utc)


def parse_utc(text):
    # Catalog timestamps can have fractional seconds or a terminal Z.
    stamp = str(text).strip().replace('Z', '+00:00')
    if stamp.endswith('+00:00'):
        stamp = stamp[:-6]
    if '.' in stamp:
        stamp = stamp.split('.', 1)[0]
    return datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)


def iso_utc(value):
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def is_finite(value):
    return math.isfinite(value) and abs(value) < 1.0e15


def geographic_distance_km(a, b):
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2.0) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2
    return 6371.0 * 2.0 * math.asin(min(1.0, math.sqrt(h)))


def normalized_longitude(lon):
    return (lon + 180.0) % 360.0 - 180.0


class Geography(object):
    """Index to geographic coordinates for the two grids used in EVU catalogs."""
    def __init__(self, grid):
        self.grid = grid
        self.nx = int(grid['GRIB_Nx'])
        self.ny = int(grid['GRIB_Ny'])
        self.kind = str(grid.get('GRIB_gridType', '')).lower()
        if not (1 < self.nx <= 20000 and 1 < self.ny <= 20000):
            raise ValueError('unsupported grid dimensions')
        if self.kind == 'regular_ll':
            self.lon0 = float(grid['GRIB_longitudeOfFirstGridPointInDegrees'])
            self.lat0 = float(grid['GRIB_latitudeOfFirstGridPointInDegrees'])
            self.dx = float(grid['GRIB_DxInDegrees'])
            self.dy = float(grid['GRIB_DyInDegrees'])
            if not (0 < self.dx < 5 and 0 < self.dy < 5):
                raise ValueError('invalid latitude/longitude increments')
        elif self.kind == 'lambert':
            deg = math.pi / 180.0
            radius = 6371229.0
            std_lat = float(grid['GRIB_Latin1InDegrees']) * deg
            self.n = math.sin(std_lat)
            self.f = (math.cos(std_lat) * math.tan(math.pi / 4.0 + std_lat / 2.0) ** self.n) / self.n
            self.radius = radius
            self.lon_ref = float(grid['GRIB_LoVInDegrees']) * deg
            self.rho0 = radius * self.f / math.tan(math.pi / 4.0 +
                            float(grid['GRIB_LaDInDegrees']) * deg / 2.0) ** self.n
            firstlat = float(grid['GRIB_latitudeOfFirstGridPointInDegrees']) * deg
            firstlon = float(grid['GRIB_longitudeOfFirstGridPointInDegrees']) * deg
            rho1 = radius * self.f / math.tan(math.pi / 4.0 + firstlat / 2.0) ** self.n
            self.x0 = rho1 * math.sin(self.n * (firstlon - self.lon_ref))
            self.y0 = self.rho0 - rho1 * math.cos(self.n * (firstlon - self.lon_ref))
            self.dx = float(grid['GRIB_DxInMetres'])
            self.dy = float(grid['GRIB_DyInMetres'])
        else:
            raise ValueError('unsupported grid projection: ' + self.kind)

    def latlon(self, ix, iy):
        if self.kind == 'regular_ll':
            return (self.lat0 + iy * self.dy,
                    normalized_longitude(self.lon0 + ix * self.dx))
        xx = self.x0 + ix * self.dx
        yy = self.rho0 - (self.y0 + iy * self.dy)
        rho = math.hypot(xx, yy)
        if rho < 1.0:
            return (90.0, normalized_longitude(math.degrees(self.lon_ref)))
        phi = 2.0 * math.atan((self.radius * self.f / rho) ** (1.0 / self.n)) - math.pi / 2.0
        lon = self.lon_ref + math.atan2(xx, yy) / self.n
        return (math.degrees(phi), normalized_longitude(math.degrees(lon)))

    def sample_stride(self, km):
        if self.kind == 'lambert':
            spacing_km = (self.dx + self.dy) / 2000.0
        else:
            # Conservative fixed estimate (longitude distance varies by latitude).
            spacing_km = max(1.0, 111.0 * min(self.dx, self.dy))
        return max(1, min(128, int(round(km / spacing_km))))


def grids_match(a, b):
    if set(a) != set(b):
        return False
    for key in a:
        va, vb = a[key], b[key]
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            if abs(float(va) - float(vb)) > 1e-6 * max(1.0, abs(float(va))):
                return False
        elif va != vb:
            return False
    return True


class LocalCatalogSource(object):
    """Read immutable, *published* EVU catalog entries and zlib Zarr v2 chunks.

    A cloud implementation can expose the same catalogs and read_hour method.
    This class requires neither a zarr library nor a running converter.
    """
    def __init__(self, root, max_chunk_mb=128, max_cache=3):
        self.root = Path(root).expanduser().resolve()
        self.max_chunk_bytes = int(max_chunk_mb * 1024 * 1024)
        self.max_cache = max(1, int(max_cache))
        self.cache = OrderedDict()
        self.bytes_read = 0
        self.chunks_read = 0
        self.errors = Counter()

    def load_catalog(self, model):
        if not model.replace('_', '').isalnum():
            raise ValueError('invalid model name')
        path = self.root / ('catalog_' + model + '.json')
        if not path.is_file():
            return []
        with path.open('r') as f:
            data = json.load(f)
        if data.get('schema') != 'rrfs-zarr-viewer-1':
            raise ValueError('unrecognized catalog schema: ' + model)
        datasets = []
        for entry in data.get('datasets', []):
            try:
                name = str(entry['path'])
                if Path(name).name != name or not name.endswith('.zarr'):
                    continue
                if not (self.root / name).is_dir():
                    continue
                info = dict(entry)
                info['_catalog'] = path.name
                info['_model_id'] = model
                info['_init_dt'] = parse_utc(info['init_time'])
                info['_vars'] = dict((v['name'], v) for v in info.get('variables', [])
                                     if isinstance(v, dict) and 'name' in v)
                info['_geo'] = Geography(info['grid'])
                datasets.append(info)
            except (ValueError, TypeError, KeyError, OverflowError, ZeroDivisionError):
                self.errors['invalid_catalog_entry'] += 1
        datasets.sort(key=lambda x: x['_init_dt'], reverse=True)
        return datasets

    def read_hour(self, ds, field, hour):
        """Return a flat readonly float32 memoryview, or None on absence.

        Hours are accepted only when present in the published catalog *and*
        chunk file; this prevents reading partially published or stale data.
        """
        variable = ds['_vars'].get(field)
        if not variable or int(hour) not in variable.get('available_hours', []):
            self.errors['field_hour_unavailable'] += 1
            return None
        name = ds['path']
        if Path(name).name != name or field not in ds['_vars']:
            return None
        try:
            # Whitelist field path to avoid using catalog content as paths.
            if not field.replace('_', '').isalnum():
                return None
            shape = variable['shape']
            geo = ds['_geo']
            if len(shape) != 3 or shape[1:] != [geo.ny, geo.nx]:
                self.errors['unexpected_shape'] += 1
                return None
            if variable.get('dtype') != '<f4' or variable.get('compressor', {}).get('id') != 'zlib':
                self.errors['unsupported_encoding'] += 1
                return None
            expected = geo.nx * geo.ny * 4
            if expected > self.max_chunk_bytes:
                self.errors['chunk_over_memory_limit'] += 1
                return None
            key = (name, field, int(hour))
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            path = self.root / name / field / ('%d.0.0' % int(hour))
            # Do not follow symlinks outside the selected data root.
            if not path.is_file() or path.is_symlink():
                self.errors['missing_chunk'] += 1
                return None
            if path.stat().st_size > self.max_chunk_bytes:
                self.errors['oversize_compressed_chunk'] += 1
                return None
            raw = zlib.decompress(path.read_bytes())
            if len(raw) != expected:
                self.errors['wrong_chunk_length'] += 1
                return None
            if sys.byteorder == 'little':
                values = memoryview(raw).cast('f')
            else:
                values = array('f')
                values.frombytes(raw)
                values.byteswap()
            self.bytes_read += path.stat().st_size
            self.chunks_read += 1
            self.cache[key] = values
            while len(self.cache) > self.max_cache:
                self.cache.popitem(last=False)
            return values
        except (OSError, ValueError, TypeError, KeyError, zlib.error, MemoryError):
            self.errors['invalid_chunk'] += 1
            return None


def top_extrema(geo, value_at, threshold, direction, sample_km, max_points=3, permitted=None):
    """Coarse global screen then exact local refinement, with distance exclusion.

    Returns value, latitude, longitude and grid indices. No invented extrema:
    every returned value comes from a read-only Zarr grid cell.
    """
    stride = geo.sample_stride(sample_km)
    candidates = []
    def sample(ix, iy):
        try:
            value = value_at(iy * geo.nx + ix)
            return float(value) if is_finite(value) else None
        except (IndexError, ValueError, TypeError, OverflowError):
            return None
    # Also sample boundaries; peaks can be near the edge of the domain.
    y_values = list(range(0, geo.ny, stride))
    x_values = list(range(0, geo.nx, stride))
    if y_values[-1] != geo.ny - 1:
        y_values.append(geo.ny - 1)
    if x_values[-1] != geo.nx - 1:
        x_values.append(geo.nx - 1)
    for iy in y_values:
        for ix in x_values:
            value = sample(ix, iy)
            if value is None:
                continue
            rank = -value if direction == 'min' else abs(value) if direction == 'abs' else value
            if rank >= threshold:
                if permitted is None or permitted(*geo.latlon(ix, iy)):
                    candidates.append((rank, ix, iy))
    # Top coarse candidates are inexpensive even on large regular grids.
    prelim = heapq.nlargest(18, candidates)
    exact = []
    seen = set()
    for _, cx, cy in prelim:
        top = None
        for iy in range(max(0, cy - stride), min(geo.ny, cy + stride + 1)):
            for ix in range(max(0, cx - stride), min(geo.nx, cx + stride + 1)):
                value = sample(ix, iy)
                if value is None:
                    continue
                rank = -value if direction == 'min' else abs(value) if direction == 'abs' else value
                if rank < threshold:
                    continue
                if permitted is not None and not permitted(*geo.latlon(ix, iy)):
                    continue
                if top is None or rank > top[0]:
                    top = (rank, value, ix, iy)
        if top is None or (top[2], top[3]) in seen:
            continue
        seen.add((top[2], top[3]))
        lat, lon = geo.latlon(top[2], top[3])
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            exact.append((top[0], top[1], lat, lon, top[2], top[3]))
    exact.sort(reverse=True)
    results = []
    for rank, value, lat, lon, ix, iy in exact:
        if all(geographic_distance_km((lat, lon), (p[2], p[3])) >= 160.0 for p in results):
            results.append((rank, value, lat, lon, ix, iy))
            if len(results) >= max_points:
                break
    return results


def nearest_hours(a_hours, b_hours, hour, delta):
    """Use only a complete fixed-length window, no invented F000 values."""
    return int(hour) in a_hours and int(hour - delta) in b_hours


def is_model_selected(model, requested):
    return model in requested


def select_hours(ds, names, hour_step, max_hour, max_frames):
    all_hours = set()
    for name in names:
        meta = ds['_vars'].get(name)
        if meta:
            all_hours.update(int(h) for h in meta.get('available_hours', []))
    choices = [h for h in sorted(all_hours) if h > 0 and h <= max_hour and h % hour_step == 0]
    return choices[:max_frames]


def model_event(category, ds, field, hour, value, unit, lat, lon, rank, other=None,
                window_hours=None, original_value=None, geographic_region=None):
    valid = ds['_init_dt'] + timedelta(hours=hour)
    model = ds['_model_id']
    token = '|'.join((category, model, ds['init_time'], field, str(hour),
                      str(round(lat, 1)), str(round(lon, 1))))
    ev_id = hashlib.sha1(token.encode('utf-8')).hexdigest()[:16]
    records = [{'model': model, 'dataset': ds['id'], 'store': ds['path'],
                'catalog': ds['_catalog'], 'field': field, 'forecast_hour': int(hour)}]
    if other:
        records.append({'model': other['_model_id'], 'dataset': other['id'],
                        'store': other['path'], 'catalog': other['_catalog'],
                        'field': field, 'forecast_hour': int(hour)})
    if category == 'model_disagreement' and other:
        comparison = '%s minus %s' % (model, other['_model_id'])
    else:
        comparison = None
    suggested_field = field
    if field == 'asnow':
        suggested_field = 'asnow'
    result = {
        'id': ev_id, 'category': category, 'title': CATEGORY_TITLES[category],
        'model': model, 'model_set': MODEL_SETS.get(model, 'unknown'),
        'init_time': iso_utc(ds['_init_dt']), 'valid_time': iso_utc(valid),
        'forecast_hour': int(hour),
        'location': {'latitude': round(lat, 4), 'longitude': round(lon, 4),
                     'region': geographic_region},
        'measurement': {'field': field, 'value': round(value, 2), 'units': unit},
        'relative_intensity': round(rank, 3),
        'source_records': records,
        'suggested_view': {'model_set': MODEL_SETS.get(model, 'unknown'),
                           'field': suggested_field, 'forecast_hour': int(hour),
                           'center': [round(lon, 4), round(lat, 4)],
                           'source_model': model},
        'status': 'unreviewed', 'experimental': True,
    }
    if comparison:
        result['comparison'] = comparison
        result['suggested_view']['difference'] = comparison
    if window_hours:
        result['measurement']['window_hours'] = int(window_hours)
        result['source_records'][0]['start_hour'] = int(hour - window_hours)
    if original_value is not None:
        result['measurement']['native_value'] = round(float(original_value), 3)
    return result


def event_candidates_from_field(source, ds, field, hour, category, threshold,
                                convert, direction='max', previous=None,
                                sample_km=75.0, permitted=None):
    chunk = source.read_hour(ds, field, hour)
    if chunk is None:
        return []
    before = source.read_hour(ds, field, previous) if previous is not None else None
    if previous is not None and before is None:
        return []
    geo = ds['_geo']
    if before is not None:
        def at(i):
            a, b = chunk[i], before[i]
            # Cumulative products can contain small negative rounding errors;
            # a negative larger than 0.1 native units is NOT valid accumulation.
            if not (is_finite(a) and is_finite(b)) or a - b < -0.1:
                return float('nan')
            return convert(max(0.0, a - b))
    else:
        def at(i):
            a = chunk[i]
            return convert(a) if is_finite(a) else float('nan')
    return top_extrema(geo, at, threshold, direction, sample_km, permitted=permitted)


def scan_dataset(source, ds, args, thresholds, coverage, geographic_policy):
    model = ds['_model_id']
    permitted = lambda lat, lon: geographic_policy.permits(model, lat, lon)
    names = ('apcp', 'asnow', 'gust', 'u10', 'v10', 'mslp', 'mlcape', 'sbcape', 'mxuphl')
    hours = select_hours(ds, names, args.hour_step, args.max_hour, args.max_frames_per_model)
    record = coverage.setdefault(model, {'datasets': 0, 'forecast_hours': 0, 'detections': 0,
                                         'screening_fields': [], 'eligible_forecast_hours': [],
                                         'catalog_fields': [], 'data_coverage_note': ''})
    record['datasets'] += 1
    tracked = set(names)
    record['catalog_fields'] = sorted(set(record['catalog_fields']).union(ds['_vars']))
    record['screening_fields'] = sorted(set(record['screening_fields']).union(
        x for x in ds['_vars'] if x in tracked))
    record['eligible_forecast_hours'] = sorted(set(record['eligible_forecast_hours']).union(hours))
    if not hours:
        if not (set(ds['_vars']) & tracked):
            record['data_coverage_note'] = 'No detector-supported fields in this catalog store'
        else:
            record['data_coverage_note'] = ('No eligible nonzero detector hours at the configured '
                                            'hour step, horizon and per-model frame cap')
    detections = []
    for hour in hours:
        record['forecast_hours'] += 1
        plans = (
            ('mslp', 'deep_low', -thresholds['low_mslp_hpa'], lambda x: x / 100.0, 'min', None, 'hPa', 'low_mslp_hpa'),
            ('gust', 'strong_wind', thresholds['gust_kt'], lambda x: x * MPS_TO_KT, 'max', None, 'kt', 'gust_kt'),
            ('mlcape', 'high_cape', thresholds['cape_jkg'], float, 'max', None, 'J kg-1', 'cape_jkg'),
            ('mxuphl', 'strong_updraft', thresholds['uh_m2s2'], float, 'max', None, 'm2 s-2', 'uh_m2s2'),
            ('apcp', 'heavy_precipitation', thresholds['precip_6h_mm'], float, 'max', hour - 6, 'mm', 'precip_6h_mm'),
            ('asnow', 'heavy_snowfall', thresholds['snow_6h_cm'], lambda x: x * 100.0, 'max', hour - 6, 'cm', 'snow_6h_cm'),
        )
        for field, category, cutoff, convert, direction, previous, unit, threshold_key in plans:
            if field not in ds['_vars']:
                continue
            if previous is not None and previous < 0:
                continue
            peaks = event_candidates_from_field(source, ds, field, hour, category,
                       cutoff, convert, direction, previous, args.sample_km, permitted)
            for rank, value, lat, lon, ix, iy in peaks:
                # Deeper lows must score higher, rather than the negative
                # ratio previously assigned to every pressure minimum.
                relative = (1.0 + max(0.0, thresholds[threshold_key] - value) / 20.0
                            if direction == 'min' else value / thresholds[threshold_key])
                detections.append(model_event(category, ds, field, hour, value, unit,
                                              lat, lon, relative,
                                              window_hours=6 if previous is not None else None,
                                              geographic_region=geographic_policy.region(model, lat, lon)))
        # Prefer gust, but fall back to 10-m vector magnitude if no gust exists.
        if 'gust' not in ds['_vars'] and 'u10' in ds['_vars'] and 'v10' in ds['_vars']:
            u = source.read_hour(ds, 'u10', hour)
            v = source.read_hour(ds, 'v10', hour)
            if u is not None and v is not None:
                def speed(i):
                    a, b = u[i], v[i]
                    return math.hypot(a, b) * MPS_TO_KT if is_finite(a) and is_finite(b) else float('nan')
                for rank, value, lat, lon, ix, iy in top_extrema(ds['_geo'], speed,
                       thresholds['wind_10m_kt'], 'max', args.sample_km, permitted=permitted):
                    ev = model_event('strong_wind', ds, 'wspd10_derived', hour, value, 'kt',
                                     lat, lon, value / thresholds['wind_10m_kt'],
                                     geographic_region=geographic_policy.region(model, lat, lon))
                    ev['source_records'][0]['field'] = 'u10+v10'
                    detections.append(ev)
        if 'mlcape' not in ds['_vars'] and 'sbcape' in ds['_vars']:
            peaks = event_candidates_from_field(source, ds, 'sbcape', hour, 'high_cape',
                         thresholds['cape_jkg'], float, sample_km=args.sample_km, permitted=permitted)
            for rank, value, lat, lon, ix, iy in peaks:
                detections.append(model_event('high_cape', ds, 'sbcape', hour, value,
                                  'J kg-1', lat, lon, value / thresholds['cape_jkg'],
                                  geographic_region=geographic_policy.region(model, lat, lon)))
    record['detections'] += len(detections)
    return detections


def compare_gfs(source, newest, prior, args, thresholds, coverage, geographic_policy):
    if not grids_match(newest['grid'], prior['grid']):
        coverage['gfs_difference'] = {'status': 'skipped_mismatched_grids'}
        return []
    definitions = (
        ('mslp', 'gfs_mslp_difference_hpa', lambda x: x / 100.0, 'hPa'),
        ('h500', 'gfs_500hpa_height_difference_dm', lambda x: x / 10.0, 'dm'),
        ('t850', 'gfs_850hpa_temperature_difference_k', float, 'K'),
    )
    results = []
    count = 0
    masked_850_field_hours = 0
    skipped_850_missing_psfc = 0
    for field, key, convert, unit in definitions:
        v1 = newest['_vars'].get(field)
        v0 = prior['_vars'].get(field)
        if not v1 or not v0:
            continue
        hours = sorted(set(v1['available_hours']).intersection(v0['available_hours']))
        hours = [int(h) for h in hours if 0 < int(h) <= args.max_hour and int(h) % args.hour_step == 0]
        hours = hours[:args.max_frames_per_model]
        for hour in hours:
            after = source.read_hour(newest, field, hour)
            before = source.read_hour(prior, field, hour)
            if after is None or before is None:
                continue
            # Subsurface isobaric temperatures must not be ranked as genuine
            # meteorological disagreement. At 850 hPa the native surface
            # pressure in BOTH versions must exceed the level pressure.
            # Fail closed if a supporting surface-pressure hour is missing.
            psfc_after = psfc_before = None
            if field == 't850':
                if 'psfc' not in newest['_vars'] or 'psfc' not in prior['_vars']:
                    skipped_850_missing_psfc += 1
                    continue
                psfc_after = source.read_hour(newest, 'psfc', hour)
                psfc_before = source.read_hour(prior, 'psfc', hour)
                if psfc_after is None or psfc_before is None:
                    skipped_850_missing_psfc += 1
                    continue
                masked_850_field_hours += 1
            def delta(i):
                a, b = after[i], before[i]
                if psfc_after is not None:
                    pa, pb = psfc_after[i], psfc_before[i]
                    if not (is_finite(pa) and is_finite(pb)) or min(pa, pb) < 85000.0:
                        return float('nan')
                return convert(a - b) if is_finite(a) and is_finite(b) else float('nan')
            for rank, signed_value, lat, lon, ix, iy in top_extrema(
                    newest['_geo'], delta, thresholds[key], 'abs', args.sample_km,
                    permitted=lambda lat, lon: geographic_policy.permits('gfs_v17', lat, lon)):
                ev = model_event('model_disagreement', newest, field, hour,
                                 signed_value, unit, lat, lon,
                                 abs(signed_value) / thresholds[key], other=prior,
                                 geographic_region=geographic_policy.region('gfs_v17', lat, lon))
                ev['title'] += ': ' + {'mslp': 'MSLP', 'h500': '500-hPa height',
                                       't850': '850-hPa temperature'}[field]
                results.append(ev)
            count += 1
    coverage['gfs_difference'] = {
        'status': 'scanned' if count else 'no_matching_field_hours',
        'field_hours': count, 'detections': len(results),
        '850hpa_surface_masked_field_hours': masked_850_field_hours,
        '850hpa_skipped_missing_surface_pressure': skipped_850_missing_psfc,
        '850hpa_surface_pressure_min_pa': 85000.0,
        'common_variables': sorted(set(newest['_vars']).intersection(prior['_vars'])),
        'supported_comparison_fields': [f for f, _, _, _ in definitions
                                        if f in newest['_vars'] and f in prior['_vars']],
        'note': ('No same-grid, same-initialization, selected-hour comparison was available'
                 if not count else 'Difference field-hours were evaluated; 850-hPa temperature uses both models surface-pressure masks'),
    }
    return results


def summarize_events(candidates, max_events, max_per_category, hours_apart=18, km_apart=250):
    """Discard spatial/time duplicate highlights; keep stronger detections."""
    final = []
    categories = Counter()
    # Stable tie-breaking for repeat runs on unchanged datasets.
    candidates = sorted(candidates, key=lambda e: (-e['relative_intensity'], e['id']))
    for ev in candidates:
        if categories[ev['category']] >= max_per_category:
            continue
        duplicate = False
        for other in final:
            if other['category'] != ev['category']:
                continue
            if ev['category'] == 'model_disagreement' and other['measurement']['field'] != ev['measurement']['field']:
                continue
            if abs((parse_utc(ev['valid_time']) - parse_utc(other['valid_time'])).total_seconds()) > hours_apart * 3600:
                continue
            a = (ev['location']['latitude'], ev['location']['longitude'])
            b = (other['location']['latitude'], other['location']['longitude'])
            if geographic_distance_km(a, b) < km_apart:
                duplicate = True
                break
        if duplicate:
            continue
        categories[ev['category']] += 1
        final.append(ev)
        if len(final) >= max_events:
            break
    return final



def event_family(category):
    if category in ('deep_low', 'strong_wind', 'heavy_precipitation', 'heavy_snowfall'):
        return 'surface_weather'
    if category in ('high_cape', 'strong_updraft'):
        return 'convective_weather'
    return 'model_disagreement'


def group_is_near(a, b):
    """Conservative temporal/spatial linkage, not a physical storm tracker.

    Link a feature across successive times or across models/fields only if
    locations are near enough for one meteorological situation.  Separate
    dissimilar hazards (e.g. convection versus a distant surface cyclone).
    """
    family = event_family(a['category'])
    if family != event_family(b['category']):
        return False
    if family == 'model_disagreement' and a['measurement']['field'] != b['measurement']['field']:
        return False
    hours = abs((parse_utc(a['valid_time']) - parse_utc(b['valid_time'])).total_seconds()) / 3600.0
    maximum_hours = 18.0 if family == 'surface_weather' else 12.0
    if hours > maximum_hours:
        return False
    km = geographic_distance_km(
        (a['location']['latitude'], a['location']['longitude']),
        (b['location']['latitude'], b['location']['longitude']))
    if family == 'surface_weather':
        return km <= (360.0 + hours * 16.0)
    return km <= (220.0 + hours * 9.0)


def candidate_groups(candidates, max_scenes=12):
    """Associate related detections without unrestricted transitive chaining.

    A feature can join a group only when it is near an existing member AND
    the entire resulting group stays within bounded time/space limits.  This
    is an experimental proximity grouping, not physical storm tracking.
    """
    candidates = sorted(candidates, key=lambda e: (-e['relative_intensity'], e['id']))
    grouped = []
    locations = [(e['location']['latitude'], e['location']['longitude']) for e in candidates]
    times = [parse_utc(e['valid_time']) for e in candidates]

    def compatible(i, members):
        ev, anchor = candidates[i], candidates[members[0]]
        family = event_family(anchor['category'])
        if event_family(ev['category']) != family:
            return None
        if family == 'model_disagreement' and ev['measurement']['field'] != anchor['measurement']['field']:
            return None
        # Caps prevent chains of individually-close features from joining
        # unrelated systems hundreds or thousands of kilometres apart.
        max_span_hours, max_diameter_km, anchor_distance_km = {
            'surface_weather': (36.0, 900.0, 650.0),
            'convective_weather': (18.0, 480.0, 340.0),
            'model_disagreement': (18.0, 450.0, 320.0),
        }[family]
        if geographic_distance_km(locations[i], locations[members[0]]) > anchor_distance_km:
            return None
        all_times = [times[j] for j in members] + [times[i]]
        if (max(all_times) - min(all_times)).total_seconds() > max_span_hours * 3600.0:
            return None
        nearest_km = float('inf')
        nearby = False
        for j in members:
            km = geographic_distance_km(locations[i], locations[j])
            if km > max_diameter_km:
                return None
            nearest_km = min(nearest_km, km)
            if group_is_near(ev, candidates[j]):
                nearby = True
        return nearest_km if nearby else None

    for i, ev in enumerate(candidates):
        options = []
        for group_index, members in enumerate(grouped):
            score = compatible(i, members)
            if score is not None:
                options.append((score, group_index))
        if options:
            _, group_index = min(options)
            grouped[group_index].append(i)
        else:
            grouped.append([i])

    results = []
    for indices in grouped:
        members = [candidates[i] for i in indices]
        ranked = sorted(members, key=lambda e: (-e['relative_intensity'], e['id']))
        primary = ranked[0]
        categories = sorted(set(x['category'] for x in members))
        models = sorted(set(x['model'] for x in members))
        model_sets = sorted(set(x['model_set'] for x in members))
        timestamps = sorted(set(x['valid_time'] for x in members))
        family = event_family(primary['category'])
        if family == 'surface_weather' and 'deep_low' in categories:
            title = 'Surface cyclone and associated weather'
        elif family == 'surface_weather' and primary['category'] == 'heavy_snowfall':
            title = 'Significant snowfall and associated weather'
        elif family == 'surface_weather' and 'heavy_precipitation' in categories:
            # A single isolated snowfall detection must not relabel a mostly
            # rainfall event as primarily winter weather.
            title = ('Heavy precipitation with snowfall' if 'heavy_snowfall' in categories
                     else 'Heavy precipitation and associated weather')
        elif family == 'surface_weather' and 'heavy_snowfall' in categories:
            title = 'Significant snowfall and associated weather'
        elif family == 'surface_weather':
            title = 'Strong surface winds'
        elif family == 'convective_weather':
            title = ('High convective instability' if categories == ['high_cape']
                     else 'Potentially significant convection')
        else:
            title = 'Model forecast disagreement'
        # Show category and model variety BEFORE filling a limited set of scene
        # slots by intensity. This prevents a strong surface cyclone's many
        # low-pressure scenes from hiding wind or precipitation in the browser.
        # Individual native model/field/cycle/lead combinations remain distinct.
        unique = OrderedDict()
        for ev in ranked:
            key = (ev['model'], ev['measurement']['field'],
                   ev['init_time'], ev['forecast_hour'], ev['valid_time'])
            if key not in unique:
                unique[key] = ev
        available = list(unique.values())
        scenes = []
        seen = set()
        covered_categories, covered_models = set(), set()
        def include(ev):
            key = (ev['model'], ev['measurement']['field'],
                   ev['init_time'], ev['forecast_hour'], ev['valid_time'])
            if key in seen or len(scenes) >= max_scenes:
                return False
            scenes.append(ev)
            seen.add(key)
            covered_categories.add(ev['category'])
            covered_models.add(ev['model'])
            return True
        include(primary)
        # First reserve scenes for each distinct hazard category.
        for category in categories:
            if category not in covered_categories:
                for ev in available:
                    if ev['category'] == category and include(ev):
                        break
        # Then include remaining models, choosing the strongest scene from each.
        for model_name in models:
            if model_name not in covered_models:
                for ev in available:
                    if ev['model'] == model_name and include(ev):
                        break
        # Fill the rest with the strongest still-unrepresented forecast scenes.
        for ev in available:
            include(ev)
            if len(scenes) >= max_scenes:
                break
        score = min(3.5, primary['relative_intensity']) + \
                0.18 * (min(4, len(categories)) - 1) + \
                0.16 * (min(4, len(models)) - 1) + \
                0.07 * (min(4, len(timestamps)) - 1)
        group_id = hashlib.sha1((family + '|' + '|'.join(sorted(
            x['id'] for x in members))).encode('utf-8')).hexdigest()[:16]
        max_member_distance = max((geographic_distance_km(
            (a['location']['latitude'], a['location']['longitude']),
            (b['location']['latitude'], b['location']['longitude']))
            for a in members for b in members), default=0.0)
        # Keep the number of omitted potential scenes explicit. The cap is a
        # visualization cap, not a deletion of candidate evidence.
        distinct_scenes = len(set((x['model'], x['measurement']['field'],
                                   x['init_time'], x['forecast_hour'], x['valid_time'])
                                  for x in members))
        review_notes = []
        tropical_cape_only = (family == 'convective_weather' and
                              set(categories) == {'high_cape'} and
                              all(x['location']['latitude'] < 18.0 for x in members))
        if tropical_cape_only:
            review_notes.append('High CAPE at tropical latitudes alone does not demonstrate a severe-weather threat; review geographic and environmental context.')
        if distinct_scenes > len(scenes):
            review_notes.append('Only a subset of available model/field/time scenes is shown; full candidate count is retained.')
        results.append({
            'id': group_id, 'title': title, 'family': family,
            'location': dict(primary['location']),
            'start_time': timestamps[0], 'end_time': timestamps[-1],
            'primary_model': primary['model'], 'primary_category': primary['category'],
            'models': models, 'model_sets': model_sets, 'categories': categories,
            'peak_relative_intensity': primary['relative_intensity'],
            'group_score': round(score, 3),
            'candidate_count': len(members), 'scenes': scenes,
            'distinct_scene_count': distinct_scenes,
            'omitted_scene_count': max(0, distinct_scenes - len(scenes)),
            'displayed_scene_category_counts': dict(Counter(x['category'] for x in scenes)),
            'displayed_scene_model_counts': dict(Counter(x['model'] for x in scenes)),
            'tropical_cape_only': tropical_cape_only,
            'candidate_category_counts': dict(Counter(x['category'] for x in members)),
            'candidate_model_counts': dict(Counter(x['model'] for x in members)),
            'review_notes': review_notes,
            'group_max_distance_km': round(max_member_distance, 1),
            'grouping_note': 'Bounded spatial-temporal proximity; not verified storm tracking',
            'representative': primary,
        })
    return sorted(results, key=lambda g: (-g['group_score'], g['id']))


def headline_overlap_penalty(group, already_selected):
    """Discourage repetitive nearby highlights without deleting candidates.

    A second detection of the same principal hazard in approximately the same
    region/time period can still be selected if its intensity is compelling.
    Not storm tracking: this is ONLY a top-list diversity adjustment. The
    complete candidates and group summaries remain in the output.
    """
    a = (group['location']['latitude'], group['location']['longitude'])
    a_start, a_end = parse_utc(group['start_time']), parse_utc(group['end_time'])
    same_category_nearby = 0
    for other in already_selected:
        if group['primary_category'] != other['primary_category']:
            continue
        b = (other['location']['latitude'], other['location']['longitude'])
        km = geographic_distance_km(a, b)
        if km > 450.0:
            continue
        b_start, b_end = parse_utc(other['start_time']), parse_utc(other['end_time'])
        # Closest gap between the two time intervals, allowing <=12h drift.
        gap_hours = max(0.0, (a_start-b_end).total_seconds()/3600.0,
                        (b_start-a_end).total_seconds()/3600.0)
        if gap_hours <= 12.0:
            same_category_nearby += 1
    return min(0.90, 0.55 * same_category_nearby)


def select_groups(groups, max_events, max_per_category, max_tropical_cape=1):
    """Prefer strong, distinct situations and avoid one model dominating.

    Category caps and diversity penalties are soft: the scanner never invents
    events to meet a quota; only real threshold-qualified groups are selected.
    """
    selected = []
    remaining = list(groups)
    count_model = Counter()
    count_category = Counter()
    count_family = Counter()
    tropical_cape_selected = 0
    while remaining and len(selected) < max_events:
        feasible = [g for g in remaining
                    if count_category[g['primary_category']] < max_per_category
                    and (not g.get('tropical_cape_only') or
                         tropical_cape_selected < max_tropical_cape)]
        if not feasible:
            break
        def benefit(g):
            return (g['group_score']
                    - 0.38 * count_model[g['primary_model']]
                    - 0.25 * count_category[g['primary_category']]
                    - 0.10 * count_family[g['family']]
                    - headline_overlap_penalty(g, selected))
        chosen = min(feasible, key=lambda g: (-benefit(g), g['id']))
        selected.append(chosen)
        remaining.remove(chosen)
        count_model[chosen['primary_model']] += 1
        count_category[chosen['primary_category']] += 1
        count_family[chosen['family']] += 1
        if chosen.get('tropical_cape_only'):
            tropical_cape_selected += 1
    return selected

def safe_write_json(path, data):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # A completed previous preview is never replaced with a partial write.
    fd, tempname = tempfile.mkstemp(prefix='.event-discovery-', suffix='.tmp', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2, sort_keys=True, allow_nan=False)
            f.write('\n')
        os.replace(tempname, str(target))
    finally:
        if os.path.exists(tempname):
            os.unlink(tempname)


def read_thresholds(path):
    thresholds = dict(DEFAULT_THRESHOLDS)
    if path is not None:
        with Path(path).open('r') as f:
            user = json.load(f)
        if not isinstance(user, dict):
            raise ValueError('thresholds file must be a JSON object')
        for key, value in user.items():
            if key not in thresholds or not isinstance(value, (float, int)) or isinstance(value, bool):
                raise ValueError('unknown or nonnumeric threshold: ' + key)
            if not math.isfinite(value) or (value <= 0 and key != 'low_mslp_hpa'):
                raise ValueError('invalid threshold: ' + key)
            thresholds[key] = float(value)
    if not 850 <= thresholds['low_mslp_hpa'] <= 1050:
        raise ValueError('low_mslp_hpa must be 850..1050 hPa')
    return thresholds


def run(args):
    source = LocalCatalogSource(args.data_root, args.max_chunk_mb)
    if not source.root.is_dir():
        raise ValueError('data root does not exist: ' + str(source.root))
    thresholds = read_thresholds(args.thresholds)
    geographic_policy = load_geographic_policy(args.geographic_regions)
    requested = [x.strip() for x in args.models.split(',') if x.strip()]
    unsupported = set(requested).difference(DEFAULT_MODELS)
    if unsupported:
        raise ValueError('unsupported model(s): ' + ','.join(sorted(unsupported)))
    seen = {}
    full_catalogs = {}
    coverage = {}
    detections = []
    for model in requested:
        catalogs = source.load_catalog(model)
        full_catalogs[model] = catalogs
        # A model may publish more than one resolution store for a cycle
        # (GEFS Mean 0.25-degree surface + 0.5-degree pressure grids).
        # The limit counts initialization cycles, not catalog entries.
        newest_inits = sorted(set(d['_init_dt'] for d in catalogs), reverse=True)[:args.recent_cycles]
        selected = [d for d in catalogs if d['_init_dt'] in newest_inits]
        if not selected:
            coverage[model] = {'datasets': 0, 'forecast_hours': 0, 'detections': 0,
                               'status': 'missing_catalog_or_data'}
            continue
        seen[model] = selected
        for ds in selected:
            detections.extend(scan_dataset(source, ds, args, thresholds, coverage, geographic_policy))
        coverage[model]['status'] = ('scanned' if coverage[model]['forecast_hours']
                                      else 'no_eligible_forecast_hours')
    if 'gfs_v16' in requested and 'gfs_v17' in requested:
        # Compare the most recent COMMON cycle, not the independently selected
        # newest cycle of each model (the two publish on different schedules).
        v16 = full_catalogs.get('gfs_v16', [])
        v17 = full_catalogs.get('gfs_v17', [])
        common = sorted(set(ds['_init_dt'] for ds in v16).intersection(
            ds['_init_dt'] for ds in v17), reverse=True)
        newest_time = max([ds['_init_dt'] for ds in v16 + v17], default=None)
        valid_common = [init for init in common if newest_time is not None and
                        (newest_time - init).total_seconds() <=
                        args.comparison_lookback_hours * 3600.0]
        common_metadata = {
            'newest_gfs_v16_cycle': iso_utc(max((ds['_init_dt'] for ds in v16), default=None)) if v16 else None,
            'newest_gfs_v17_cycle': iso_utc(max((ds['_init_dt'] for ds in v17), default=None)) if v17 else None,
            'latest_common_cycle': iso_utc(common[0]) if common else None,
            'comparison_lookback_hours': args.comparison_lookback_hours,
            'common_cycle_count': len(common),
        }
        if not valid_common:
            common_metadata['status'] = 'skipped_no_matching_cycles'
            common_metadata['note'] = ('No common catalog cycle found' if not common else
                                       'Common cycles exist but are older than the bounded lookback')
            coverage['gfs_difference'] = common_metadata
        else:
            comparisons = 0
            total_field_hours = 0
            total_detections = 0
            total_850_masked_hours = 0
            total_850_skipped_hours = 0
            for init in valid_common[:args.recent_cycles]:
                left = [ds for ds in v17 if ds['_init_dt'] == init]
                right = [ds for ds in v16 if ds['_init_dt'] == init]
                # If multiple grid stores exist, choose the same-grid pair with
                # the greatest number of mutually available supported hours.
                pairs = []
                for newer in left:
                    for older in right:
                        if not grids_match(newer['grid'], older['grid']):
                            continue
                        fields = ('mslp', 'h500', 't850')
                        available = sum(len(set(newer['_vars'].get(f, {}).get('available_hours', []))
                                           .intersection(older['_vars'].get(f, {}).get('available_hours', [])))
                                        for f in fields)
                        pairs.append((available, newer['id'], older['id'], newer, older))
                if not pairs:
                    continue
                chosen = sorted(pairs, key=lambda x: (-x[0], x[1], x[2]))[0]
                additions = compare_gfs(source, chosen[3], chosen[4], args,
                                        thresholds, coverage, geographic_policy)
                detections.extend(additions)
                total_detections += len(additions)
                total_field_hours += coverage['gfs_difference'].get('field_hours', 0)
                total_850_masked_hours += coverage['gfs_difference'].get('850hpa_surface_masked_field_hours', 0)
                total_850_skipped_hours += coverage['gfs_difference'].get('850hpa_skipped_missing_surface_pressure', 0)
                comparisons += 1
            status = ('scanned' if total_field_hours else
                      ('no_matching_field_hours' if comparisons else 'skipped_mismatched_grids'))
            diagnostic = dict(coverage.get('gfs_difference', {}))
            diagnostic.update(common_metadata)
            diagnostic.update({'status': status, 'compared_cycles': comparisons,
                               'field_hours': total_field_hours,
                               '850hpa_surface_masked_field_hours': total_850_masked_hours,
                               '850hpa_skipped_missing_surface_pressure': total_850_skipped_hours,
                               'detections': total_detections,
                               'comparison_cycles': [iso_utc(x) for x in valid_common[:args.recent_cycles]]})
            if not comparisons:
                diagnostic['note'] = 'Common cycles were found but no native-grid pair matched'
            coverage['gfs_difference'] = diagnostic
    groups = candidate_groups(detections)
    selected_groups = select_groups(groups, args.max_events, args.max_per_category,
                                    args.max_tropical_cape_highlights)
    # Summarize ALL potential groups, not only the ones selected for the
    # headline feed. This lets the operator audit selection quality without
    # copying every full raw detection into the JSON preview.
    selection_ranks = {g['id']: i + 1 for i, g in enumerate(selected_groups)}
    selected_tropical = sum(1 for g in selected_groups if g.get('tropical_cape_only'))
    selected_category_counts = Counter(g['primary_category'] for g in selected_groups)
    group_summaries = []
    for g in groups:
        summary = {k: v for k, v in g.items()
                   if k not in ('representative', 'scenes')}
        summary['selected'] = g['id'] in selection_ranks
        summary['selection_rank'] = selection_ranks.get(g['id'])
        if summary['selected']:
            summary['selection_note'] = 'selected'
        elif (g.get('tropical_cape_only') and
              selected_tropical >= args.max_tropical_cape_highlights):
            summary['selection_note'] = 'tropical_cape_highlight_limit'
        elif selected_category_counts[g['primary_category']] >= args.max_per_category:
            summary['selection_note'] = 'category_highlight_limit'
        else:
            summary['selection_note'] = 'not_in_top_selected_events'
        group_summaries.append(summary)
    events = []
    for g in selected_groups:
        ev = dict(g['representative'])
        ev['event_group_id'] = g['id']
        ev['related_categories'] = g['categories']
        ev['related_models'] = g['models']
        ev['related_detection_count'] = g['candidate_count']
        events.append(ev)
    candidate_by_model = Counter(e['model'] for e in detections)
    candidate_by_category = Counter(e['category'] for e in detections)
    selected_by_model = Counter(e['model'] for e in events)
    selected_by_category = Counter(e['category'] for e in events)
    report = {
        'schema': SCHEMA, 'generated_utc': iso_utc(utcnow()),
        'mode': 'read_only_experimental_preview',
        'automatically_published': False, 'cases_preserved': False,
        'review_required': True,
        'methodology': {
            'type': 'objective_threshold_screening_not_climatological_extremes',
            'sample_km': args.sample_km, 'hour_step': args.hour_step,
            'max_forecast_hour': args.max_hour, 'recent_cycles_per_model': args.recent_cycles,
            'multiple_grids_per_cycle': 'all cataloged grid stores for each selected initialization',
            'precipitation_and_snowfall': 'fixed 6-hour difference of cumulative fields; requires both complete hours',
            'validation': 'candidate peak refined against actual native grid cells; no analysis verification yet',
            'thresholds': thresholds,
            'geographic_profiles': geographic_policy.profiles,
            'geographic_policy': 'model-group mask intersected with the native published grid and finite field cells',
            'grouping': 'bounded-diameter spatial-temporal proximity, not verified storm tracks',
            'gfs_comparison': 'latest common initialization within lookback, same-grid same-hour fields',
            'ranking': 'objective thresholds with soft model/category diversity and nearby same-category headline penalty; tropical CAPE-only headline quota; all group summaries retained for auditing',
            'scene_selection': 'representative, then one scene per category and per model where capacity allows, then highest intensity',
            'max_tropical_cape_highlights': args.max_tropical_cape_highlights,
        },
        'counts': {'candidate_detections': len(detections), 'candidate_event_groups': len(groups),
                   'selected_events': len(events), 'selected_event_groups': len(selected_groups),
                   'zarr_chunks_read': source.chunks_read,
                   'compressed_bytes_read': source.bytes_read},
        'candidate_breakdown': {'by_model': dict(candidate_by_model),
                                'by_category': dict(candidate_by_category)},
        'selected_breakdown': {'by_model': dict(selected_by_model),
                               'by_category': dict(selected_by_category)},
        'coverage': coverage, 'skipped_data': dict(source.errors),
        'events': events,
        'event_groups': [{k: v for k, v in g.items() if k != 'representative'}
                         for g in selected_groups],
        'candidate_group_summaries': group_summaries,
    }
    safe_write_json(args.output, report)
    print('EVU event discovery (EXPERIMENTAL; read-only)')
    print('Output: %s' % args.output)
    print('Candidates: %d; grouped situations: %d; selected: %d; Zarr chunks read: %d (%.1f MiB compressed)' %
          (len(detections), len(groups), len(events), source.chunks_read,
           source.bytes_read / (1024.0 * 1024.0)))
    for ev in events:
        print('  %-22s %-13s F%03d %7.2f %-7s %6.2f,%7.2f %s' %
              (ev['category'], ev['model'], ev['forecast_hour'],
               ev['measurement']['value'], ev['measurement']['units'],
               ev['location']['latitude'], ev['location']['longitude'], ev['valid_time']))
    for model, details in sorted(coverage.items()):
        if details.get('status') in ('no_eligible_forecast_hours',
                                     'no_matching_field_hours',
                                     'skipped_no_matching_cycles',
                                     'skipped_mismatched_grids',
                                     'missing_catalog_or_data'):
            print('Coverage warning: %s: %s (%s)' %
                  (model, details['status'], details.get('data_coverage_note',
                                                       details.get('note', 'review catalog/field availability'))))
    if source.errors:
        print('Skipped inputs: ' + ', '.join('%s=%d' % x for x in sorted(source.errors.items())))
    if not events:
        print('No events met the prototype thresholds. Review coverage and available fields in the JSON.')
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('--data-root', default='./zarr', help='Directory containing catalog_MODEL.json and model Zarr stores')
    p.add_argument('--output', default='./event_discovery_preview.json', help='Local JSON preview output (never auto-published)')
    p.add_argument('--models', default=','.join(DEFAULT_MODELS), help='Comma-separated model IDs to inspect')
    p.add_argument('--recent-cycles', type=int, default=1, help='Number of newest cycles per model (each scanned separately)')
    p.add_argument('--max-hour', type=int, default=72, help='Maximum forecast hour considered')
    p.add_argument('--hour-step', type=int, default=6, help='Forecast hours screened at this cadence')
    p.add_argument('--max-frames-per-model', type=int, default=12, help='Hard cap per model cycle')
    p.add_argument('--sample-km', type=float, default=75.0, help='Approximate first-pass scan spacing; peak refined on native grid')
    p.add_argument('--max-chunk-mb', type=int, default=128, help='Memory guard for single decompressed chunk')
    p.add_argument('--max-events', type=int, default=15, help='Maximum number of preview events')
    p.add_argument('--comparison-lookback-hours', type=int, default=72,
                   help='Maximum age of a common GFS v16/v17 initialization cycle')
    p.add_argument('--max-per-category', type=int, default=6, help='Limit events per category after deduplication')
    p.add_argument('--max-tropical-cape-highlights', type=int, default=1,
                   help='Max high-CAPE-only events entirely south of 18N in selected highlights (0 disables; all candidates retained)')
    p.add_argument('--thresholds', help='Optional JSON file overriding recognized thresholds (see example)')
    p.add_argument('--geographic-regions', help='Optional JSON override of regional, GFS and AI geographic masks')
    a = p.parse_args(argv)
    for label in ('recent_cycles', 'max_hour', 'hour_step', 'max_frames_per_model',
                  'max_chunk_mb', 'max_events', 'max_per_category',
                  'comparison_lookback_hours'):
        if getattr(a, label) < 1:
            p.error('--%s must be positive' % label.replace('_', '-'))
    if a.max_tropical_cape_highlights < 0:
        p.error('--max-tropical-cape-highlights must not be negative')
    if not 5.0 <= a.sample_km <= 500.0:
        p.error('--sample-km must be between 5 and 500')
    try:
        run(a)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        p.error(str(exc))


if __name__ == '__main__':
    main()
