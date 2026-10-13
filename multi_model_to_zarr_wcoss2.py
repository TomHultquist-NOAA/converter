#!/usr/bin/env python3
"""Multi-source RRFS/REFS/HRRR GRIB2 -> browser-compatible Zarr v2.

WCOSS2: Python standard library + wgrib2. Works with the existing RRFS
converter's tested Zarr/GRIB routines; no pip, numpy, xarray or conda.

Each (model, forecast hour) is an independent process. Only the coordinator
updates the processed array, manifests, catalogs and publishes. Existing RRFS
output and its original catalog.json are not modified unless the RRFS store
itself is explicitly selected for additional hours.
"""
from __future__ import annotations

import argparse
from array import array
from concurrent.futures import ProcessPoolExecutor, as_completed
import datetime as dt
import json
import logging
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.error
import zlib

# Keep each wgrib2 subprocess single-threaded. WCOSS2 may allow up to 20
# Python hour-workers, but many simultaneous OpenMP thread pools exhaust
# the node's process/thread quota (OMP Error #34, SIGABRT).
# Apply before loading the support module or starting the worker pool so
# direct Python invocations and the cron wrapper behave identically.
for _thread_var in ('OMP_NUM_THREADS', 'OMP_THREAD_LIMIT',
                    'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS',
                    'NUMEXPR_NUM_THREADS'):
    os.environ[_thread_var] = '1'



# ---------------------------------------------------------------------------
# RRFS GRIB2/Zarr helpers (bundled from the established RRFS converter)
# ---------------------------------------------------------------------------
# Helpers use a private scope to keep grid/field constants independent of
# the multi-model names below. No external Python helper file is required.
from types import SimpleNamespace

def _bundled_rrfs_support():
    from array import array
    import datetime as dt
    import json
    import logging
    import math
    import os
    from pathlib import Path
    import re
    import shlex
    import shutil
    import ssl
    import subprocess
    import sys
    import tarfile
    import tempfile
    import time
    import urllib.error
    import urllib.request
    import zlib

    NX = 1799



    NY = 1059



    NPTS = NX * NY



    FLOAT_BYTES = 4



    FIELD_BYTES = NPTS * FLOAT_BYTES



    GRID_ATTRS = {

        "GRIB_gridType": "lambert",

        "GRIB_Nx": NX,

        "GRIB_Ny": NY,

        "GRIB_DxInMetres": 3000.0,

        "GRIB_DyInMetres": 3000.0,

        "GRIB_LoVInDegrees": -97.5,

        "GRIB_LaDInDegrees": 38.5,

        "GRIB_Latin1InDegrees": 38.5,

        "GRIB_Latin2InDegrees": 38.5,

        "GRIB_longitudeOfFirstGridPointInDegrees": -122.719528,

        "GRIB_latitudeOfFirstGridPointInDegrees": 21.138123,

    }



    FIELDS = [

        # output, product, short name, inventory phrase, units, long name

        ("refc",   "2dfld",  "REFC",   "entire atmosphere", "dB",       "Composite reflectivity"),

        ("csnow",  "2dfld",  "CSNOW",  "surface",           "1",        "Categorical snow"),

        ("cicep",  "2dfld",  "CICEP",  "surface",           "1",        "Categorical ice pellets"),

        ("cfrzr",  "2dfld",  "CFRZR",  "surface",           "1",        "Categorical freezing rain"),

        ("crain",  "2dfld",  "CRAIN",  "surface",           "1",        "Categorical rain"),

        ("t2m",    "2dfld",  "TMP",    "2 m above ground",  "K",        "2-m temperature"),

        ("dpt2m",  "2dfld",  "DPT",    "2 m above ground",  "K",        "2-m dew point temperature"),

        ("rh2m",   "2dfld",  "RH",     "2 m above ground",  "%",        "2-m relative humidity"),

        ("prate",  "2dfld",  "PRATE",  "surface",           "kg m-2 s-1","Precipitation rate"),

        ("apcp",   "2dfld",  "APCP",   "surface",           "kg m-2",   "Total precipitation"),

        ("frozr",  "2dfld",  "FROZR",  "surface",           "kg m-2",   "Frozen rain"),

        ("frzr",   "2dfld",  "FRZR",   "surface",           "kg m-2",   "Freezing rain"),

        ("tsnowp", "2dfld",  "TSNOWP", "surface",           "kg m-2",   "Total snow precipitation"),

        ("asnow",  "2dfld",  "ASNOW",  "surface",           "m",        "Total snowfall"),

        ("cpofp",  "2dfld",  "CPOFP",  "surface",           "%",        "Percent frozen precipitation"),

        ("snowc",  "2dfld",  "SNOWC",  "surface",           "%",        "Snow cover"),

        ("sden",   "2dfld",  "SDEN",   "surface",           "kg m-3",   "Snow density"),

        ("weasd",  "2dfld",  "WEASD",  "surface",           "kg m-2",   "Water equivalent of accumulated snow depth"),

        ("snod",   "2dfld",  "SNOD",   "surface",           "m",        "Snow depth"),

        ("t925",   "prslev", "TMP",    "925 mb",            "K",        "925-hPa temperature"),

        ("t850",   "prslev", "TMP",    "850 mb",            "K",        "850-hPa temperature"),

        ("t700",   "prslev", "TMP",    "700 mb",            "K",        "700-hPa temperature"),

    ]



    ACCUM_FIELDS = {"apcp", "frozr", "frzr", "tsnowp", "asnow", "weasd"}



    PTYPE_INPUTS = ("crain", "csnow", "cfrzr", "cicep")



    class HTTPClient:

        def __init__(self, timeout=120, retries=5, insecure=False):

            self.timeout = timeout

            self.retries = retries

            self.context = ssl._create_unverified_context() if insecure else ssl.create_default_context()

            self.user_agent = "rrfs-to-zarr-wcoss2/1.0"



        def _request(self, req):

            last = None

            for attempt in range(self.retries + 1):

                try:

                    return urllib.request.urlopen(req, timeout=self.timeout, context=self.context)

                except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:

                    last = exc

                    if isinstance(exc, urllib.error.HTTPError) and exc.code in (400, 401, 403, 404):

                        raise

                    if attempt >= self.retries:

                        raise

                    time.sleep(min(2 ** attempt, 30))

            raise last



        def text(self, url):

            req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})

            with self._request(req) as r:

                return r.read().decode("utf-8")



        def size(self, url):

            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": self.user_agent})

            try:

                with self._request(req) as r:

                    size = r.headers.get("Content-Length")

                    if size is not None:

                        return int(size)

            except Exception:

                pass



            req = urllib.request.Request(

                url,

                headers={"User-Agent": self.user_agent, "Range": "bytes=0-0"},

            )

            with self._request(req) as r:

                cr = r.headers.get("Content-Range", "")

                m = re.search(r"/(\d+)$", cr)

                if m:

                    return int(m.group(1))

                size = r.headers.get("Content-Length")

                if size is not None and getattr(r, "status", None) == 200:

                    return int(size)

            raise RuntimeError("Could not determine object size: " + url)



        def range_bytes(self, url, start, end):

            expected = end - start + 1

            req = urllib.request.Request(

                url,

                headers={

                    "User-Agent": self.user_agent,

                    "Range": f"bytes={start}-{end}",

                },

            )

            with self._request(req) as r:

                data = r.read()

                status = getattr(r, "status", 200)



            if status == 200 and len(data) != expected:

                raise RuntimeError(

                    f"Server ignored byte range for {url}; expected {expected} bytes, got {len(data)}"

                )

            if len(data) != expected:

                raise RuntimeError(

                    f"Short range response for {url}; expected {expected} bytes, got {len(data)}"

                )

            return data



    def parse_hours(text):

        out = set()

        for piece in text.split(","):

            piece = piece.strip()

            if not piece:

                continue

            if "-" in piece:

                a, b = piece.split("-", 1)

                a, b = int(a), int(b)

                if b < a:

                    raise ValueError("Bad hour range: " + piece)

                out.update(range(a, b + 1))

            else:

                out.add(int(piece))

        vals = sorted(out)

        if any(h < 0 or h > 84 for h in vals):

            raise ValueError("RRFS forecast hours must be between 0 and 84.")

        return vals



    def parse_idx(text, total_size):

        rows = []

        for raw in text.splitlines():

            raw = raw.strip()

            if not raw:

                continue

            p = raw.split(":")

            if len(p) < 5:

                continue

            try:

                msg = int(p[0])

                off = int(p[1])

            except ValueError:

                continue

            rows.append({

                "msg": msg,

                "offset": off,

                "short_name": p[3].strip(),

                "text": raw,

            })



        if not rows:

            raise RuntimeError("No inventory records parsed.")



        for i, row in enumerate(rows):

            row["end"] = (rows[i + 1]["offset"] - 1) if i + 1 < len(rows) else (total_size - 1)

        return rows



    ACC_RE = re.compile(r"(\d+)\s*-\s*(\d+)\s*(?:hour|hr)\s+acc", re.I)



    def choose_inventory(rows, output_name, short_name, level_phrase, fhr):

        cands = [

            r for r in rows

            if r["short_name"].upper() == short_name.upper()

            and level_phrase.lower() in r["text"].lower()

        ]

        if not cands:

            return None

        if len(cands) == 1:

            return cands[0]



        if output_name in ACCUM_FIELDS:

            ranked = []

            for r in cands:

                m = ACC_RE.search(r["text"])

                if m:

                    start, end = int(m.group(1)), int(m.group(2))

                    score = (

                        int(end == fhr),

                        int(start == 0),

                        end - start,

                        -r["msg"],

                    )

                else:

                    score = (-1, -1, -1, -r["msg"])

                ranked.append((score, r))

            ranked.sort(key=lambda x: x[0], reverse=True)

            return ranked[0][1]



        # Prefer instantaneous-looking records over accumulation/average variants.

        def score(r):

            t = r["text"].lower()

            penalty = int(" acc" in t) + int(" ave" in t) + int(" avg" in t)

            return penalty, r["msg"]



        return sorted(cands, key=score)[0]



    def group_consecutive(present):

        if not present:

            return []

        groups = [[present[0]]]

        for item in present[1:]:

            if item["record"]["msg"] == groups[-1][-1]["record"]["msg"] + 1:

                groups[-1].append(item)

            else:

                groups.append([item])

        return groups



    def download_partial(client, grib_url, present, output_path):

        groups = group_consecutive(present)

        with output_path.open("wb") as f:

            for group in groups:

                start = group[0]["record"]["offset"]

                end = group[-1]["record"]["end"]

                logging.debug("Range %d-%d from %s", start, end, grib_url)

                f.write(client.range_bytes(grib_url, start, end))



    def find_wgrib2(explicit=None):

        if explicit:

            p = Path(explicit)

            if p.exists() and os.access(str(p), os.X_OK):

                return str(p)

            raise RuntimeError("--wgrib2 is not executable: " + explicit)

        found = shutil.which("wgrib2")

        if not found:

            raise RuntimeError(

                "wgrib2 was not found in PATH. On WCOSS2, load/use the environment "

                "that provides wgrib2, or pass --wgrib2 /full/path/to/wgrib2."

            )

        return found



    def run(cmd, capture=False):

        logging.debug("RUN: %s", " ".join(str(x) for x in cmd))

        return subprocess.run(

            [str(x) for x in cmd],

            check=True,

            text=True,

            stdout=subprocess.PIPE if capture else None,

            stderr=subprocess.STDOUT if capture else None,

        )



    def decode_to_ieee(wgrib2, grib_path, binary_path):

        if binary_path.exists():

            binary_path.unlink()

        cmd = [

            wgrib2,

            str(grib_path),

            "-no_header",

            "-order", "we:sn",

            "-little_endian",

            "-ieee", str(binary_path),

        ]

        run(cmd)



    def grid_description(wgrib2, grib_path):

        try:

            p = run([wgrib2, str(grib_path), "-d", "1", "-V"], capture=True)

            return p.stdout

        except Exception as exc:

            return f"Could not obtain wgrib2 -V grid description: {exc}"



    def json_write(path, obj):

        path.parent.mkdir(parents=True, exist_ok=True)

        tmp = path.with_name(path.name + ".tmp")

        with tmp.open("w", encoding="utf-8") as f:

            json.dump(obj, f, indent=2, sort_keys=True, allow_nan=False)

        os.replace(tmp, path)



    def zarr_array_metadata(shape, chunks, dtype, fill_value, level):

        return {

            "zarr_format": 2,

            "shape": list(shape),

            "chunks": list(chunks),

            "dtype": dtype,

            "compressor": {"id": "zlib", "level": level},

            "fill_value": fill_value,

            "order": "C",

            "filters": None,

            "dimension_separator": ".",

        }



    def ensure_array(root, name, shape, chunks, dtype, fill_value, attrs, level):

        d = root / name

        d.mkdir(parents=True, exist_ok=True)

        za = d / ".zarray"

        zz = d / ".zattrs"

        if not za.exists():

            json_write(za, zarr_array_metadata(shape, chunks, dtype, fill_value, level))

        if not zz.exists():

            json_write(zz, attrs)



    def write_compressed_chunk(path, raw, level):

        path.parent.mkdir(parents=True, exist_ok=True)

        tmp = path.with_name(path.name + ".tmp")

        compressed = zlib.compress(raw, level)

        with tmp.open("wb") as f:

            f.write(compressed)

        os.replace(tmp, path)



    def pack_int64_le(values):

        a = array("q", values)

        if sys.byteorder != "little":

            a.byteswap()

        return a.tobytes()



    def pack_int16_le(values):

        a = array("h", values)

        if sys.byteorder != "little":

            a.byteswap()

        return a.tobytes()



    def derive_ptype(cat_raw):

        """

        cat_raw values are little-endian float32 full-grid byte strings.



        Returns uint8 bytes:

          0 none/unknown

          1 rain

          2 snow

          3 freezing rain

          4 ice pellets

          5 mixed

        """

        decoded = {}

        for name in PTYPE_INPUTS:

            a = array("f")

            a.frombytes(cat_raw[name])

            if sys.byteorder != "little":

                a.byteswap()

            if len(a) != NPTS:

                raise RuntimeError(f"{name}: unexpected point count {len(a)}")

            decoded[name] = a



        rain = decoded["crain"]

        snow = decoded["csnow"]

        frzr = decoded["cfrzr"]

        icep = decoded["cicep"]



        out = bytearray(NPTS)

        for i in range(NPTS):

            r = rain[i] > 0.5

            s = snow[i] > 0.5

            f = frzr[i] > 0.5

            p = icep[i] > 0.5

            n = r + s + f + p

            if n == 0:

                code = 0

            elif n > 1:

                code = 5

            elif r:

                code = 1

            elif s:

                code = 2

            elif f:

                code = 3

            else:

                code = 4

            out[i] = code

        return bytes(out)



    DEFAULT_PUBLISH_HOST = "thultquist@rzdm.ncep.noaa.gov"



    DEFAULT_PUBLISH_DIR = (

        "/home/people/emc/www/htdocs/users/verification_restricted/"

        "test/trh/dynamic_viewer/data"

    )

    _local = locals()
    return SimpleNamespace(**{name: _local[name] for name in (
        'DEFAULT_PUBLISH_DIR',
        'DEFAULT_PUBLISH_HOST',
        'FIELDS',
        'FIELD_BYTES',
        'GRID_ATTRS',
        'HTTPClient',
        'NPTS',
        'NX',
        'NY',
        'PTYPE_INPUTS',
        'choose_inventory',
        'decode_to_ieee',
        'derive_ptype',
        'download_partial',
        'ensure_array',
        'find_wgrib2',
        'grid_description',
        'json_write',
        'pack_int16_le',
        'pack_int64_le',
        'parse_hours',
        'parse_idx',
        'write_compressed_chunk',
    )})

core = _bundled_rrfs_support()

SOURCE_ROOT = 'https://noaa-rrfs-ops-pds.s3.amazonaws.com'
HRRR_ROOT = 'https://noaa-hrrr-bdp-pds.s3.amazonaws.com'
MODELS = ('rrfs', 'refs_mean', 'refs_m001', 'refs_m002', 'refs_m003', 'refs_m004', 'refs_m005', 'hrrr')
PRODUCTS = {
    'rrfs': ('2dfld', 'prslev'),
    'refs_mean': ('mean','pmmn'),
    'refs_member': ('2dfldnomads', 'prslevnomads'),
    'hrrr': ('wrfsfc', 'wrfprs'),
}
# Fields retain the units expected by viewer.js, before any browser conversion.
# Prefer product order above if a field is supplied in two HRRR products.
FIELDS = {n: dict(long_name=long, short=short, level=phrase, units=unit,
                  product=product)
          for n, product, short, phrase, unit, long in core.FIELDS}
FIELDS.update({
    'u10': dict(long_name='10-m U wind component', short='UGRD', level='10 m above ground', units='m s-1', product='2dfld'),
    'v10': dict(long_name='10-m V wind component', short='VGRD', level='10 m above ground', units='m s-1', product='2dfld'),
    'gust': dict(long_name='10-m wind gust', short='GUST', level='surface', units='m s-1', product='2dfld'),
    'wspd10': dict(long_name='10-m wind speed derived from U/V', short='WIND', level='10 m above ground', units='m s-1', product='derived'),
})
# Accumulated fields are processed carefully: nonzero-start interval amounts
# must be added to a prior total before appearing as cumulative in the viewer.
ACCUMULATIONS = {'apcp', 'frozr', 'frzr', 'tsnowp', 'asnow', 'weasd'}
ACC_RANGE = re.compile(r'(?<!\d)(\d+)\s*-\s*(\d+)\s*(?:hour|hr)\s+acc', re.I)
ACC_LENGTH = re.compile(r'(?<!\d)(\d+)\s*(?:hour|hr)\s+acc', re.I)
STORES_RE = re.compile(r'^(rrfs|refs_mean|refs_m00[1-5]|hrrr)_(\d{8})_(00|06|12|18)00\.zarr$')
CHUNK_RE = re.compile(r'^(\d+)\.0\.0$')
NO_FIELDS = {'processed', 'valid_time', 'forecast_hour', 'latitude', 'longitude', 'x', 'y'}


def family(model):
    return 'refs_member' if model.startswith('refs_m') else model


def hour_bounds(model):
    return (1, 60) if model == 'refs_mean' else (0, 48) if model == 'hrrr' else (0, 60) if model.startswith('refs_') else (0, 84)


def file_urls(model, date, cycle, fhr, product):
    if model == 'rrfs':
        prefix = f'{SOURCE_ROOT}/rrfs.{date}/{cycle}/'
        fname = f'rrfs.t{cycle}z.{product}.3km.f{fhr:03d}.conus.grib2'
    elif model == 'refs_mean':
        prefix = f'{SOURCE_ROOT}/refs.{date}/{cycle}/ensprod/'
        fname = f'refs.t{cycle}z.{product}.f{fhr:02d}.conus.grib2'
    elif model.startswith('refs_m'):
        member = model.split('_', 1)[1]
        prefix = f'{SOURCE_ROOT}/rrfsens.{date}/{cycle}/{member}/'
        fname = f'rrfs.t{cycle}z.{member}.{product}.3km.f{fhr:03d}.conus.grib2'
    elif model == 'hrrr':
        prefix = f'{HRRR_ROOT}/hrrr.{date}/conus/'
        fname = f'hrrr.t{cycle}z.{product}f{fhr:02d}.grib2'
    else:
        raise ValueError('Unsupported model: ' + model)
    url = prefix + fname
    return url, url + '.idx'


def parse_acc_interval(text, fhr):
    """Return (start,end), None if not an accumulation, or 'unknown' if ambiguous."""
    m = ACC_RANGE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = ACC_LENGTH.search(text)
    if m:
        width = int(m.group(1))
        return max(0, fhr-width), fhr
    return 'unknown' if re.search(r'\bacc\b', text, re.I) else None


def choose_field(rows, name, fhr):
    info = FIELDS[name]
    # Avoid mistaking wind gust at 10 m for arbitrary GUST levels in GRIB.
    cands = [r for r in rows if r['short_name'].upper() == info['short']
             and info['level'].lower() in r['text'].lower()]
    if not cands:
        return None
    if name in ACCUMULATIONS:
        def rank(r):
            it = parse_acc_interval(r['text'], fhr)
            # 0-F total preferred; else larger interval ending at fhr;
            # never prefer an ambiguous/mismatched interval to a known one.
            if isinstance(it, tuple):
                a, b = it
                return (int(a == 0 and b == fhr), int(b == fhr), b-a, -r['msg'])
            return (0, 0, -1, -r['msg'])
        return max(cands, key=rank)
    return core.choose_inventory(rows, name, info['short'], info['level'], fhr)


def iso_init(date, cycle):
    return dt.datetime.strptime(date + cycle, '%Y%m%d%H').replace(tzinfo=dt.timezone.utc)


def store_name(model, date, cycle):
    return f'{model}_{date}_{cycle}00.zarr'


def init_store(store, model, date, cycle, level):
    """Write all immutable Zarr metadata once, in the main process."""
    store.mkdir(parents=True, exist_ok=True)
    if not (store / '.zgroup').exists():
        core.json_write(store / '.zgroup', {'zarr_format': 2})
    init = iso_init(date, cycle)
    first, last = hour_bounds(model)
    length = last + 1  # index is always the actual integer forecast hour
    label = ('RRFS' if model == 'rrfs' else 'REFS Mean' if model == 'refs_mean'
             else 'HRRR' if model == 'hrrr' else 'REFS ' + model.split('_')[1])
    attrs = {
        'model': label, 'model_id': model, 'domain': 'CONUS',
        'grid_spacing_km': 3,
        'init_time': init.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'source': 'NOAA public S3 operational GRIB2 archives',
        'source_base_url': HRRR_ROOT if model == 'hrrr' else SOURCE_ROOT,
        'dimensions': {'valid_time': length, 'y': core.NY, 'x': core.NX},
        'grid_order': 'WE:SN',
        'viewer_hint': 'autumnplot-gl LambertGrid.fromLLCornerLonLat',
        **core.GRID_ATTRS,
    }
    if not (store / '.zattrs').exists():
        core.json_write(store / '.zattrs', attrs)
    else:
        previous = json.loads((store / '.zattrs').read_text())
        if previous.get('init_time') != attrs['init_time']:
            raise RuntimeError('Existing store initialization mismatch: ' + str(store))
    times = [int((init+dt.timedelta(hours=h)).timestamp()) for h in range(length)]
    coords = [
        ('valid_time', '<i8', core.pack_int64_le(times), 'seconds since 1970-01-01 00:00:00 UTC'),
        ('forecast_hour', '<i2', core.pack_int16_le(range(length)), 'hours'),
    ]
    for name, dtype, raw, units in coords:
        core.ensure_array(store, name, (length,), (length,), dtype, None,
                          {'_ARRAY_DIMENSIONS': ['valid_time'], 'units': units}, level)
        if not (store / name / '0').exists():
            core.write_compressed_chunk(store / name / '0', raw, level)
    core.ensure_array(store, 'processed', (length,), (length,), '|u1', 0,
                      {'_ARRAY_DIMENSIONS': ['valid_time'],
                       'long_name': '1 after forecast-hour processing completed'}, level)
    for name, info in FIELDS.items():
        core.ensure_array(store, name, (length, core.NY, core.NX), (1, core.NY, core.NX),
                          '<f4', 'NaN', {'_ARRAY_DIMENSIONS': ['valid_time','y','x'],
                          'long_name': ('Magnitude of ensemble-mean 10-m wind vector (not mean scalar wind speed)'
                                        if model == 'refs_mean' and name == 'wspd10'
                                        else info['long_name']), 'units': info['units'],
                          'GRIB_shortName': info['short'], 'GRIB_level': info['level'],
                          **({'ensemble_mean_fraction': True,
                              'long_name': 'Weighted ensemble-mean '+info['long_name'].lower()+' (not a deterministic category)'}
                             if model=='refs_mean' and name in core.PTYPE_INPUTS else {})}, level)
    if model != 'refs_mean':
        core.ensure_array(store, 'ptype', (length, core.NY, core.NX),
                          (1, core.NY, core.NX), '|u1', 0,
                          {'_ARRAY_DIMENSIONS': ['valid_time','y','x'],
                           'long_name': 'Derived precipitation type', 'units': '1',
                           'flag_values': [0,1,2,3,4,5]}, level)


def read_processed(store, n):
    p = store / 'processed' / '0'
    out = bytearray(zlib.decompress(p.read_bytes())) if p.exists() else bytearray(n)
    if len(out) != n:
        raise RuntimeError(f'Processed length mismatch for {store}: {len(out)} != {n}')
    return out


def write_processed(store, arr, level):
    core.write_compressed_chunk(store / 'processed' / '0', bytes(arr), level)


def decode_wspd(u_raw, v_raw):
    u, v = array('f'), array('f')
    u.frombytes(u_raw)
    v.frombytes(v_raw)
    if sys.byteorder != 'little':
        u.byteswap(); v.byteswap()
    out = array('f', (math.hypot(a,b) if math.isfinite(a) and math.isfinite(b) and
                      a > -1e9 and b > -1e9 else math.nan for a,b in zip(u,v)))
    if sys.byteorder != 'little':
        out.byteswap()
    return out.tobytes()


def check_grid(wgrib2, path):
    info = core.grid_description(wgrib2, path)
    dims = re.search(r'\(\s*(\d+)\s*x\s*(\d+)\s*\)', info)
    if dims and (int(dims.group(1)), int(dims.group(2))) != (core.NX, core.NY):
        raise RuntimeError(f'Unsupported {dims.group(1)}x{dims.group(2)} GRIB grid; '
                           f'viewer metadata assumes {core.NX}x{core.NY}. '
                           f'Inspect GRIB projection before ingesting.')
    # All stores use the standard HRRR/RRFS 3-km CONUS Lambert metadata.
    # Reject a geolocation mismatch even if NX and NY happen to agree.
    lat1 = re.search(r'\bLat1\s*=?\s*([+-]?\d+(?:\.\d+)?)', info, re.I)
    lon1 = re.search(r'\bLon1\s*=?\s*([+-]?\d+(?:\.\d+)?)', info, re.I)
    if lat1 and lon1:
        lat = float(lat1.group(1))
        lon = (float(lon1.group(1)) + 180) % 360 - 180
        target_lat = float(core.GRID_ATTRS['GRIB_latitudeOfFirstGridPointInDegrees'])
        target_lon = float(core.GRID_ATTRS['GRIB_longitudeOfFirstGridPointInDegrees'])
        if abs(lat-target_lat) > .03 or abs(lon-target_lon) > .03:
            raise RuntimeError(f'Grid first point ({lat:.4f}, {lon:.4f}) differs from '
                               f'assumed RRFS/HRRR reference ({target_lat:.4f}, '
                               f'{target_lon:.4f}). Do not misplace this field.')
    # Decoded byte counts are also checked when writing field chunks.
    # That prevents a silent mismatch if wgrib2 omits projection details.


def process_job(args):
    """Runs in a child process. Writes only hour-unique files; no shared metadata."""
    model, date, cycle, fhr, store_str, work_str, wgrib2, level, timeout, retries, insecure, keep_grib, strict = args[:13]
    repair_fields = tuple(args[13]) if len(args) > 13 and args[13] else None
    repair_kind = args[14] if len(args)>14 else None
    store = Path(store_str)
    # Cycle-scoped temporary GRIB directory: safe to expire by init time.
    work = Path(work_str) / model / f'{date}_{cycle}' / f'f{fhr:03d}'
    work.mkdir(parents=True, exist_ok=True)
    client = core.HTTPClient(timeout=timeout, retries=retries, insecure=insecure)
    inventory = {}
    missing_products = {}
    chosen = {}
    pending = {}
    raw_categories = {}
    wind_raw = {}
    count = 0
    product_info = {}
    try:
        # Remove stale chunks from a previously interrupted attempt at THIS
        # hour only; other workers own other hour numbers.
        for name in (repair_fields if repair_fields is not None else tuple(FIELDS) + ('ptype',)):
            # Do not touch any already present surface/derived chunks in a repair.
            if repair_fields is None:
                (store / name / f'{fhr}.0.0').unlink(missing_ok=True)
                (store / '.accum_intervals' / name / f'{fhr}.zlib').unlink(missing_ok=True)
        for product in ((('prslevnomads',) if repair_kind=='pressure' else PRODUCTS[family(model)]) if repair_fields else PRODUCTS[family(model)]):
            url, idxurl = file_urls(model, date, cycle, fhr, product)
            try:
                # The index is authoritative for which records actually exist.
                # Missing *products* are distinct from missing *fields* and from
                # operational errors (403, 5xx, TLS, timeouts, bad GRIB, etc.).
                index_text = client.text(idxurl)
                rows = core.parse_idx(index_text, client.size(url))
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
                missing_products[product] = 'HTTP 404 (not published / not supplied)'
                continue
            inventory[product] = (url, idxurl, rows)
        # Across all products, select at most one record per logical field.
        # REFS mean categorical values are not deterministic weather types.
        for name in FIELDS:
            if repair_fields is not None and name not in repair_fields:
                continue
            if name == 'wspd10':
                continue
            for product, (_, _, rows) in inventory.items():
                if model=='refs_mean' and name=='refc' and product!='pmmn':
                    continue
                rec = choose_field(rows, name, fhr)
                if rec is not None:
                    interval = parse_acc_interval(rec['text'], fhr) if name in ACCUMULATIONS else None
                    # An explicitly non-zero start needs interval-to-date reconstruction.
                    chosen[name] = (product, rec, interval)
                    break
        if strict and not chosen:
            raise RuntimeError('No requested fields in available GRIB inventories')
        if not chosen:
            # A source with no matching records cannot supply this hour. Do
            # not convert the expected absence into a failed download job.
            # The processed flag stays 0 so cron may retry later.
            reason = ('No available GRIB products' if not inventory else
                      'Available products contain none of the configured fields')
            return {'model': model, 'fhr': fhr, 'status': 'unavailable',
                    'reason': reason, 'missing_products': missing_products,
                    'inventory_products': sorted(inventory), 'fields': [], 'count': 0}
        for product, (url, idxurl, rows) in inventory.items():
            requests = [{'output': n, 'record': rec} for n,(p,rec,_) in chosen.items() if p == product]
            if not requests:
                continue
            requests.sort(key=lambda x: x['record']['msg'])
            partial = work / f'{product}.selected.grib2'
            binary = work / f'{product}.selected.ieee'
            core.download_partial(client, url, requests, partial)
            check_grid(wgrib2, partial)
            core.decode_to_ieee(wgrib2, partial, binary)
            expected = len(requests) * core.FIELD_BYTES
            if binary.stat().st_size != expected:
                raise RuntimeError(f'{model} F{fhr:03d} {product}: decoded byte count '
                                   f'{binary.stat().st_size}, expected {expected}; grid mismatch?')
            with binary.open('rb') as stream:
                for item in requests:
                    name = item['output']
                    raw = stream.read(core.FIELD_BYTES)
                    if len(raw) != core.FIELD_BYTES:
                        raise RuntimeError('Short decoded binary: '+str(binary))
                    _, rec, interval = chosen[name]
                    # Accumulation from nonzero start is NOT yet a model-run total.
                    # Stage it for later cumulative reconstruction by coordinator.
                    if isinstance(interval, tuple) and interval[0] > 0:
                        staged = store / '.accum_intervals' / name / f'{fhr}.zlib'
                        core.write_compressed_chunk(staged, raw, level)
                        pending[name] = [interval[0], interval[1]]
                    elif interval == 'unknown':
                        # Unknown aggregation range: never falsely label as run total.
                        staged = store / '.accum_intervals' / name / f'{fhr}.zlib'
                        core.write_compressed_chunk(staged, raw, level)
                        pending[name] = 'unknown'
                    else:
                        core.write_compressed_chunk(store / name / f'{fhr}.0.0', raw, level)
                    if name in core.PTYPE_INPUTS and model != 'refs_mean':
                        raw_categories[name] = raw
                    if name in ('u10','v10'):
                        wind_raw[name] = raw
                    count += 1
            product_info[product] = {'url':url,'idx_url':idxurl,'selected': len(requests)}
            binary.unlink(missing_ok=True)
            if not keep_grib:
                partial.unlink(missing_ok=True)
        if all(k in raw_categories for k in core.PTYPE_INPUTS):
            core.write_compressed_chunk(store / 'ptype' / f'{fhr}.0.0',
                                        core.derive_ptype(raw_categories), level)
        if all(k in wind_raw for k in ('u10','v10')):
            core.write_compressed_chunk(store / 'wspd10' / f'{fhr}.0.0',
                                        decode_wspd(wind_raw['u10'],wind_raw['v10']), level)
        selections={name:{'product':product,'inventory':rec['text'],
                          'interval':list(interval) if isinstance(interval,tuple) else interval}
                    for name,(product,rec,interval) in chosen.items()}
        return {'model':model,'fhr':fhr,'fields':sorted(chosen),'selections':selections,
                'pending':pending,'products':product_info,'status':'complete',
                'missing_products': missing_products,'count':count,
                'repair_only': bool(repair_fields), 'repair_kind': repair_kind}
    except Exception as exc:
        return {'model':model,'fhr':fhr,'status':'failed','error':str(exc),
                'fields':sorted(chosen),'pending':pending}
    finally:
        if not keep_grib:
            shutil.rmtree(work, ignore_errors=True)


def add_float_arrays(base_raw, increment_raw):
    b, inc = array('f'), array('f')
    b.frombytes(base_raw); inc.frombytes(increment_raw)
    if sys.byteorder != 'little':
        b.byteswap(); inc.byteswap()
    if len(b) != core.NPTS or len(inc) != core.NPTS:
        raise RuntimeError('Invalid accumulation byte count')
    for i in range(len(b)):
        a, v = b[i], inc[i]
        b[i] = (a+v) if math.isfinite(a) and math.isfinite(v) and a>-1e9 and v>-1e9 else math.nan
    if sys.byteorder != 'little':
        b.byteswap()
    return b.tobytes()


def finalize_accums(store, manifest, level):
    """Convert GRIB interval amounts to 0-F run totals only when provable."""
    updated = False
    for field in ACCUMULATIONS:
        # Pass in forecast-hour order so F001 is available for F002, etc.
        for htext, result in sorted(manifest['forecast_hours'].items(), key=lambda kv:int(kv[0])):
            fhr = int(htext)
            interval = result.get('pending',{}).get(field)
            if not interval:
                continue
            staged = store / '.accum_intervals' / field / f'{fhr}.zlib'
            dest = store / field / f'{fhr}.0.0'
            if dest.exists():
                continue
            if interval == 'unknown' or int(interval[1]) != fhr or not staged.exists():
                logging.warning('%s F%03d %s: uncertain accumulation interval %s; omitted',
                                store.name, fhr, field, interval)
                continue
            start = int(interval[0])
            try:
                inc = zlib.decompress(staged.read_bytes())
                if start == 0:
                    total = inc
                else:
                    predecessor = store / field / f'{start}.0.0'
                    if not predecessor.is_file():
                        logging.warning('%s F%03d %s: cannot build 0-F total without F%03d',
                                        store.name, fhr, field, start)
                        continue
                    total = add_float_arrays(zlib.decompress(predecessor.read_bytes()), inc)
                core.write_compressed_chunk(dest, total, level)
                # Keep intervals for provenance/resuming unless user removes
                # .accum_intervals after validating the run.
                updated = True
            except (OSError,ValueError,zlib.error) as exc:
                logging.warning('%s F%03d %s: could not finalize: %s',store.name,fhr,field,exc)
    return updated


def scan_stores(output_root, retention_days=None, as_of=None):
    """Construct viewer catalogs, optionally excluding cycles beyond retention."""
    now = as_of or dt.datetime.now(dt.timezone.utc)
    cutoff = (now - dt.timedelta(days=retention_days)) if retention_days else None
    grouped = {m:[] for m in MODELS}
    for store in sorted(Path(output_root).glob('*.zarr')):
        m = STORES_RE.fullmatch(store.name)
        if not m or not store.is_dir():
            continue
        model, date, cycle = m.groups()
        if cutoff is not None and iso_init(date, cycle) < cutoff:
            continue
        try:
            attrs = json.loads((store/'.zattrs').read_text())
            if json.loads((store/'.zgroup').read_text()).get('zarr_format') != 2:
                continue
            processed_path = store/'processed'/'0'
            completed = {i for i,v in enumerate(zlib.decompress(processed_path.read_bytes())) if v==1} if processed_path.exists() else set()
            if not completed:
                continue
            variables = []
            for field_dir in sorted(store.iterdir()):
                if not field_dir.is_dir() or field_dir.name in NO_FIELDS or not re.fullmatch(r'[A-Za-z][\w]*',field_dir.name):
                    continue
                meta_path = field_dir/'.zarray'
                if not meta_path.is_file():
                    continue
                md = json.loads(meta_path.read_text())
                shape, chunks = md.get('shape'), md.get('chunks')
                if (not isinstance(shape,list) or len(shape)!=3 or
                    chunks != [1,shape[1],shape[2]] or md.get('dtype') not in ('<f4','|u1') or
                    md.get('compressor',{}).get('id')!='zlib'):
                    continue
                hours = sorted(int(cm.group(1)) for child in field_dir.iterdir()
                               if child.is_file() and (cm:=CHUNK_RE.fullmatch(child.name))
                               and int(cm.group(1)) in completed and child.stat().st_size>0)
                if hours:
                    variables.append({'name':field_dir.name,'shape':shape,'chunks':chunks,
                                      'dtype':md['dtype'],'compressor':md['compressor'],
                                      'attrs':json.loads((field_dir/'.zattrs').read_text())
                                              if (field_dir/'.zattrs').exists() else {},
                                      'available_hours':hours})
            if variables:
                grouped[model].append({'id':store.stem,'path':store.name,
                                       'model':attrs.get('model',model),'domain':attrs.get('domain','CONUS'),
                                       'init_time':attrs.get('init_time',iso_init(date,cycle).strftime('%Y-%m-%dT%H:%M:%SZ')),
                                       'grid':{k:v for k,v in attrs.items() if k.startswith('GRIB_')},
                                       'variables':variables})
        except (OSError, ValueError, zlib.error) as exc:
            logging.warning('Skipping %s in catalog: %s', store, exc)
    written = {}
    for model, entries in grouped.items():
        if not entries:
            continue
        entries.sort(key=lambda e:e['init_time'], reverse=True)
        path = Path(output_root)/f'catalog_{model}.json'
        core.json_write(path,{'schema':'rrfs-zarr-viewer-1',
                              'generated_utc':dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
                              'datasets':entries})
        written[model] = path
    return written


def publish_one(store, catalog, work_root, host, remote_dir, keep_archive=False, store_arcname=None):
    """Uncompressed TAR: Zarr chunks are already zlib-compressed.

    Publish a model's catalog LAST. Unique archives per cycle prevent collision.
    Existing viewer catalog.json and other sources are untouched.
    """
    if not shutil.which('ssh') or not shutil.which('scp'):
        raise RuntimeError('Publishing requires ssh and scp')
    if not re.fullmatch(r'[\w.-]+@[\w.-]+',host):
        raise ValueError('Unsafe --publish-host')
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+',remote_dir):
        raise ValueError('Unsafe --publish-dir')
    work_root.mkdir(parents=True,exist_ok=True)
    archive=work_root/f'{store.stem}.tar'
    tmp=archive.with_name(archive.name+'.tmp')
    try:
        with tarfile.open(tmp,'w') as f:
            def safe_filter(t):
                # Reconstruction intermediates are for WCOSS2 only, not the viewer.
                if '/.accum_intervals/' in t.name or t.name.endswith('/.accum_intervals'):
                    return None
                if not (t.isfile() or t.isdir()):
                    raise RuntimeError('Refusing non-regular file in published Zarr: '+t.name)
                # Analysis stores originate in tempfile.mkdtemp(), which has
                # mode 0700 by default.  Without normalizing TAR entry modes,
                # Apache cannot traverse the published .zarr directory and
                # browser chunk GETs fail with HTTP 403.  Apply only to web
                # analysis artifacts, not private WCOSS2 work directories.
                if store_arcname and store_arcname.startswith('analysis/'):
                    t.mode = 0o755 if t.isdir() else 0o644
                return t
            f.add(store, arcname=store_arcname or store.name,recursive=True,filter=safe_filter)
            if catalog is not None:
                f.add(catalog,arcname=catalog.name,recursive=False,filter=safe_filter)
        os.replace(tmp,archive)
        remote_archive=f'.{archive.name}.incoming'
        subprocess.run(['ssh',host,'mkdir -p '+shlex.quote(remote_dir)],check=True)
        subprocess.run(['scp',str(archive),f'{host}:{remote_dir}/{remote_archive}'],check=True)
        command=(f'set -eu; cd {shlex.quote(remote_dir)}; '
                 f'tar -xf {shlex.quote(remote_archive)}; '
                 f'rm -f {shlex.quote(remote_archive)}')
        subprocess.run(['ssh',host,command],check=True)
        logging.info('Published %s%s to %s',store.name,(' and '+catalog.name) if catalog else '',host)
    finally:
        tmp.unlink(missing_ok=True)
        if archive.is_file() and not keep_archive:
            # On failure retain archive to allow a manual retry.
            if sys.exc_info()[0] is None:
                archive.unlink()


# This is executed on RZDM through SSH. It deliberately cannot remove
# arbitrary directory names, follows no store symlinks, and never touches
# the viewer's JS, CSS, static geography or vendor folders.
REMOTE_RETENTION_SCRIPT = r'''
from pathlib import Path
import datetime as dt
import json, os, re, shutil, sys

root = Path(sys.argv[1])
# RZDM's login-shell python3 may be Python 3.6, which does not have
# datetime.fromisoformat() (introduced in 3.7). Use the UTC timestamp
# format transmitted by WCOSS2 and parse it with Python 3.6-compatible
# datetime.strptime(). Reject unexpected formats before any deletion.
cutoff_text = sys.argv[2]
if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z', cutoff_text):
    raise SystemExit('Refusing unexpected UTC retention cutoff: ' + cutoff_text)
cutoff_body = cutoff_text[:-1]
cutoff_format = '%Y-%m-%dT%H:%M:%S.%f' if '.' in cutoff_body else '%Y-%m-%dT%H:%M:%S'
cutoff = dt.datetime.strptime(cutoff_body, cutoff_format).replace(tzinfo=dt.timezone.utc)
dry_run = sys.argv[3] == '1'
if not root.is_absolute() or root.name != 'data' or root.is_symlink() or not root.is_dir():
    raise SystemExit('Refusing unsafe or nonexistent remote data directory: ' + str(root))
# Strict allow-list for only this processor's output and the existing RRFS store.
pattern = re.compile(r'^(rrfs|refs_mean|refs_m00[1-5]|hrrr)_(\d{8})_(00|06|12|18)00\.zarr$')
# Parse catalogs before performing any deletion. Refuse an inconsistent
# catalog rather than deleting data first and then failing to update it.
cat_re = re.compile(r'^catalog(?:_(?:rrfs|refs_mean|refs_m00[1-5]|hrrr))?\.json$')
catalogs = []
for cat in sorted(root.iterdir()):
    if not cat_re.fullmatch(cat.name) or cat.is_symlink() or not cat.is_file():
        continue
    try:
        obj = json.loads(cat.read_text(encoding='utf-8'))
    except Exception as exc:
        raise SystemExit('Cannot safely inspect catalog ' + str(cat) + ': ' + str(exc))
    if not isinstance(obj, dict):
        raise SystemExit('Catalog root must be an object: ' + str(cat))
    catalogs.append((cat, obj))
pruned = set()
for child in sorted(root.iterdir()):
    match = pattern.fullmatch(child.name)
    if not match or child.is_symlink() or not child.is_dir():
        continue
    try:
        init = dt.datetime.strptime(match.group(2) + match.group(3), '%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
    except ValueError:
        continue
    if init >= cutoff:
        continue
    print(('WOULD REMOVE ' if dry_run else 'REMOVE ') + str(child), flush=True)
    pruned.add(child.name)
    if not dry_run:
        shutil.rmtree(child)
# Catalog files are updated after deletion, removing only paths that are
# recognized model stores and are expired or no longer present.
for cat, obj in catalogs:
    datasets = obj.get('datasets')
    if not isinstance(datasets, list):
        continue
    keep = []
    for entry in datasets:
        name = entry.get('path', '') if isinstance(entry, dict) else ''
        match = pattern.fullmatch(name)
        if not match:
            keep.append(entry)
            continue
        try:
            cycle_init = dt.datetime.strptime(match.group(2) + match.group(3), '%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
        except ValueError:
            keep.append(entry)
            continue
        if cycle_init >= cutoff and (root / name).is_dir() and not (root / name).is_symlink():
            keep.append(entry)
    removed = len(datasets) - len(keep)
    if removed:
        print(('WOULD PRUNE ' if dry_run else 'PRUNE ') + str(cat) + ': ' + str(removed) + ' entries', flush=True)
        if not dry_run:
            obj['datasets'] = keep
            temp = cat.with_name('.' + cat.name + '.retention.' + str(os.getpid()))
            try:
                with temp.open('w', encoding='utf-8') as handle:
                    json.dump(obj, handle, indent=2, sort_keys=True)
                    handle.write('\n')
                os.replace(temp, cat)
            finally:
                if temp.exists():
                    temp.unlink()
print(('DRY RUN ' if dry_run else '') + 'Retention complete: ' + str(len(pruned)) + ' stores', flush=True)
'''


# Local retention uses the same UTC initialization-time cutoff as RZDM.
# Every name must match an exact allow-list; nothing is removed recursively
# unless it is a recognized dated Zarr store or a dated work-cycle directory.
LOCAL_SELECTION_RE = re.compile(
    r'^(rrfs|refs_mean|refs_m00[1-5]|hrrr)_(\d{8})_(00|06|12|18)00\.selection\.json$')
LOCAL_ARCHIVE_RE = re.compile(
    r'^(rrfs|refs_mean|refs_m00[1-5]|hrrr)_(\d{8})_(00|06|12|18)00\.tar(?:\.bz2)?(?:\.tmp)?$')
LOCAL_SUMMARY_RE = re.compile(r'^run_summary_(\d{8})_(00|06|12|18)\.json$')
LOCAL_WORK_CYCLE_RE = re.compile(r'^(\d{8})_(00|06|12|18)$')
LOCAL_LOG_RE = re.compile(r'^multisource_(\d{8})_(\d{6})\.log$')
LOCAL_CATALOG_RE = re.compile(r'^catalog(?:_(?:rrfs|refs_mean|refs_m00[1-5]|hrrr))?\.json$')


def _safe_local_root(path, label):
    """Reject dangerous directory targets while allowing normal WCOSS2 paths."""
    original = Path(path).expanduser()
    if original.is_symlink():
        raise ValueError(f'{label} cannot be a symlink: {original}')
    root = original.resolve()
    if root == Path('/') or len(root.parts) < 3:
        raise ValueError(f'{label} is too broad for automated cleanup: {root}')
    if root.exists() and (not root.is_dir() or root.is_symlink()):
        raise ValueError(f'{label} must be a real directory: {root}')
    return root


def _cycle_from_parts(date, cycle):
    try:
        if not re.fullmatch(r'\d{8}', date) or not re.fullmatch(r'(?:00|06|12|18)', cycle):
            return None
        return iso_init(date, cycle)
    except ValueError:
        return None


def cleanup_local(output_root, work_root, retention_days=7, dry_run=False,
                  now=None, logs_dir=None):
    """Expire recognized WCOSS2 artifacts using init UTC (not filesystem mtime).

    Retention policy: only cycle-scoped stores, selection manifests, summaries,
    publication TARs and post-v3 work folders are eligible. Nonmatching files,
    undated legacy work directories, symlinks, and current data are preserved.
    """
    out = _safe_local_root(output_root, 'output_root')
    work = _safe_local_root(work_root, 'work_root')
    logs = _safe_local_root(logs_dir, 'logs_dir') if logs_dir is not None else None
    roots = [r for r in (out, work, logs) if r is not None]
    if len(set(roots)) != len(roots):
        raise ValueError('Cleanup roots must be distinct directories')
    for first in roots:
        for second in roots:
            if first != second and first in second.parents:
                raise ValueError('Cleanup roots cannot contain each other')
    if not isinstance(retention_days, int) or not 1 <= retention_days <= 366:
        raise ValueError('retention_days must be an integer between 1 and 366')
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError('now must be UTC timezone-aware')
    cutoff = now.astimezone(dt.timezone.utc) - dt.timedelta(days=retention_days)
    stats = {'stores': 0, 'manifests': 0, 'summaries': 0,
             'archives': 0, 'work_cycles': 0, 'logs': 0, 'catalog_entries': 0}

    def eligible(child, match, date_index, cycle_index):
        if child.is_symlink():
            return False
        init = _cycle_from_parts(match.group(date_index), match.group(cycle_index))
        return init is not None and init < cutoff

    def remove(path, category, directory):
        if path.is_symlink() or (not path.is_dir() if directory else not path.is_file()):
            return
        logging.info('WCOSS2 %s %s', 'WOULD REMOVE' if dry_run else 'REMOVE', path)
        stats[category] += 1
        if not dry_run:
            if directory:
                shutil.rmtree(path)
            else:
                path.unlink()

    # Preflight all recognized local catalogs before deleting any data.
    # This keeps local catalog.json and catalog_MODEL.json coherent, just as
    # the RZDM retention script does for its catalogs.
    catalogs = []
    if out.exists():
        for child in sorted(out.iterdir()):
            if not LOCAL_CATALOG_RE.fullmatch(child.name) or child.is_symlink() or not child.is_file():
                continue
            try:
                obj = json.loads(child.read_text(encoding='utf-8'))
                if not isinstance(obj, dict):
                    raise ValueError('Catalog is not a JSON object')
            except (OSError, ValueError) as exc:
                raise RuntimeError(f'Cannot safely inspect local catalog {child}: {exc}') from exc
            catalogs.append((child, obj))

    if out.exists():
        for child in sorted(out.iterdir()):
            match = STORES_RE.fullmatch(child.name)
            if match and eligible(child, match, 2, 3):
                remove(child, 'stores', True)
                continue
            match = LOCAL_SELECTION_RE.fullmatch(child.name)
            if match and eligible(child, match, 2, 3):
                remove(child, 'manifests', False)
                continue
            match = LOCAL_SUMMARY_RE.fullmatch(child.name)
            if match and eligible(child, match, 1, 2):
                remove(child, 'summaries', False)

    if work.exists():
        # Publication TARs (.tar and older standalone .tar.bz2) may be retained on
        # failed SCP/SSH; only archives for expired cycles are removable.
        for child in sorted(work.iterdir()):
            match = LOCAL_ARCHIVE_RE.fullmatch(child.name)
            if match and eligible(child, match, 2, 3):
                remove(child, 'archives', False)
        # After this release, scratch jobs are /work/model/YYYYMMDD_CC/fNNN.
        for model in MODELS:
            model_root = work / model
            if not model_root.is_dir() or model_root.is_symlink():
                continue
            for child in sorted(model_root.iterdir()):
                match = LOCAL_WORK_CYCLE_RE.fullmatch(child.name)
                if match and eligible(child, match, 1, 2):
                    remove(child, 'work_cycles', True)

    if logs is not None and logs.exists():
        for child in sorted(logs.iterdir()):
            match = LOCAL_LOG_RE.fullmatch(child.name)
            if not match or child.is_symlink() or not child.is_file():
                continue
            try:
                ts = dt.datetime.strptime(''.join(match.groups()), '%Y%m%d%H%M%S')
                ts = ts.replace(tzinfo=dt.timezone.utc)
            except ValueError:
                continue
            if ts < cutoff:
                remove(child, 'logs', False)

    # Prune only expired or missing recognized model-store references.
    # Any unrelated/custom dataset entries remain untouched.
    for catalog_path, obj in catalogs:
        entries = obj.get('datasets')
        if not isinstance(entries, list):
            continue
        keep = []
        for entry in entries:
            name = entry.get('path', '') if isinstance(entry, dict) else ''
            match = STORES_RE.fullmatch(name)
            if not match:
                keep.append(entry)
                continue
            init = _cycle_from_parts(match.group(2), match.group(3))
            store_path = out / name
            if init is not None and init >= cutoff and store_path.is_dir() and not store_path.is_symlink():
                keep.append(entry)
        removed = len(entries) - len(keep)
        if removed:
            stats['catalog_entries'] += removed
            logging.info('WCOSS2 %s %s: %d expired/missing entries',
                         'WOULD PRUNE' if dry_run else 'PRUNE', catalog_path, removed)
            if not dry_run:
                obj['datasets'] = keep
                core.json_write(catalog_path, obj)

    logging.info('WCOSS2 retention %s cutoff=%s (UTC): %s',
                 'dry-run' if dry_run else 'complete', cutoff.isoformat(), stats)
    return stats


def validate_publisher(host, remote_dir):
    if not re.fullmatch(r'[\w.-]+@[\w.-]+', host):
        raise ValueError('Unsafe --publish-host')
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', remote_dir):
        raise ValueError('Unsafe --publish-dir')
    # No arbitrary directory deletion via a mistyped argument.
    path = Path(remote_dir)
    if path.name != 'data' or '..' in path.parts:
        raise ValueError('Remote cleanup requires a directory explicitly named data; '
                         'use --no-remote-cleanup for a different publish directory')


def cleanup_remote(host, remote_dir, retention_days=7, dry_run=False, now=None):
    """Remove only date-stamped modeled .zarr stores older than retention."""
    validate_publisher(host, remote_dir)
    if not shutil.which('ssh'):
        raise RuntimeError('Remote retention requires ssh')
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = (now - dt.timedelta(days=retention_days)).isoformat().replace('+00:00', 'Z')
    cmd = 'python3 - ' + shlex.quote(remote_dir) + ' ' + shlex.quote(cutoff) + ' ' + ('1' if dry_run else '0')
    logging.info('RZDM retention: %s stores initialized before %s (%s)',
                 'would remove' if dry_run else 'removing', cutoff, remote_dir)
    return subprocess.run(['ssh', host, cmd], input=REMOTE_RETENTION_SCRIPT,
                          text=True, check=True)


def resolve_cycle(a, now=None):
    """Cron-friendly, UTC-aware resolution, including midnight rollover."""
    if a.date == 'auto' and a.cycle == 'auto':
        now = now or dt.datetime.now(dt.timezone.utc)
        reference = now - dt.timedelta(hours=a.lag_hours)
        init = reference.replace(hour=(reference.hour // 6)*6,
                                 minute=0, second=0, microsecond=0)
        a.date = init.strftime('%Y%m%d')
        a.cycle = init.strftime('%H')
        logging.info('Auto-selected cycle %s %s UTC (lag %sh)', a.date, a.cycle, a.lag_hours)
    elif a.date == 'auto' or a.cycle == 'auto':
        raise ValueError('--date and --cycle must BOTH be auto, or both explicit')
    else:
        dt.datetime.strptime(a.date, '%Y%m%d')


def parse_models(s):
    models = MODELS if s == 'all' else tuple(dict.fromkeys(x.strip().lower() for x in s.split(',')))
    if not models or any(m not in MODELS for m in models):
        raise ValueError('Models must be all or a comma-separated subset of '+','.join(MODELS))
    return models


def arguments(argv=None):
    p=argparse.ArgumentParser(description='Parallel WCOSS2 RRFS / REFS / HRRR GRIB -> Zarr v2 + RZDM publishing')
    p.add_argument('--date',required=True,help='YYYYMMDD or auto (with --cycle auto)')
    p.add_argument('--cycle',required=True,choices=('00','06','12','18','auto'))
    p.add_argument('--lag-hours',type=float,default=3.5,help='UTC auto-cycle availability delay in hours (default 3.5 h = 210 min)')
    p.add_argument('--models',default='all',help='all or rrfs,refs_mean,refs_m001,...,hrrr')
    p.add_argument('--hours',default=None,help='Forecast-hour subset, e.g. 0-3 (default model-specific full range)')
    p.add_argument('--workers',type=int,default=8,help='Concurrent forecast-hour processes, 1-20 (default 8)')
    p.add_argument('--output-root',default='./zarr')
    p.add_argument('--work-root',default='./work')
    p.add_argument('--wgrib2',default=None)
    p.add_argument('--compression-level',type=int,default=3,choices=range(10))
    p.add_argument('--timeout',type=int,default=120)
    p.add_argument('--retries',type=int,default=5)
    p.add_argument('--insecure',action='store_true',help='Disable TLS verification for local CA problems only')
    p.add_argument('--keep-grib',action='store_true')
    p.add_argument('--overwrite-hours',action='store_true')
    p.add_argument('--repair-refs-pressure',action='store_true',
                   help='Safely add missing t925/t850/t700 REFS-member fields to already processed hours, retaining existing fields')
    p.add_argument('--repair-refs-mean',action='store_true',
                   help='Safely add missing REFS mean categorical fields, REFC from PMMN, WEASD and ASNOW to completed forecast hours')
    p.add_argument('--strict-fields',action='store_true',help='Fail if an hour has no supported fields')
    p.add_argument('--no-publish',action='store_true',help='Do not SCP/SSH to RZDM')
    p.add_argument('--inventory-only',action='store_true',help='Inspect GRIB .idx files only; no downloads, Zarr output or publishing (default: first hour per model)')
    p.add_argument('--catalog-only',action='store_true',help='Rebuild per-model catalogs without downloading/publishing')
    p.add_argument('--publish-only',action='store_true',help='Publish existing stores and model catalogs, without processing')
    p.add_argument('--publish-host',default=core.DEFAULT_PUBLISH_HOST)
    p.add_argument('--publish-dir',default=core.DEFAULT_PUBLISH_DIR)
    p.add_argument('--keep-publish-archive',action='store_true')
    p.add_argument('--retention-days',type=int,default=7,help='Keep this many days of initialization cycles on WCOSS2 and RZDM (default 7)')
    p.add_argument('--no-local-cleanup',action='store_true',help='Disable WCOSS2 Zarr, scratch, and summary retention')
    p.add_argument('--logs-dir',default=None,help='Optional cron log directory; expire only multisource_YYYYMMDD_HHMMSS.log files')
    p.add_argument('--no-remote-cleanup',action='store_true',help='Disable SSH retention cleanup')
    p.add_argument('--cleanup-dry-run',action='store_true',help='Preview BOTH WCOSS2 and RZDM retention without deleting')
    p.add_argument('--cleanup-only',action='store_true',help='Run enabled local/remote retention without processing or publishing')
    p.add_argument('-v','--verbose',action='store_true')
    a=p.parse_args(argv)
    try:
        if not (math.isfinite(a.lag_hours) and 0 <= a.lag_hours <= 24):
            raise ValueError('--lag-hours must be within 0-24')
        if not (1 <= a.retention_days <= 366):
            raise ValueError('--retention-days must be within 1-366')
        resolve_cycle(a)
        a.models=parse_models(a.models)
        if a.hours is not None:
            a.hours=core.parse_hours(a.hours)
        if not 1<=a.workers<=20:
            raise ValueError('--workers must be 1-20')
        if a.repair_refs_pressure and a.repair_refs_mean:
            raise ValueError('Select only one REFS repair mode')
        if (a.repair_refs_pressure or a.repair_refs_mean) and (a.overwrite_hours or a.inventory_only or a.catalog_only or a.publish_only):
            raise ValueError('--repair-refs-pressure cannot be combined with overwrite/inventory/catalog/publish-only')
        if a.inventory_only and (a.catalog_only or a.publish_only or a.overwrite_hours):
            raise ValueError('--inventory-only cannot be combined with catalog-only, publish-only, or overwrite-hours')
        if a.publish_only and a.no_publish:
            raise ValueError('--publish-only incompatible with --no-publish')
        if a.publish_only and a.catalog_only:
            raise ValueError('--publish-only incompatible with --catalog-only')
        if a.cleanup_only and (a.inventory_only or a.publish_only or a.catalog_only):
            raise ValueError('--cleanup-only incompatible with inventory/catalog/publish-only modes')
        if a.cleanup_only and a.no_local_cleanup and (a.no_remote_cleanup or a.no_publish):
            raise ValueError('--cleanup-only has both local and remote cleanup disabled')
        if a.cleanup_only and a.overwrite_hours:
            raise ValueError('--cleanup-only incompatible with --overwrite-hours')
    except ValueError as e:
        p.error(str(e))
    return a


def inspect_inventories(a):
    """Network-only field probe; no wgrib2 and no persistent files needed.

    In particular, confirms FROZR/FRZR inventory names and which HRRR
    product contains them before the user schedules a long production run.
    """
    client = core.HTTPClient(timeout=a.timeout, retries=a.retries, insecure=a.insecure)
    failures = 0
    for model in a.models:
        lo,hi=hour_bounds(model)
        hours=[lo] if a.hours is None else [h for h in a.hours if lo<=h<=hi]
        for fhr in hours:
            inventories={}
            for product in PRODUCTS[family(model)]:
                url,idxurl=file_urls(model,a.date,a.cycle,fhr,product)
                try:
                    inventory=core.parse_idx(client.text(idxurl),client.size(url))
                    inventories[product]=inventory
                    print(f'[{model} F{fhr:03d} {product}] {len(inventory)} GRIB records: {url}',flush=True)
                except urllib.error.HTTPError as exc:
                    if exc.code == 404:
                        print(f'[{model} F{fhr:03d} {product}] not published (HTTP 404)',flush=True)
                    else:
                        failures += 1
                        print(f'[{model} F{fhr:03d} {product}] ERROR: {exc}',flush=True)
                except Exception as exc:
                    failures += 1
                    print(f'[{model} F{fhr:03d} {product}] ERROR: {exc}',flush=True)
            matches={}
            for name in FIELDS:
                if name=='wspd10':
                    continue
                for product,rows in inventories.items():
                    r=choose_field(rows,name,fhr)
                    if r:
                        matches[name]=(product,r['text'])
                        break
            for name,(product,record) in matches.items():
                print(f'  {name:9s} {product:8s} {record}',flush=True)
            requested=('frozr','frzr','u10','v10','gust')
            for name in requested:
                if name not in matches:
                    print(f'  {name:9s} -- not found in this forecast hour',flush=True)
            print(f'  {len(matches)} matching fields; derived wspd10 is available when u10/v10 exist',flush=True)
    return failures


def main(argv=None):
    a=arguments(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format='%(asctime)s %(processName)s %(levelname)s %(message)s')
    if a.inventory_only:
        if inspect_inventories(a):
            raise SystemExit(1)
        return
    if a.cleanup_only:
        if not a.no_local_cleanup:
            cleanup_local(a.output_root, a.work_root, a.retention_days,
                          a.cleanup_dry_run, logs_dir=a.logs_dir)
        if not (a.no_publish or a.no_remote_cleanup):
            cleanup_remote(a.publish_host, a.publish_dir, a.retention_days, a.cleanup_dry_run)
        return
    if not (a.no_publish or a.no_remote_cleanup):
        validate_publisher(a.publish_host, a.publish_dir)
    logging.info('Running cycle %s %s UTC', a.date, a.cycle)
    out=Path(a.output_root).expanduser().resolve()
    work=Path(a.work_root).expanduser().resolve()
    out.mkdir(parents=True,exist_ok=True)
    work.mkdir(parents=True,exist_ok=True)
    manifest_paths={}
    manifests={}
    processed={}
    stores={}
    jobs=[]
    if not(a.catalog_only or a.publish_only):
        wgrib2=core.find_wgrib2(a.wgrib2)
        logging.info('Using %s; up to %d parallel forecast-hour workers',wgrib2,a.workers)
    else:
        wgrib2=None
    for model in a.models:
        if a.repair_refs_pressure and not model.startswith('refs_m'):
            continue
        if a.repair_refs_mean and model!='refs_mean':
            continue
        lo,hi=hour_bounds(model)
        hours=range(lo,hi+1) if a.hours is None else [h for h in a.hours if lo<=h<=hi]
        if not hours:
            logging.warning('%s: no hours requested within %d-%d',model,lo,hi)
            continue
        store=out/store_name(model,a.date,a.cycle)
        stores[model]=store
        if a.catalog_only or a.publish_only:
            continue
        init_store(store,model,a.date,a.cycle,a.compression_level)
        if model=='refs_mean':
            for cname in core.PTYPE_INPUTS:
                attrs_path=store/cname/'.zattrs'
                if attrs_path.exists():
                    attrs=json.loads(attrs_path.read_text())
                    attrs['ensemble_mean_fraction']=True
                    attrs['long_name']='Weighted ensemble-mean '+FIELDS[cname]['long_name'].lower()+' (not a deterministic category)'
                    core.json_write(attrs_path,attrs)
        flags=read_processed(store,hi+1)
        processed[model]=flags
        manifest_path=out/f'{store.stem}.selection.json'
        manifest_paths[model]=manifest_path
        manifest=json.loads(manifest_path.read_text()) if manifest_path.exists() else {
            'model':model,'date':a.date,'cycle':a.cycle,'forecast_hours':{}}
        manifests[model]=manifest
        for h in hours:
            repair_fields = None
            if a.repair_refs_pressure:
                # Only touch previously finished hours. Repair missing pressure
                # chunks individually instead of reprocessing surface fields.
                if not flags[h]:
                    continue
                repair_fields = tuple(name for name in ('t925','t850','t700')
                                      if not (store/name/f'{h}.0.0').is_file())
                if not repair_fields:
                    continue
            elif a.repair_refs_mean:
                if not flags[h]:
                    continue
                repair_fields=tuple(name for name in ('refc','crain','cfrzr','cicep','csnow','weasd','asnow')
                                    if not (store/name/f'{h}.0.0').is_file()
                                    and not (store/'.accum_intervals'/name/f'{h}.zlib').is_file())
                if not repair_fields:
                    continue
            else:
                if flags[h] and not a.overwrite_hours:
                    logging.info('%s F%03d already processed; skipping',model,h)
                    continue
                if flags[h]:
                    flags[h]=0
                    write_processed(store,flags,a.compression_level)
            jobs.append((model,a.date,a.cycle,h,str(store),str(work),wgrib2,
                         a.compression_level,a.timeout,a.retries,a.insecure,a.keep_grib,a.strict_fields,
                         repair_fields, 'pressure' if a.repair_refs_pressure else 'mean' if a.repair_refs_mean else None))
    failed=[]
    unavailable=[]
    incomplete_products=[]
    published_errors=[]
    changed_models=set()
    if jobs:
        logging.info('Starting %d forecast-hour tasks across %d models',len(jobs),len(stores))
        with ProcessPoolExecutor(max_workers=min(a.workers,len(jobs))) as pool:
            future_map={pool.submit(process_job,job):(job[0],job[3]) for job in jobs}
            for future in as_completed(future_map):
                model,h=future_map[future]
                try:
                    result=future.result()
                except Exception as exc:
                    result={'model':model,'fhr':h,'status':'failed','error':str(exc)}
                if result['status']=='complete':
                    changed_models.add(model)
                    flags=processed[model]
                    flags[h]=1
                    write_processed(stores[model],flags,a.compression_level)
                    if result.get('repair_only'):
                        previous = manifests[model]['forecast_hours'].get(f'{h:03d}',{})
                        combined = dict(previous)
                        combined['status']='complete'
                        combined['fields']=sorted(set(previous.get('fields',[])) | set(result['fields']))
                        for key in ('selections','products'):
                            combined[key]={**previous.get(key,{}),**result.get(key,{})}
                        combined['repair_'+result.get('repair_kind','fields')+'_added']=result['fields']
                        manifests[model]['forecast_hours'][f'{h:03d}']=combined
                    else:
                        manifests[model]['forecast_hours'][f'{h:03d}']=result
                    core.json_write(manifest_paths[model],manifests[model])
                    missing = result.get('missing_products', {})
                    if missing:
                        incomplete_products.append((model,h,missing))
                        logging.warning('%s F%03d ingested %d fields, but product(s) absent: %s. '
                                        'Remaining fields will show unavailable; use --overwrite-hours '
                                        'to revisit after publication.',
                                        model,h,result['count'], ', '.join(sorted(missing)))
                    else:
                        logging.info('%s F%03d complete (%d fields)',model,h,result['count'])
                elif result['status']=='unavailable':
                    unavailable.append((model,h,result.get('reason','Unavailable'),
                                        result.get('missing_products',{})))
                    logging.warning('%s F%03d unavailable (not fatal): %s; %s',
                                    model,h,result['reason'],result.get('missing_products',{}))
                else:
                    failed.append((model,h,result['error']))
                    logging.error('%s F%03d failed: %s',model,h,result['error'])
    if not (a.catalog_only or a.publish_only):
        for model,store in stores.items():
            if model in manifests:
                finalize_accums(store,manifests[model],a.compression_level)
    catalog_paths=scan_stores(out, retention_days=a.retention_days)
    logging.info('Wrote %d model-specific catalogs: %s',len(catalog_paths),', '.join(catalog_paths))
    if not (a.no_publish or a.catalog_only):
        # Prune before copying the new stores. This frees old data first and
        # lowers the risk of exhausting RZDM disk quota during large SCPs.
        if not a.no_remote_cleanup:
            try:
                cleanup_remote(a.publish_host,a.publish_dir,a.retention_days,a.cleanup_dry_run)
            except Exception as exc:
                published_errors.append(('remote_cleanup', str(exc)))
                logging.error('Remote retention cleanup failed: %s', exc)
        for model,store in stores.items():
            if model not in changed_models and not a.publish_only:
                logging.info('%s: no new hour data; skipping expensive repeat upload', model)
                continue
            catalog=catalog_paths.get(model)
            if not store.is_dir() or catalog is None:
                logging.warning('Cannot publish %s: no completed Zarr store/catalog',model)
                continue
            try:
                publish_one(store,catalog,work,a.publish_host,a.publish_dir,a.keep_publish_archive)
            except Exception as exc:
                published_errors.append((model, str(exc)))
                logging.error('Publishing %s failed: %s', model, exc)
    field_coverage = {}
    for model, manifest in manifests.items():
        model_counts = {}
        for item in manifest.get('forecast_hours', {}).values():
            if item.get('status') != 'complete':
                continue
            for name in item.get('fields', []):
                model_counts[name] = model_counts.get(name, 0) + 1
        field_coverage[model] = dict(sorted(model_counts.items()))
    summary = {'date':a.date, 'cycle':a.cycle, 'models':list(a.models),
               'field_coverage_hour_counts':field_coverage,
               'generated_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
               'completed':sum(len(m.get('forecast_hours',{})) for m in manifests.values()),
               'unavailable_hours':[{'model':m,'hour':h,'reason':why,'missing_products':prods}
                                    for m,h,why,prods in unavailable],
               'hours_with_missing_products':[{'model':m,'hour':h,'missing_products':prods}
                                              for m,h,prods in incomplete_products],
               'failed_hours':[{'model':m,'hour':h,'error':err} for m,h,err in failed],
               'publish_errors':[{'model':m,'error':err} for m,err in published_errors]}
    if not (a.catalog_only or a.publish_only):
        summary_path=out/f'run_summary_{a.date}_{a.cycle}.json'
        core.json_write(summary_path, summary)
        logging.info('Run summary: %s', summary_path)
    if not a.no_local_cleanup:
        try:
            cleanup_local(out, work, a.retention_days, a.cleanup_dry_run,
                          logs_dir=a.logs_dir)
        except Exception as exc:
            published_errors.append(('local_cleanup', str(exc)))
            logging.error('WCOSS2 retention cleanup failed: %s', exc)
    logging.info('Run outcome: %d genuine hour failures, %d unavailable hours, '
                 '%d hours with missing optional products, %d publishing errors',
                 len(failed),len(unavailable),len(incomplete_products),len(published_errors))
    if failed or published_errors:
        logging.error('Errors require attention: see per-hour messages and run summary. '
                      'Rerunning resumes hours still unprocessed.')
        raise SystemExit(1)
    logging.info('Done. Existing catalog.json was NOT overwritten by this publisher. '
                 'Model catalogs: %s',', '.join(p.name for p in catalog_paths.values()))


if __name__=='__main__':
    if '--verification-only' in sys.argv:
        from verification_pipeline import main as run_verification
        run_verification([x for x in sys.argv[1:] if x!='--verification-only'],
                         publish_store=publish_one, validate_publisher=validate_publisher,
                         default_publish_host=core.DEFAULT_PUBLISH_HOST,
                         default_publish_dir=core.DEFAULT_PUBLISH_DIR)
    else:
        main()
