#!/usr/bin/env python3
"""Multi-source RRFS/REFS/HRRR GRIB2 -> browser-compatible Zarr v2.

Processing server: Python standard library + wgrib2. Works with the existing RRFS
converter's tested Zarr/GRIB routines; no pip, numpy, xarray or conda.

Each (model, forecast hour) is an independent process. Only the coordinator
updates the processed array, manifests, catalogs and publishes. Existing RRFS
output and its original catalog.json are not modified unless the RRFS store
itself is explicitly selected for additional hours.
"""
from __future__ import annotations

import argparse
from array import array
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
import datetime as dt
import hashlib
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
import publish_transport
import perf_monitor

# Keep each wgrib2 subprocess single-threaded. The processing environment may allow up to 20
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

            self.user_agent = "omd-evu-converter/1.4.10"



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
            started = time.monotonic()
            with self._request(req) as r:
                data = r.read()
            perf_monitor.add_metric('http_inventory_bytes', len(data))
            perf_monitor.add_metric('http_inventory_seconds', time.monotonic() - started)
            return data.decode("utf-8")



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

            started = time.monotonic()
            with self._request(req) as r:

                data = r.read()

                status = getattr(r, "status", 200)
            perf_monitor.add_metric('http_grib_range_bytes', len(data))
            perf_monitor.add_metric('http_grib_range_seconds', time.monotonic() - started)



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

                "wgrib2 was not found in PATH. Load/use the configured processing environment "

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

        compress_started = time.monotonic()
        compressed = zlib.compress(raw, level)
        perf_monitor.add_metric('zlib_compress_seconds', time.monotonic() - compress_started)
        perf_monitor.add_metric('zlib_input_bytes', len(raw))
        perf_monitor.add_metric('zlib_output_bytes', len(compressed))
        write_started = time.monotonic()
        with tmp.open("wb") as f:

            f.write(compressed)

        os.replace(tmp, path)
        perf_monitor.add_metric('zarr_chunk_write_seconds', time.monotonic() - write_started)



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
    'mslp': dict(long_name='Mean sea level pressure', short='PRMSL', level='mean sea level', units='Pa', product='2dfld'),
    'u10': dict(long_name='10-m U wind component', short='UGRD', level='10 m above ground', units='m s-1', product='2dfld'),
    'v10': dict(long_name='10-m V wind component', short='VGRD', level='10 m above ground', units='m s-1', product='2dfld'),
    'gust': dict(long_name='10-m wind gust', short='GUST', level='surface', units='m s-1', product='2dfld'),
    'psfc': dict(long_name='Native surface pressure (terrain-relative shear reference)',
                 short='PRES', level='surface', units='Pa', product='2dfld'),
    'wspd10': dict(long_name='10-m wind speed derived from U/V', short='WIND', level='10 m above ground', units='m s-1', product='derived'),
})
# Upper-air fields are selected from the exact pressure-level records, not
# substituted from other levels. Each field is optional in each GRIB inventory.
for pressure in (925, 850, 700, 500, 300):
    for prefix, short, units, description in (
        ('h','HGT','gpm','Geopotential height'),
        ('u','UGRD','m s-1','U wind'),('v','VGRD','m s-1','V wind')):
        FIELDS[f'{prefix}{pressure}'] = dict(long_name=f'{pressure}-hPa {description}',
            short=short, level=f'{pressure} mb', units=units, product='prslev')
# Optional isobaric moisture. DPT is native when published; SPFH is
# preferred as an input for deriving dewpoint when DPT is absent.
for pressure in (700, 850, 925):
    FIELDS[f'q{pressure}'] = dict(long_name=f'{pressure}-hPa specific humidity',
        short='SPFH', level=f'{pressure} mb', units='kg kg-1', product='prslev')
    FIELDS[f'td{pressure}'] = dict(long_name=f'{pressure}-hPa dewpoint',
        short='DPT', level=f'{pressure} mb', units='K', product='prslev')
FIELDS['wspd300'] = dict(long_name='300-hPa wind speed derived from U/V',short='DERIVED',
                          level='300 mb',units='m s-1',product='derived')
FIELDS['absv500'] = dict(long_name='500-hPa absolute vorticity', short='ABSV',
                          level='500 mb', units='s-1', product='prslev')
# Conservative severe-weather selections: require matching short name AND
# stated layer. Unavailable products remain absent, never estimated/fabricated.
SEVERE_SPECS = {
    'srh01': ('HLCY', ('0-1000 m above ground','1000-0 m above ground'), 'm2 s-2', '0–1 km storm-relative helicity'),
    'srh03': ('HLCY', ('0-3000 m above ground','3000-0 m above ground'), 'm2 s-2', '0–3 km storm-relative helicity'),
    'sbcape': ('CAPE', ('surface',), 'J kg-1', 'Surface-based CAPE'),
    'mlcape': ('CAPE', ('180-0 mb above ground','0-180 mb above ground'), 'J kg-1', 'Mixed-layer CAPE'),
    'mucape': ('CAPE', ('most unstable', '255-0 mb above ground'), 'J kg-1', 'Most-unstable CAPE'),
    # Instantaneous UPHL deliberately not acquired. MXUPHL must represent
    # the SINGLE hour ending at F###; run maximum is derived independently.
    'mxuphl': ('MXUPHL', ('5000-2000 m above ground', '2000-5000 m above ground'),
               'm2 s-2', '1-hour maximum updraft helicity (2–5 km AGL)'),
}
# True bulk vector shear is sqrt(dU^2 + dV^2), not the GRIB VWSH
# vertical derivative. U/V shear components are the UPP VUCSH/VVCSH
# records on the 0–1 km and 0–6 km layers (s-1). Multiply the
# vertical gradient magnitude by the layer depth in metres to obtain bulk
# vector wind difference (m s-1), then the viewer converts to knots.
SHEAR_COMPONENTS = {
    'shru01': ('VUCSH', ('0-1000 m above ground','1000-0 m above ground','0-1 km above ground')),
    'shrv01': ('VVCSH', ('0-1000 m above ground','1000-0 m above ground','0-1 km above ground')),
    'shru06': ('VUCSH', ('0-6000 m above ground','6000-0 m above ground','0-6 km above ground')),
    'shrv06': ('VVCSH', ('0-6000 m above ground','6000-0 m above ground','0-6 km above ground')),
}
for field,(short,layers) in SHEAR_COMPONENTS.items():
    FIELDS[field] = dict(long_name=field+' bulk-shear component',short=short,
                        level=layers[0],levels=layers,units='m s-1',product='2dfld')
for suffix in ('01','06'):
    FIELDS['shear'+suffix] = dict(long_name=f'0–{1 if suffix=="01" else 6} km bulk wind shear',
                                 short='DERIVED',level='0 m above ground', units='m s-1',product='derived')
# Bunkers right-moving storm-motion components. They are distinct from
# ambient wind and have native units m/s. Missing GRIB fields remain absent.
for field, short in (('ustm','USTM'),('vstm','VSTM')):
    FIELDS[field] = dict(long_name='Bunkers right-moving storm motion '+field[0].upper(),
        short=short,level='0-6000 m above ground',
        levels=('0-6000 m above ground','6000-0 m above ground'),
        units='m s-1',product='2dfld')
for output, (short, layers, units, long_name) in SEVERE_SPECS.items():
    FIELDS[output] = dict(long_name=long_name, short=short,
                          level=layers[0] if layers else 'effective layer',
                          levels=layers, units=units, product='2dfld')
# A derived spatial MAX over all *consecutive, complete* 1-hour UH maxima.
# This field is never downloaded directly from GRIB as an accumulation.
FIELDS['mxuphl_run'] = dict(long_name='Accumulated maximum updraft helicity (2–5 km AGL)',
                            short='DERIVED', level='5000-2000 m above ground',
                            units='m2 s-2', product='derived')

# Accumulated fields are processed carefully: nonzero-start interval amounts
# must be added to a prior total before appearing as cumulative in the viewer.
ACCUMULATIONS = {'apcp', 'frozr', 'frzr', 'tsnowp', 'asnow', 'weasd'}
# wgrib2 indexes may express accumulation intervals in hours OR days.
# For example HRRR ASNOW at F024/F048 uses '0-1 day acc'/'0-2 day acc'.
# Day means exactly 24 forecast hours in this GRIB time-unit context.
ACC_RANGE = re.compile(r'(?<![\d.])(\d+)\s*-\s*(\d+)\s*(hour|hr|day|days)\s+acc\b', re.I)
ACC_LENGTH = re.compile(r'(?<![\d.])(\d+)\s*(hour|hr|day|days)\s+acc\b', re.I)
STORES_RE = re.compile(r'^(rrfs|refs_mean|refs_m00[1-5]|hrrr)_(\d{8})_(00|06|12|18)00\.zarr$')
CHUNK_RE = re.compile(r'^(\d+)\.0\.0$')
NO_FIELDS = {'processed', 'valid_time', 'forecast_hour', 'latitude', 'longitude', 'x', 'y'}


def family(model):
    # 'refs_mean' also begins with 'refs_m'.  Match ensemble members
    # precisely so REFS Mean uses ('mean', 'pmmn'), not member products.
    return 'refs_member' if model in ('refs_m001', 'refs_m002', 'refs_m003',
                                      'refs_m004', 'refs_m005') else model


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
    """Decode explicit GRIB accumulation intervals as UTC forecast hours.

    Returns (start,end), None when not an accumulation, or 'unknown'
    when an accumulation cannot be safely interpreted.  Day-based
    intervals in UPP inventories (e.g. '0-1 day acc f') are 24-hour
    intervals, not missing data.  Mismatched end times must not be
    misrepresented as run totals.
    """
    m = ACC_RANGE.search(text)
    if m:
        scale = 24 if m.group(3).lower().startswith('day') else 1
        return int(m.group(1)) * scale, int(m.group(2)) * scale
    m = ACC_LENGTH.search(text)
    if m:
        scale = 24 if m.group(2).lower().startswith('day') else 1
        width = int(m.group(1)) * scale
        return max(0, fhr-width), fhr
    return 'unknown' if re.search(r'\bacc\b', text, re.I) else None


# Accept explicit time intervals only. A running max must never be
# confused with the hourly MXUPHL GRIB record.
MAX_RANGE = re.compile(r'(?<!\d)(\d+)\s*-\s*(\d+)\s*(?:hour|hr)\s+max\b', re.I)
MAX_LENGTH = re.compile(r'(?<!\d)(\d+)\s*(?:hour|hr)\s+max\b', re.I)


def is_one_hour_max(text, fhr):
    if fhr < 1:
        return False
    match = MAX_RANGE.search(text)
    if match:
        return (int(match.group(1)), int(match.group(2))) == (fhr - 1, fhr)
    match = MAX_LENGTH.search(text)
    return bool(match and int(match.group(1)) == 1)


# MSLP records use different GRIB abbreviations for different NCEP products.
# Favor the native reduction and never substitute surface PRES.  Match only the
# GRIB mean-sea-level level, not an unrelated mention of pressure in its title.
MSLP_PRIORITY = {
    'hrrr': ('MSLMA', 'PRMSL', 'MSLET'),
    'refs_mean': ('MSLET', 'PRMSL', 'MSLMA'),
}

def select_mslp_record(inventories, model):
    """Return (product, idx record) from {product: rows}, or None."""
    priorities = MSLP_PRIORITY.get(model, ('PRMSL', 'MSLET', 'MSLMA'))
    for code in priorities:
        for product, rows in inventories.items():
            for row in rows:
                short = row['short_name'].strip().upper().split()[0]
                # wgrib2 locally-defined variants may include a trailing M.
                if short == code and ':mean sea level:' in row['text'].lower():
                    return product, row
    return None


def choose_field(rows, name, fhr, fields=None):
    """Choose a native GRIB field, allowing a model-specific field registry.

    RRFS/REFS/HRRR use the original shared FIELDS by default. GFS passes
    its extended registry for additional 250/1000-hPa fields; this avoids
    mutating which fields the other model families request or publish.
    """
    info = (FIELDS if fields is None else fields)[name]
    if name == 'mslp':
        selected = select_mslp_record({'inventory': rows}, 'rrfs')
        return selected[1] if selected else None
    # Avoid mistaking wind gust at 10 m for arbitrary GUST levels in GRIB.
    cands = [r for r in rows if r['short_name'].upper() == info['short']
             and info['level'].lower() in r['text'].lower()]
    if name in SHEAR_COMPONENTS or name in ('ustm', 'vstm'):
        # UPP inventories describe 0–6 km storm-motion layers in either
        # vertical order. RRFS/REFS commonly use "6000-0 m above ground",
        # whereas HRRR uses "0-6000 m above ground". Matching only the
        # primary level string silently drops native RRFS right-mover fields.
        layers=tuple(x.lower() for x in info['levels'])
        cands=[r for r in rows if r['short_name'].upper()==info['short'] and
               any(layer in r['text'].lower() for layer in layers)]
        return min(cands,key=lambda r:r['msg']) if cands else None
    if name in SEVERE_SPECS:
        acceptable=tuple(x.lower() for x in info['levels'])
        # The three composite parameters have unique, well-defined GRIB2
        # identifiers; accept their native level metadata, which varies by UPP.
        cands=[r for r in rows if r['short_name'].upper()==info['short'] and
               (not acceptable or any(layer in r['text'].lower() for layer in acceptable))]
        if name=='mxuphl':
            cands=[r for r in cands if is_one_hour_max(r['text'],fhr)]
        return min(cands,key=lambda r:r['msg']) if cands else None
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
                          'long_name': ('PMM composite reflectivity'
                                        if model == 'refs_mean' and name == 'refc'
                                        else 'PMM accumulated precipitation' if model == 'refs_mean' and name == 'apcp'
                                        else 'Magnitude of ensemble-mean 10-m wind vector (not mean scalar wind speed)'
                                        if model == 'refs_mean' and name == 'wspd10'
                                        else info['long_name']), 'units': info['units'],
                          'GRIB_shortName': (MSLP_PRIORITY.get(model, ('PRMSL',))[0] if name == 'mslp' else info['short']), 'GRIB_level': info['level'],
                          **({'GRIB_acceptable_shortNames': ['PRMSL','MSLET','MSLMA'], 'pressure_reduction': 'model-native'} if name == 'mslp' else {}),
                          **({'ensemble_mean_fraction': True,
                              'long_name': 'Weighted ensemble-mean '+info['long_name'].lower()+' (not a deterministic category)'}
                             if model=='refs_mean' and name in core.PTYPE_INPUTS else {}),
                          **({'probability_matched_mean': True, 'GRIB_product': 'pmmn'}
                             if model=='refs_mean' and name in ('refc','apcp') else {})}, level)
    # REFS mean gets a distinct dominant-ensemble-type derivation below.
    core.ensure_array(store, 'ptype', (length, core.NY, core.NX),
                          (1, core.NY, core.NX), '|u1', 0,
                          {'_ARRAY_DIMENSIONS': ['valid_time','y','x'],
                           'long_name': ('Derived precipitation type' if model == 'hrrr' else 'Dominant precipitation type'), 'units': '1',
                           'flag_values': [0,1,2,3,4,5],
                           **({'ensemble_dominant_type': True,
                               'long_name': 'Dominant REFS ensemble precipitation type (heuristic; not a deterministic PTYPE field)',
                               'derivation': 'Largest categorical mean fraction >= 0.1; near ties (within 0.05) designated mixed'}
                              if model == 'refs_mean' else {})}, level)


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


def derive_absolute_vorticity_from_winds(u_raw, v_raw, nx=None, ny=None):
    """Absolute vorticity from REFS ensemble-mean earth-relative 500 hPa U/V.

    Finite differences are transformed through the exact Lambert conformal
    geometry to local east/north distances; includes spherical metric curvature
    and Coriolis. Boundaries use one-sided finite differences. Source is NOT
    represented as a native GRIB ABSV field. No non-standard dependencies.
    """
    nx = nx or core.NX
    ny = ny or core.NY
    u, v = array('f'), array('f')
    u.frombytes(u_raw)
    v.frombytes(v_raw)
    if sys.byteorder != 'little':
        u.byteswap(); v.byteswap()
    if len(u) != nx*ny or len(v) != nx*ny:
        raise ValueError('Mean 500-hPa wind chunks do not match model grid')
    deg = math.pi/180.0
    earth_radius = 6371229.0
    phi0 = core.GRID_ATTRS['GRIB_Latin1InDegrees']*deg
    n = math.sin(phi0)
    F = (math.cos(phi0) * math.tan(math.pi/4 + phi0/2)**n) / n
    rho0 = earth_radius * F / math.tan(math.pi/4+phi0/2)**n
    phi1 = core.GRID_ATTRS['GRIB_latitudeOfFirstGridPointInDegrees']*deg
    lon1 = core.GRID_ATTRS['GRIB_longitudeOfFirstGridPointInDegrees']*deg
    lov = core.GRID_ATTRS['GRIB_LoVInDegrees']*deg
    rho1 = earth_radius * F / math.tan(math.pi/4+phi1/2)**n
    x0 = rho1*math.sin(n*(lon1-lov))
    y0 = rho0-rho1*math.cos(n*(lon1-lov))
    dx = core.GRID_ATTRS['GRIB_DxInMetres']
    dy = core.GRID_ATTRS['GRIB_DyInMetres']
    out = array('f', [math.nan]) * (nx*ny)
    def finite(i):
        return math.isfinite(i) and abs(i)<1e9
    for iy in range(ny):
        ym = max(iy-1,0)
        yp = min(iy+1,ny-1)
        grid_y = y0+iy*dy
        yy = rho0-grid_y
        for ix in range(nx):
            xx = x0+ix*dx
            r = math.hypot(xx, yy)
            phi = 2.0*math.atan((earth_radius*F/r)**(1/n))-math.pi/2
            gamma = math.atan2(xx,yy)
            cos_phi=math.cos(phi)
            if cos_phi<=0: continue
            # Projection scale (grid metres per ground metre).
            scale=n*r/(earth_radius*cos_phi)
            center=iy*nx+ix
            xm=max(ix-1,0); xp=min(ix+1,nx-1)
            il=iy*nx+xm; ir=iy*nx+xp
            ib=ym*nx+ix; it=yp*nx+ix
            if not all(finite(q) for q in (u[center],u[il],u[ir],u[ib],u[it],
                                           v[center],v[il],v[ir],v[ib],v[it])):
                continue
            du_dx=(u[ir]-u[il])/((xp-xm)*dx)
            du_dy=(u[it]-u[ib])/((yp-ym)*dy)
            dv_dx=(v[ir]-v[il])/((xp-xm)*dx)
            dv_dy=(v[it]-v[ib])/((yp-ym)*dy)
            relative=scale*(math.cos(gamma)*(dv_dx-du_dy) +
                            math.sin(gamma)*(dv_dy+du_dx))
            # Spherical east/north metric: +u*tan(phi)/a.
            relative+=u[center]*math.tan(phi)/earth_radius
            coriolis=2.0*7.2921159e-5*math.sin(phi)
            out[center]=relative+coriolis
    if sys.byteorder!='little':out.byteswap()
    return out.tobytes()


def fill_refs_mean_500_vorticity(store, manifest, level):
    """Derive only missing ABSV500 from available mean winds, never overwrite native data."""
    added=[]
    for htext,rec in sorted(manifest.get('forecast_hours',{}).items()):
        if rec.get('status')!='complete':continue
        h=int(htext)
        target=store/'absv500'/f'{h}.0.0'
        if target.is_file():continue
        up=store/'u500'/f'{h}.0.0'; vp=store/'v500'/f'{h}.0.0'
        if not up.is_file() or not vp.is_file():continue
        try:
            raw=derive_absolute_vorticity_from_winds(zlib.decompress(up.read_bytes()),
                                                     zlib.decompress(vp.read_bytes()))
            core.write_compressed_chunk(target,raw,level)
        except (OSError,ValueError,zlib.error) as exc:
            logging.warning('%s F%03d: cannot derive 500-hPa vorticity: %s',store.name,h,exc)
            continue
        rec['fields']=sorted(set(rec.get('fields',[]))|{'absv500'})
        rec.setdefault('selections',{})['absv500']={
            'product':'derived', 'derivation':'absolute_vorticity_from_ensemble_mean_u500_v500',
            'source_fields':['u500','v500']}
        added.append(h)
    if added:
        attrs_path=store/'absv500'/'.zattrs'
        attrs=json.loads(attrs_path.read_text())
        attrs.update({'long_name':'Derived REFS Mean 500-hPa absolute vorticity',
                      'derived_from':['u500','v500'],
                      'derivation':'lambert_finite_difference_plus_coriolis',
                      'GRIB_shortName':'DERIVED',
                      'units':'s-1'})
        core.json_write(attrs_path,attrs)
    return added




def is_profile_shear_model(model):
    """Only RRFS and the five native REFS members have the suspect 0–1 km product."""
    return model == 'rrfs' or family(model) == 'refs_member'


PROFILE_SHEAR_REVISION = 6
PROFILE_SHEAR_INPUTS = ('psfc', 'u10', 'v10',
                        'h925', 'u925', 'v925', 'h850', 'u850', 'v850',
                        'h700', 'u700', 'v700', 'h500', 'u500', 'v500')


def safe_revision(value, default=0):
    """Return a usable integer revision; null or malformed metadata is stale.

    JSON manifests can legitimately contain an explicit null when a source
    field was unavailable.  Treat that the same as an absent revision (zero)
    so comparisons remain safe and targeted repair logic can make the decision.
    """
    if value is None:
        return default
    try:
        revision = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    # Do not silently truncate a non-integral JSON float into a valid revision.
    if isinstance(value, float) and revision != value:
        return default
    return revision


def manifest_revision(manifest_hour, key):
    """Read a per-hour revision safely from an optional manifest object."""
    if not isinstance(manifest_hour, dict):
        return 0
    return safe_revision(manifest_hour.get(key), 0)
PROFILE_SHEAR_OUTPUTS = ('shru01', 'shrv01', 'shear01')
PROFILE_SHEAR_SOURCE = 'derived_native_3km_pressure_level_winds_height_surface_pressure'


def profile_shear_missing_inputs(store, fhr):
    """Acquire indispensable inputs; existing two-level profiles work offline.

    Height and wind levels farther aloft are optional: no single pressure
    level is mandatory because it can fall below terrain. A minimum of two
    above-surface level triplets is required for the per-gridpoint calculation.
    """
    has=lambda name:(store/name/f'{fhr}.0.0').is_file()
    missing=[name for name in ('psfc','u10','v10') if not has(name)]
    complete=[p for p in (925,850,700,500)
              if all(has(prefix+str(p)) for prefix in ('h','u','v'))]
    if len(complete)<2:
        # Fetch the lower three native pressure levels, allowing a below-ground
        # level to be excluded at each gridpoint; use 500 hPa as an upper fallback.
        missing.extend(name for p in (925,850,700,500)
                       for name in (f'h{p}',f'u{p}',f'v{p}') if not has(name))
    return tuple(missing)



def derive_profile_bulk_shear(fields, expected_points=None):
    """Estimate 0–1 km AGL bulk wind difference from *native, co-grid* arrays.

    `fields` is a mapping from logical names to uncompressed float32 bytes.
    Uses the 10-m wind for the bottom, the 925/850/700/500-hPa native wind
    and geopotential heights to interpolate the wind at 1 km ABOVE TERRAIN.
    Surface geopotential height is estimated by log-pressure extrapolation
    using two *above-ground* isobaric heights and native surface pressure.
    A layer-top wind is only interpolated between two above-ground levels;
    no below-ground wind or extrapolated top wind is allowed. All input grids
    are required to be identical (caller enforces 3-km grid dimensions).
    Returns (U,V,scalar,valid_count) where U/V are upper minus 10-m in m/s.
    """
    def unpack(name):
        raw=fields.get(name)
        if raw is None:return None
        a=array('f');a.frombytes(raw)
        if sys.byteorder!='little':a.byteswap()
        return a
    pressure=unpack('psfc');bottom_u=unpack('u10');bottom_v=unpack('v10')
    if pressure is None or bottom_u is None or bottom_v is None:
        raise ValueError('0–1 km profile shear requires native psfc/u10/v10')
    n=len(pressure)
    if expected_points is not None and n!=expected_points:
        raise ValueError('3-km profile shear grid length mismatch')
    levels=[]
    for p in (925,850,700,500):
        h,u,v=unpack('h'+str(p)),unpack('u'+str(p)),unpack('v'+str(p))
        if h is not None and u is not None and v is not None:
            levels.append((p,h,u,v))
    if len(levels)<2:
        raise ValueError('0–1 km profile shear requires two native height/U/V pressure levels')
    if any(len(a)!=n for a in (bottom_u,bottom_v)) or any(
            len(a)!=n for _,h,u,v in levels for a in (h,u,v)):
        raise ValueError('Incompatible GRIB arrays for 3-km profile shear')
    nan=math.nan
    out_u=array('f',[nan])*n;out_v=array('f',[nan])*n;mag=array('f',[nan])*n
    count=0
    for i in range(n):
        sp=pressure[i]*0.01  # GRIB native surface pressure is Pa.
        su,sv=bottom_u[i],bottom_v[i]
        if not (math.isfinite(sp) and 450<sp<1100 and
                math.isfinite(su) and math.isfinite(sv) and
                abs(su)<150 and abs(sv)<150):continue
        # Pressure-level winds from heights at and ABOVE the surface only.
        available=[]
        for p,hs,us,vs in levels:
            h,u,v=hs[i],us[i],vs[i]
            if (p < sp-0.25 and math.isfinite(h) and -500<h<10000 and
                math.isfinite(u) and math.isfinite(v) and
                abs(u)<150 and abs(v)<150):
                available.append((p,h,u,v))
        if len(available)<2:continue
        # Lowest two above-ground levels constrain local height vs ln(p).
        p0,z0,_,_=available[0];p1,z1,_,_=available[1]
        dp=math.log(p0/p1)
        if not (0.005<dp<0.7 and 10<z1-z0<5000):continue
        # Don't extrapolate surface height beyond one coarse (~175 hPa)
        # pressure interval below the first above-ground level.
        if sp-p0>175:continue
        z_surface=z0-(z1-z0)*math.log(sp/p0)/dp
        target=z_surface+1000
        # A native 10-m wind is used at bottom but never as an upper bracket.
        # Prefer bracketing top within native pressure levels; no extrapolation.
        upper=None
        for lo,hi in zip(available,available[1:]):
            if lo[1]<=target<=hi[1] and 0<hi[1]-lo[1]<5000:
                f=(target-lo[1])/(hi[1]-lo[1])
                upper=(lo[2]+f*(hi[2]-lo[2]),lo[3]+f*(hi[3]-lo[3]))
                break
        if upper is None:continue
        du,dv=upper[0]-su,upper[1]-sv
        speed=math.hypot(du,dv)
        if not (math.isfinite(speed) and speed<=150):continue
        out_u[i]=du;out_v[i]=dv;mag[i]=speed;count+=1
    if sys.byteorder!='little':
        out_u.byteswap();out_v.byteswap();mag.byteswap()
    return out_u.tobytes(),out_v.tobytes(),mag.tobytes(),count


def write_derived_profile_shear(store, fhr, source_fields, level):
    """Write a complete *consistent* three-field bundle or fail without edits."""
    raw={}
    for name in PROFILE_SHEAR_INPUTS:
        if name in source_fields:
            raw[name]=source_fields[name]
            continue
        path=store/name/f'{fhr}.0.0'
        if path.is_file():
            raw[name]=zlib.decompress(path.read_bytes())
    u,v,m,n=derive_profile_bulk_shear(raw,expected_points=core.NPTS)
    if n==0:
        raise ValueError('Cannot derive 0–1 km shear: zero valid 3-km terrain-relative profiles')
    # Stage ALL three chunks before replacing any, then move staged files
    # into place. A failed computation never overwrites earlier data.
    targets=[(name,blob) for name,blob in zip(PROFILE_SHEAR_OUTPUTS,(u,v,m))]
    staged=[]
    try:
        for name,blob in targets:
            path=store/name/f'{fhr}.0.0'
            tmp=path.with_name(path.name+'.profile-shear-stage')
            core.write_compressed_chunk(tmp,blob,level)
            staged.append((tmp,path))
        for tmp,dest in staged:os.replace(tmp,dest)
    finally:
        for tmp,_ in staged:tmp.unlink(missing_ok=True)
    logging.info('%s F%03d: computed 0–1 km AGL shear for %d/%d 3-km points',
                 store.name,fhr,n,core.NPTS)
    return n


def scale_float_chunk(raw, scale):
    """Multiply a little-endian float32 GRIB-derived field by a scalar."""
    vals=array('f')
    vals.frombytes(raw)
    if sys.byteorder != 'little': vals.byteswap()
    for i,v in enumerate(vals):
        vals[i]=v*scale if math.isfinite(v) and v>-1e9 else math.nan
    if sys.byteorder != 'little': vals.byteswap()
    return vals.tobytes()


def infer_bulk_shear_units(u_raw, v_raw, model):
    """Identify GRIB VUCSH/VVCSH as 1/s gradient or direct m/s difference.

    Native GRIB2 VUCSH/VVCSH metadata specifies reciprocal seconds for
    HRRR, RRFS, and REFS; do not assume that ambiguous 0.03–0.2 values
    are direct wind differences for RRFS/REFS. That older fallback caused
    severe under-scaling specifically in 0–1 km shear. Exceptionally high
    sample medians are treated as already-normalized only for compatibility,
    and sample diagnostics are logged for auditing.
    """
    u,v=array('f'),array('f');u.frombytes(u_raw);v.frombytes(v_raw)
    if sys.byteorder!='little':u.byteswap();v.byteswap()
    if len(u)!=len(v):raise ValueError('Mismatched bulk-shear component lengths')
    step=max(1,len(u)//2048)
    values=sorted(math.hypot(u[i],v[i]) for i in range(0,len(u),step)
                  if math.isfinite(u[i]) and math.isfinite(v[i]) and
                  -1e9<u[i]<1e9 and -1e9<v[i]<1e9)
    if not values:raise ValueError('No valid native shear components')
    median=values[len(values)//2]
    # The native GRIB unit is 1/s for all three model families.
    # In particular 0.03–0.2 is NOT evidence of m/s units.
    if median>=0.2:return 'm/s',median
    return 's-1',median


def orient_low_level_shear(u_bytes, v_bytes, near_surface, low_level):
    """Check 0-1 km shear sign against independent native low-level winds.

    A negative signed coherence means the GRIB shear component convention is
    lower-minus-upper rather than upper-minus-lower. Only reverse when robust
    agreement is present; an ambiguous test leaves the source direction alone.
    This never changes the scalar magnitude and never touches 0-6 km shear.
    """
    if not near_surface or not low_level:return u_bytes,v_bytes,'unverified (reference winds missing)'
    def unpack(b):
        a=array('f');a.frombytes(b)
        if sys.byteorder!='little':a.byteswap()
        return a
    su,sv=map(unpack,near_surface)
    lu,lv=map(unpack,low_level)
    u,v=unpack(u_bytes),unpack(v_bytes)
    if len({len(su),len(sv),len(lu),len(lv),len(u),len(v)})!=1:
        return u_bytes,v_bytes,'unverified (reference grid mismatch)'
    positive=negative=0
    stride=max(1,len(u)//8000)
    for i in range(0,len(u),stride):
        vals=(su[i],sv[i],lu[i],lv[i],u[i],v[i])
        if not all(math.isfinite(x) and abs(x)<150 for x in vals):continue
        ru,rv=lu[i]-su[i],lv[i]-sv[i]
        rm=math.hypot(ru,rv);sm=math.hypot(u[i],v[i])
        if rm<2. or sm<2.:continue
        similarity=(u[i]*ru+v[i]*rv)/(rm*sm)
        if similarity>.45:positive+=1
        if similarity<-.45:negative+=1
    total=positive+negative
    if total<80 or max(positive,negative)<.7*total:
        return u_bytes,v_bytes,('unverified (direction coherence %d / %d)'%(positive,negative))
    reverse=negative>positive
    if reverse:
        for i in range(len(u)):
            u[i]=-u[i];v[i]=-v[i]
        if sys.byteorder!='little':u.byteswap();v.byteswap()
        return u.tobytes(),v.tobytes(),('reversed (negative=%d positive=%d)'%(negative,positive))
    return u_bytes,v_bytes,('upper-minus-lower validated (positive=%d negative=%d)'%(positive,negative))


def derive_bulk_shear_bytes(u_raw,v_raw,depth_m,model):
    unit,median=infer_bulk_shear_units(u_raw,v_raw,model)
    scale=depth_m if unit=='s-1' else 1
    speed=decode_wspd(u_raw,v_raw)
    vals=array('f');vals.frombytes(speed)
    if sys.byteorder!='little':vals.byteswap()
    # Guard against silently publishing implausible multi-thousand-knot winds.
    # Discard only individual physically impossible values (and preserve NaNs).
    for i,x in enumerate(vals):
        v=x*scale if math.isfinite(x) else math.nan
        vals[i]=v if 0<=v<=250 else math.nan
    if sys.byteorder!='little':vals.byteswap()
    logging.info('%s bulk shear depth=%dm components=%s (median %.5g), multiplier=%s',
                 model,depth_m,unit,median,scale)
    return vals.tobytes()



def normalize_bulk_shear_pair(u_raw, v_raw, depth_m, model):
    """Convert a native shear-gradient pair to compatible m/s components.

    The viewer draws barbs from the two stored components, so the component
    units MUST agree with the derived scalar, not remain in native s^-1.
    Return (U bytes, V bytes, speed bytes) with the same finite-value mask.
    """
    unit, median = infer_bulk_shear_units(u_raw, v_raw, model)
    factor = float(depth_m) if unit == 's-1' else 1.0
    raw_u, raw_v = array('f'), array('f')
    raw_u.frombytes(u_raw); raw_v.frombytes(v_raw)
    if sys.byteorder != 'little':raw_u.byteswap();raw_v.byteswap()
    result_u, result_v, speed = array('f'), array('f'), array('f')
    for u, v in zip(raw_u, raw_v):
        a, b = u*factor, v*factor
        if not (math.isfinite(a) and math.isfinite(b) and
                -250 <= a <= 250 and -250 <= b <= 250 and
                math.hypot(a,b) <= 250):
            a = b = math.nan
            magnitude = math.nan
        else:
            magnitude = math.hypot(a,b)
        result_u.append(a); result_v.append(b); speed.append(magnitude)
    if sys.byteorder != 'little':
        result_u.byteswap();result_v.byteswap();speed.byteswap()
    logging.info('%s normalized 0–%d m shear components to m/s from %s (median %.5g)',
                 model, depth_m, unit, median)
    return result_u.tobytes(), result_v.tobytes(), speed.tobytes()


def fill_missing_300_wind(store,manifest,level):
    """Derive wind speed from paired existing 300-hPa U/V, including repairs."""
    added=[]
    a=store/'u300';b=store/'v300'
    if not a.is_dir() or not b.is_dir():return added
    for candidate in a.glob('*.0.0'):
        h=candidate.name.split('.')[0]
        if not h.isdigit() or not (b/candidate.name).is_file():continue
        dest=store/'wspd300'/candidate.name
        if dest.is_file():continue
        try:
            u=zlib.decompress(candidate.read_bytes())
            v=zlib.decompress((b/candidate.name).read_bytes())
            if len(u)!=core.FIELD_BYTES or len(v)!=core.FIELD_BYTES:continue
            core.write_compressed_chunk(dest,decode_wspd(u,v),level)
            rec=manifest.get('forecast_hours',{}).get(f'{int(h):03d}')
            if rec and rec.get('status')=='complete':
                rec['fields']=sorted(set(rec.get('fields',[]))|{'wspd300'})
            added.append(int(h))
        except (ValueError,zlib.error,OSError) as exc:
            logging.warning('Unable to derive %s F%s wind speed: %s',store.name,h,exc)
    return added


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


def _process_job_impl(args):
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
    moisture_raw = {}
    count = 0
    product_info = {}
    try:
        if is_profile_shear_model(model) and repair_fields==('shear01',):
            valid=write_derived_profile_shear(store,fhr,{},level)
            return {'model':model,'fhr':fhr,'fields':list(PROFILE_SHEAR_OUTPUTS),
                    'selections':{name:{'product':'derived','derivation':PROFILE_SHEAR_SOURCE,
                        'source_fields':list(PROFILE_SHEAR_INPUTS)} for name in PROFILE_SHEAR_OUTPUTS},
                    'products':{},'pending':{},'status':'complete','count':0,
                    'repair_only':True,'repair_kind':repair_kind,
                    'shear01_revision':PROFILE_SHEAR_REVISION,
                    'shear01_orientation':'derived upper-minus-lower',
                    'shear01_valid_points':valid}
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
            if name in ('wspd10','wspd300','mxuphl_run','shear01','shear06'):
                continue
            if name=='psfc' and not (is_profile_shear_model(model) or model=='hrrr'):continue
            if is_profile_shear_model(model) and name in ('shru01','shrv01'):
                continue
            if name == 'mslp':
                hit = select_mslp_record({product: rows for product,(_,_,rows) in inventory.items()}, model)
                if hit is not None:
                    chosen[name] = (hit[0], hit[1], None)
                continue
            for product, (_, _, rows) in inventory.items():
                if model=='refs_mean' and name in ('refc','apcp') and product!='pmmn':
                    continue
                rec = choose_field(rows, name, fhr)
                if rec is not None:
                    interval = parse_acc_interval(rec['text'], fhr) if name in ACCUMULATIONS else None
                    # An explicitly non-zero start needs interval-to-date reconstruction.
                    chosen[name] = (product, rec, interval)
                    break
        # During a targeted upper-air repair, the native dewpoint record can
        # be absent even though valid Q and TMP for the SAME hour/level are
        # already present in this Zarr store. Derive directly from cached
        # chunks without re-downloading or incorrectly reporting no match.
        # Existing completed fields are never overwritten.
        if not chosen and repair_fields is not None and repair_kind=='upper_air':
            from upper_air_moisture import derive_dewpoint_bytes
            repaired=[]
            for pressure in (700,850,925):
                name='td{}'.format(pressure)
                if name not in repair_fields or (store/name/'{}.0.0'.format(fhr)).is_file():
                    continue
                qfile=store/'q{}'.format(pressure)/'{}.0.0'.format(fhr)
                tfile=store/'t{}'.format(pressure)/'{}.0.0'.format(fhr)
                if not qfile.is_file() or not tfile.is_file():continue
                raw=derive_dewpoint_bytes(zlib.decompress(qfile.read_bytes()),
                                          zlib.decompress(tfile.read_bytes()),pressure)
                core.write_compressed_chunk(store/name/'{}.0.0'.format(fhr),raw,level)
                repaired.append(name)
            if repaired:
                return {'model':model,'fhr':fhr,'fields':repaired,'count':0,
                        'selections':{name:{'product':'derived',
                         'derivation':'dewpoint_from_SPFH_and_TMP',
                         'source_fields':[name.replace('td','q'),name.replace('td','t')]} for name in repaired},
                        'pending':{},'products':{},'status':'complete',
                        'repair_only':True,'repair_kind':repair_kind}
        if strict and not chosen:
            raise RuntimeError('No requested fields in available GRIB inventories')
        if not chosen:
            # Incremental missing-field checks revisit hours that were ALREADY
            # processed.  A source can be available but have none of the
            # remaining optional parameters (e.g. CAPE at F000). This
            # is not evidence the original forecast hour is unavailable.
            # Preserve the completed-hour flag, data chunks and manifest.
            # Keep the normal retry interval for fields that might appear later.
            reason = ('No available GRIB products' if not inventory else
                      'Available products contain none of the requested fields')
            return {'model': model, 'fhr': fhr,
                    'status': 'repair_no_match' if repair_fields is not None else 'unavailable',
                    'reason': reason, 'missing_products': missing_products,
                    'inventory_products': sorted(inventory),
                    'requested_fields': list(repair_fields or ()),
                    'fields': [], 'count': 0}
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
                    if isinstance(interval, tuple) and (interval[0] > 0 or
                            interval[1] != fhr or interval[0] < 0 or interval[0] > interval[1]):
                        # Never write a nonzero-start or mismatched interval
                        # into a 0-F cumulative Zarr chunk.
                        staged = store / '.accum_intervals' / name / f'{fhr}.zlib'
                        core.write_compressed_chunk(staged, raw, level)
                        pending[name] = [interval[0], interval[1]]
                    elif interval == 'unknown':
                        # Unknown aggregation range: never falsely label as run total.
                        staged = store / '.accum_intervals' / name / f'{fhr}.zlib'
                        core.write_compressed_chunk(staged, raw, level)
                        pending[name] = 'unknown'
                    elif name not in SHEAR_COMPONENTS:
                        # Native VUCSH/VVCSH are vertical gradients (s^-1),
                        # not the m/s vectors the viewer needs for wind barbs.
                        # Commit these only after both components agree on scale.
                        core.write_compressed_chunk(store / name / f'{fhr}.0.0', raw, level)
                    if name in ('q700','q850','q925','t700','t850','t925','td700','td850','td925'):
                        moisture_raw[name] = raw
                    if name in core.PTYPE_INPUTS and model != 'refs_mean':
                        raw_categories[name] = raw
                    if name in ('u10','v10','u925','v925','u850','v850','u300','v300',*SHEAR_COMPONENTS):
                        wind_raw[name] = raw
                    count += 1
            product_info[product] = {'url':url,'idx_url':idxurl,'selected': len(requests)}
            binary.unlink(missing_ok=True)
            if not keep_grib:
                partial.unlink(missing_ok=True)
        # Native DPT takes precedence. A missing dewpoint can be derived from
        # colocated temperature and specific humidity, never from an unrelated
        # model level or another forecast hour.
        from upper_air_moisture import derive_dewpoint_bytes
        derived_moisture = []
        for pressure in (700, 850, 925):
            output=f'td{pressure}'
            if output in chosen or output in moisture_raw:
                continue
            if repair_fields is not None and output not in repair_fields:
                continue
            q=moisture_raw.get(f'q{pressure}')
            t=moisture_raw.get(f't{pressure}')
            if q is None:
                cached=store/f'q{pressure}'/f'{fhr}.0.0'
                if cached.is_file():q=zlib.decompress(cached.read_bytes())
            if t is None:
                cached=store/f't{pressure}'/f'{fhr}.0.0'
                if cached.is_file():t=zlib.decompress(cached.read_bytes())
            if q is None or t is None:continue
            result=derive_dewpoint_bytes(q,t,pressure)
            core.write_compressed_chunk(store/output/f'{fhr}.0.0',result,level)
            derived_moisture.append(output)
        if all(k in raw_categories for k in core.PTYPE_INPUTS):
            core.write_compressed_chunk(store / 'ptype' / f'{fhr}.0.0',
                                        core.derive_ptype(raw_categories), level)
        if all(k in wind_raw for k in ('u10','v10')):
            core.write_compressed_chunk(store / 'wspd10' / f'{fhr}.0.0',
                                        decode_wspd(wind_raw['u10'],wind_raw['v10']), level)
        if all(k in wind_raw for k in ('u300','v300')):
            core.write_compressed_chunk(store/'wspd300'/f'{fhr}.0.0',
                                        decode_wspd(wind_raw['u300'],wind_raw['v300']),level)
        direction_status='not checked'
        nvalid=None
        if is_profile_shear_model(model):
            try:
                nvalid=write_derived_profile_shear(store,fhr,wind_raw,level)
                direction_status='derived upper-minus-lower'
            except (OSError,ValueError,zlib.error) as exc:
                if repair_fields is not None:
                    raise  # targeted repair must report failures, never silently keep suspect shear
                logging.warning('%s F%03d: derived 0-1 km shear unavailable: %s',model,fhr,exc)
                direction_status='unavailable (no valid terrain-relative profile)'

        for suffix in ('01','06'):
            if suffix=='01' and is_profile_shear_model(model):continue
            u,v='shru'+suffix,'shrv'+suffix
            if u in wind_raw and v in wind_raw:
                u_bytes,v_bytes,mag_bytes=normalize_bulk_shear_pair(
                    wind_raw[u],wind_raw[v],1000 if suffix=='01' else 6000,model)
                for name,data in ((u,u_bytes),(v,v_bytes),('shear'+suffix,mag_bytes)):
                    core.write_compressed_chunk(store/name/f'{fhr}.0.0',data,level)
        selections={name:{'product':product,'inventory':rec['text'],
                          'interval':list(interval) if isinstance(interval,tuple) else interval}
                    for name,(product,rec,interval) in chosen.items()}
        for name in derived_moisture:
            selections[name]={'product':'derived','derivation':'dewpoint_from_SPFH_and_TMP','source_fields':[name.replace('td','q'),name.replace('td','t')]}
        if is_profile_shear_model(model) and nvalid:
            for name in PROFILE_SHEAR_OUTPUTS:
                selections[name]={'product':'derived','derivation':PROFILE_SHEAR_SOURCE,
                                  'source_fields':list(PROFILE_SHEAR_INPUTS)}
        return {'model':model,'fhr':fhr,
                'fields':sorted(set(chosen)|set(derived_moisture)|set(PROFILE_SHEAR_OUTPUTS if nvalid else ())),
                'selections':selections,
                'pending':pending,'products':product_info,'status':'complete',
                'missing_products': missing_products,'count':count,
                'repair_only': bool(repair_fields), 'repair_kind': repair_kind,
                'shear01_revision':PROFILE_SHEAR_REVISION if nvalid else
                                  4 if all(k in wind_raw for k in ('shru01','shrv01')) else None,
                'shear01_valid_points':nvalid,
                'shear01_orientation':direction_status,
                'shear06_revision':4 if all(k in wind_raw for k in ('shru06','shrv06')) else None}
    except Exception as exc:
        return {'model':model,'fhr':fhr,'status':'failed','error':str(exc),
                'fields':sorted(chosen),'pending':pending}
    finally:
        if not keep_grib:
            shutil.rmtree(work, ignore_errors=True)


def process_job(args):
    """Timed process-pool wrapper; preserves the original worker contract."""
    model, date, cycle, fhr = args[0], args[1], args[2], args[3]
    started = time.monotonic()
    before = perf_monitor.metric_snapshot()
    status = 'complete'
    task_id = '%s-%s-%s-%s' % (os.getpid(), model, date, fhr)
    if len(args) > 15:
        perf_monitor.record_event('task_queue', category='forecast_hour',
            queue_wait_seconds=round(max(0.0, time.time() - float(args[15])), 4))
    perf_monitor.record_event('task_start', category='forecast_hour',
        label='%s %s%s F%03d' % (model, date, cycle, fhr), task_id=task_id)
    try:
        result = _process_job_impl(args)
        status = result.get('status', 'complete') if isinstance(result, dict) else 'complete'
        return result
    except BaseException:
        status = 'failed'
        raise
    finally:
        perf_monitor.record_event('task', category='forecast_hour', task_id=task_id,
            label='%s %s%s F%03d' % (model, date, cycle, fhr),
            elapsed_seconds=round(time.monotonic() - started, 4), status=status,
            metrics=perf_monitor.metric_delta(before))


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


def derive_mean_ptype(raw_categories):
    """Dominant class from REFS mean categorical fractions, NOT deterministic PTYPE.

    Codes: 0 none/unknown, 1 rain, 2 snow, 3 freezing rain, 4 ice pellets,
    5 mixed (largest two fractions within 0.05).  Threshold 0.1 filters very
    weak signals; values outside [0,1] are marked missing/unknown.  Missing
    categorical inputs must never be silently treated as zero.
    """
    order=('crain','csnow','cfrzr','cicep')
    try:
        import numpy as np
    except ImportError:
        np=None
    if np is not None:
        vec=[np.frombuffer(raw_categories[name],dtype='<f4') for name in order]
        if any(len(v)!=core.NPTS for v in vec):
            raise ValueError('Unexpected REFS mean categorical byte count')
        # A four-array stack is bounded (~31 MB for the operational CONUS grid).
        m=np.stack(vec,axis=0)
        valid=np.all(np.isfinite(m)&(m>=0)&(m<=1.001),axis=0)
        best=np.argmax(m,axis=0)
        largest=np.max(m,axis=0)
        second=np.partition(m,2,axis=0)[2]
        codes=np.where(valid & (largest>=.1),best+1,0).astype('u1')
        codes=np.where(valid & (largest>=.1) & ((largest-second)<=.05),5,codes).astype('u1')
        return codes.tobytes()
    vectors=[]
    for name in order:
        a=array('f');a.frombytes(raw_categories[name])
        if sys.byteorder!='little':a.byteswap()
        if len(a)!=core.NPTS:raise ValueError('Unexpected REFS mean categorical byte count')
        vectors.append(a)
    codes=bytearray(core.NPTS)
    for i in range(core.NPTS):
        values=[v[i] for v in vectors]
        if not all(math.isfinite(v) and 0<=v<=1.001 for v in values):continue
        largest=max(values)
        if largest<.1:continue
        sorted_values=sorted(values,reverse=True)
        codes[i]=5 if sorted_values[0]-sorted_values[1]<=.05 else values.index(largest)+1
    return bytes(codes)


def repair_mean_ptype(store, processed, level):
    """Build only missing ptype chunks from existing model mean categories."""
    result=[]
    for h,done in enumerate(processed):
        if done!=1:continue
        output=store/'ptype'/f'{h}.0.0'
        if output.is_file():continue
        paths={name:store/name/f'{h}.0.0' for name in core.PTYPE_INPUTS}
        if not all(path.is_file() for path in paths.values()):continue
        raw={name:zlib.decompress(path.read_bytes()) for name,path in paths.items()}
        derived=derive_mean_ptype(raw)
        core.write_compressed_chunk(output,derived,level)
        result.append(h)
    return result


def derive_run_max_uphl(store, manifest, level):
    """Write a maximum *over time* from F001 through each lead hour.

    Requires EVERY 1-hour source chunk since initialization; after a gap,
    no subsequent run-max is published (even if later hours are available).
    Within a chunk, invalid input at any hour leaves that pixel unavailable.
    Returns True when the on-disk run-maximum chunks have changed.
    """
    match = STORES_RE.match(store.name)
    if not match:
        return False
    model = match.group(1)
    _, last = hour_bounds(model)
    source = store / 'mxuphl'
    output = store / 'mxuphl_run'
    output.mkdir(parents=True, exist_ok=True)
    maxvals = None
    changed = False
    first_missing = False
    recompute = False
    record_map = manifest.get('forecast_hours', {})
    for fhr in range(1, last + 1):
        dest = output / f'{fhr}.0.0'
        chunk = source / f'{fhr}.0.0'
        inventory = (record_map.get(f'{fhr:03d}', {})
                     .get('selections', {}).get('mxuphl', {}).get('inventory', ''))
        # In old stores the MXUPHL selection may have been an arbitrary
        # period maximum. Require the recorded, validated hourly provenance.
        provenance_ok = is_one_hour_max(inventory, fhr)
        if first_missing or not chunk.is_file() or not provenance_ok:
            first_missing = True
            if dest.is_file():
                dest.unlink()
                changed = True
            continue
        try:
            # Existing derived chunks are reusable only if every earlier hour
            # was valid and no upstream input has since been rewritten.
            if (not recompute and dest.is_file()
                    and dest.stat().st_mtime_ns >= chunk.stat().st_mtime_ns):
                raw_previous = zlib.decompress(dest.read_bytes())
                if len(raw_previous) == core.FIELD_BYTES:
                    maxvals = array('f')
                    maxvals.frombytes(raw_previous)
                    if sys.byteorder != 'little':
                        maxvals.byteswap()
                    continue
            recompute = True
            raw = zlib.decompress(chunk.read_bytes())
            if len(raw) != core.FIELD_BYTES:
                raise ValueError('incorrect MXUPHL chunk length')
            values = array('f')
            values.frombytes(raw)
            if sys.byteorder != 'little':
                values.byteswap()
            if maxvals is None:
                maxvals = values
                for i, value in enumerate(maxvals):
                    if not (math.isfinite(value) and value > -1e9):
                        maxvals[i] = math.nan
            else:
                for i, value in enumerate(values):
                    before = maxvals[i]
                    if not (math.isfinite(value) and value > -1e9):
                        maxvals[i] = math.nan
                    elif math.isfinite(before):
                        maxvals[i] = max(before, value)
            if sys.byteorder != 'little':
                maxvals.byteswap()
            out_raw = maxvals.tobytes()
            if sys.byteorder != 'little':
                maxvals.byteswap()
            if not dest.is_file() or zlib.decompress(dest.read_bytes()) != out_raw:
                core.write_compressed_chunk(dest, out_raw, level)
                changed = True
        except (OSError, ValueError, zlib.error) as exc:
            logging.warning('%s F%03d: cannot derive run max: %s', store.name, fhr, exc)
            first_missing = True
            if dest.is_file():
                dest.unlink()
                changed = True
    # Never publish F000 as an hourly or accumulated-maximum UH product.
    zero = output / '0.0.0'
    if zero.exists():
        zero.unlink()
        changed = True
    return changed


def restore_staged_snowfall(store, manifest, level, hours):
    """Recover missing ASNOW chunks from previously staged GRIB data.

    Earlier converters staged legitimate UPP '0-1 day acc' records as
    'unknown'.  An explicit original inventory interval is required to
    reinterpret each chunk; staged bytes are not assumed cumulative.
    Only processed forecast hours in the requested hour subset are used
    by the caller.  Already present ASNOW chunks are never overwritten.
    """
    recovered=[]
    for h in sorted(set(hours)):
        chunk=store/'asnow'/f'{h}.0.0'
        staged=store/'.accum_intervals'/'asnow'/f'{h}.zlib'
        if chunk.is_file() or not staged.is_file():
            continue
        record=manifest.get('forecast_hours',{}).get(f'{h:03d}',{})
        inventory=record.get('selections',{}).get('asnow',{}).get('inventory','')
        if not re.search(r':ASNOW:',inventory,re.I):
            logging.warning('%s F%03d ASNOW: staged data has no trustworthy inventory; retry from source',store.name,h)
            continue
        interval=parse_acc_interval(inventory,h)
        if not isinstance(interval,tuple) or interval[1]!=h or not 0<=interval[0]<=h:
            logging.warning('%s F%03d ASNOW: cannot verify staged interval %s; retry from source',store.name,h,interval)
            continue
        start=interval[0]
        predecessor=store/'asnow'/f'{start}.0.0'
        if start and not predecessor.is_file():
            # Leave this for an indexed source retry; never invent the
            # missing cumulative predecessor or replace it with zero.
            continue
        try:
            raw=zlib.decompress(staged.read_bytes())
            if len(raw)!=core.FIELD_BYTES:
                raise ValueError('incorrect staged snowfall byte count')
            total=(raw if start==0 else add_float_arrays(zlib.decompress(predecessor.read_bytes()),raw))
            core.write_compressed_chunk(chunk,total,level)
        except (OSError,ValueError,zlib.error) as exc:
            logging.warning('%s F%03d ASNOW: local recovery failed: %s',store.name,h,exc)
            continue
        selection=record.get('selections',{}).get('asnow')
        if selection is not None:
            selection['interval']=list(interval)
        record.get('pending',{}).pop('asnow',None)
        record['fields']=sorted(set(record.get('fields',[]))|{'asnow'})
        record['repair_snowfall_restored']='from verified staged GRIB interval'
        recovered.append(h)
    return recovered


def finalize_accums(store, manifest, level, warn_unknown=True):
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
                if warn_unknown:
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


# Retention exceptions are defined once locally and passed explicitly to
# the remote server, so both sides make identical deletion/catalog decisions.
DEFAULT_PROTECTED_CYCLES_FILE = Path(__file__).with_name('protected_cycles.json')


def load_protected_cycles(path):
    """Validate the shared model/analysis whitelist and return protected model stores."""
    from retention_policy import load_policy
    policy = load_policy(path)
    logging.info('Loaded %d protected model stores and %d analysis intervals from %s',
                 len(policy.model_stores), len(policy.analysis_rules), path)
    return policy.model_stores


def is_protected(protected_stores, date, cycle, model=None):
    if model is None:
        return any(f'{name}_{date}_{cycle}00.zarr' in protected_stores for name in MODELS)
    return f'{model}_{date}_{cycle}00.zarr' in protected_stores


def scan_stores(output_root, retention_days=None, as_of=None, protected_stores=(), only_stores=None):
    """Construct viewer catalogs, optionally excluding cycles beyond retention."""
    now = as_of or dt.datetime.now(dt.timezone.utc)
    cutoff = (now - dt.timedelta(days=retention_days)) if retention_days else None
    grouped = {m:[] for m in MODELS}
    if only_stores is not None:
        # Read the existing catalog as a cache of unchanged, fully processed
        # stores. Only rescan the new/modified stores for this cycle; a full
        # archive scan can contain hundreds of thousands of files.
        cached={}
        for model in MODELS:
            catalog=Path(output_root)/f'catalog_{model}.json'
            if not catalog.is_file():
                only_stores=None
                break
            try:cached[model]=json.loads(catalog.read_text())['datasets']
            except (OSError,ValueError,KeyError,TypeError):
                only_stores=None
                break
        if only_stores is not None:
            selected={Path(p).stem for p in only_stores}
            for model,entries in cached.items():
                for entry in entries:
                    name=entry.get('path','')
                    match=STORES_RE.fullmatch(name)
                    if not match or entry.get('id') in selected:
                        continue
                    _,date,cycle=match.groups()
                    if not (Path(output_root)/name).is_dir():continue
                    if cutoff is not None and iso_init(date,cycle)<cutoff and not is_protected(protected_stores,date,cycle,model):
                        continue
                    grouped[model].append({**entry, 'is_case': is_protected(protected_stores,date,cycle,model)})
    to_scan=(sorted(Path(output_root).glob('*.zarr')) if only_stores is None
             else sorted(map(Path,only_stores)))
    for store in to_scan:
        m = STORES_RE.fullmatch(store.name)
        if not m or not store.is_dir():
            continue
        model, date, cycle = m.groups()
        if cutoff is not None and iso_init(date, cycle) < cutoff and not is_protected(protected_stores, date, cycle, model):
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
                                       'variables':variables,
                                       'is_case': is_protected(protected_stores,date,cycle,model)})
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


def _published_files_snapshot(store):
    """A cheap, persistent snapshot used to identify newly written Zarr chunks."""
    result={}
    for path in store.rglob('*'):
        if not path.is_file(): continue
        rel=path.relative_to(store)
        if any(part in ('.accum_intervals','.legacy_mean_apcp') for part in rel.parts):
            continue
        st=path.stat()
        result[rel.as_posix()]=[st.st_size,st.st_mtime_ns]
    return result


@perf_monitor.timed_task('publish_store_delta', lambda a,k: a[0].name, include_metrics=False)
def _publish_delta(store, catalog, work_root, host, remote_dir, keep_archive=False,
                   publish_mode='auto'):
    """Full bootstrap, then transfer only changed Zarr chunks and atomic catalog.

    The record is written ONLY after a successful remote SSH extraction. If
    interrupted, a retry will resend the same files. Version 1.0.x has no
    publish-state file, so the first publish after upgrading is a full upload.
    """
    state_dir=work_root/'.published'
    state_file=state_dir/f'{store.name}.json'
    current=_published_files_snapshot(store)
    previous=None
    if publish_mode=='auto' and state_file.is_file():
        try:
            candidate=json.loads(state_file.read_text())
            if candidate.get('schema')==1 and candidate.get('store')==store.name:
                previous=candidate['files']
        except (OSError,ValueError,KeyError,TypeError):pass
    if previous is None:
        publish_one(store,catalog,work_root,host,remote_dir,keep_archive,
                    store_arcname=None,publish_mode='legacy_full')
        state_dir.mkdir(parents=True,exist_ok=True)
        core.json_write(state_file,{'schema':1,'store':store.name,'files':current})
        return
    modified=[name for name,stat in current.items() if previous.get(name)!=stat]
    deleted=[name for name in previous if name not in current]
    if not modified and not deleted:
        logging.info('%s: no changed Zarr files; refreshing catalog only',store.name)
    work_root.mkdir(parents=True,exist_ok=True)
    archive=work_root/f'{store.stem}.delta.tar'
    tmp=archive.with_name(archive.name+'.tmp')
    try:
        with tarfile.open(tmp,'w') as tar:
            for name in sorted(modified):
                tar.add(store/name,arcname=f'{store.name}/{name}',recursive=False)
            if deleted:
                import io
                payload='\n'.join(f'{store.name}/{name}' for name in sorted(deleted))+'\n'
                content=payload.encode('utf-8')
                ti=tarfile.TarInfo('.delta-delete.txt')
                ti.size=len(content);ti.mode=0o600
                tar.addfile(ti,io.BytesIO(content))
            if catalog is not None:
                tar.add(catalog,arcname=catalog.name,recursive=False)
        os.replace(tmp,archive)
        remote_archive=f'.{archive.name}.incoming'
        publish_transport.run(['ssh',host,'mkdir -p '+shlex.quote(remote_dir)],check=True)
        publish_transport.run(['scp',str(archive),f'{host}:{remote_dir}/{remote_archive}'],check=True)
        # Extract into a private staging area, then atomically install each
        # changed file. Publish the catalog last so readers never see future
        # available_hours pointing at chunks that have yet to be transferred.
        script=(
            'set -eu; cd '+shlex.quote(remote_dir)+'; '
            'stage=$(mktemp -d .delta-stage-XXXXXXXX); '
            "trap 'rm -rf \"$stage\"' EXIT HUP INT TERM; "
            'tar -xf '+shlex.quote(remote_archive)+' -C "$stage"; '
            'if [ -f "$stage/.delta-delete.txt" ]; then '
            'while IFS= read -r rel; do '
            'case "$rel" in '+shlex.quote(store.name)+'/[A-Za-z0-9_.-]*/*) rm -f -- "$rel";; '
            '*) echo "Unsafe delta deletion path" >&2; exit 1;; esac; '
            'done < "$stage/.delta-delete.txt"; '
            'rm -f "$stage/.delta-delete.txt"; fi; '
            'if [ -d "$stage/'+store.name+'" ]; then '
            'find "$stage/'+store.name+'" -type f | while IFS= read -r file; do '
            'rel=${file#"$stage"/}; dest=${rel%/*}; mkdir -p "$dest"; '
            'mv -f -- "$file" "$rel"; done; fi; '
            + (('if [ -f "$stage/'+catalog.name+'" ]; then '
                'mv -f "$stage/'+catalog.name+'" '+shlex.quote(catalog.name)+'; fi; ') if catalog else '')
            + 'rm -f '+shlex.quote(remote_archive)+'; '
        )
        publish_transport.run(['ssh',host,script],check=True)
        state_dir.mkdir(parents=True,exist_ok=True)
        core.json_write(state_file,{'schema':1,'store':store.name,'files':current})
        logging.info('Incrementally published %s: %d changed, %d removed Zarr files',
                     store.name,len(modified),len(deleted))
    finally:
        tmp.unlink(missing_ok=True)
        if archive.exists() and not keep_archive and sys.exc_info()[0] is None:
            archive.unlink()


def publish_one(store, catalog, work_root, host, remote_dir, keep_archive=False, store_arcname=None, publish_mode='auto'):
    """Uncompressed TAR: Zarr chunks are already zlib-compressed.

    Publish a model's catalog LAST. Unique archives per cycle prevent collision.
    Existing viewer catalog.json and other sources are untouched.
    """
    if publish_mode in ('auto','full') and store_arcname is None:
        return _publish_delta(store,catalog,Path(work_root),host,remote_dir,keep_archive,publish_mode)
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
                # Reconstruction intermediates are for local processing only, not the viewer.
                if any(marker in t.name for marker in ('/.accum_intervals/', '/.legacy_mean_apcp/')) or t.name.endswith(('/.accum_intervals','/.legacy_mean_apcp')):
                    return None
                if not (t.isfile() or t.isdir()):
                    raise RuntimeError('Refusing non-regular file in published Zarr: '+t.name)
                # Analysis stores originate in tempfile.mkdtemp(), which has
                # mode 0700 by default.  Without normalizing TAR entry modes,
                # Apache cannot traverse the published .zarr directory and
                # browser chunk GETs fail with HTTP 403.  Apply only to web
                # analysis artifacts, not private processing work directories.
                if store_arcname and store_arcname.startswith(('analysis/', 'aligned/')):
                    t.mode = 0o755 if t.isdir() else 0o644
                return t
            f.add(store, arcname=store_arcname or store.name,recursive=True,filter=safe_filter)
            if catalog is not None:
                f.add(catalog,arcname=catalog.name,recursive=False,filter=safe_filter)
        os.replace(tmp,archive)
        remote_archive=f'.{archive.name}.incoming'
        publish_transport.run(['ssh',host,'mkdir -p '+shlex.quote(remote_dir)],check=True)
        publish_transport.run(['scp',str(archive),f'{host}:{remote_dir}/{remote_archive}'],check=True)
        command=(f'set -eu; cd {shlex.quote(remote_dir)}; '
                 f'tar -xf {shlex.quote(remote_archive)}; '
                 f'rm -f {shlex.quote(remote_archive)}')
        publish_transport.run(['ssh',host,command],check=True)
        logging.info('Published %s%s to %s',store.name,(' and '+catalog.name) if catalog else '',host)
    finally:
        tmp.unlink(missing_ok=True)
        if archive.is_file() and not keep_archive:
            # On failure retain archive to allow a manual retry.
            if sys.exc_info()[0] is None:
                archive.unlink()


# This is executed on the remote web server through SSH. It deliberately cannot remove
# arbitrary directory names, follows no store symlinks, and never touches
# the viewer's JS, CSS, static geography or vendor folders.
REMOTE_RETENTION_SCRIPT = r'''
from pathlib import Path
import datetime as dt
import json, os, re, shutil, sys

root = Path(sys.argv[1])
# The remote login-shell python3 may be Python 3.6, which does not have
# datetime.fromisoformat() (introduced in 3.7). Use the UTC timestamp
# format transmitted by the processing host and parse it with Python 3.6-compatible
# datetime.strptime(). Reject unexpected formats before any deletion.
cutoff_text = sys.argv[2]
if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z', cutoff_text):
    raise SystemExit('Refusing unexpected UTC retention cutoff: ' + cutoff_text)
cutoff_body = cutoff_text[:-1]
cutoff_format = '%Y-%m-%dT%H:%M:%S.%f' if '.' in cutoff_body else '%Y-%m-%dT%H:%M:%S'
cutoff = dt.datetime.strptime(cutoff_body, cutoff_format).replace(tzinfo=dt.timezone.utc)
# Optional separate observation/GFS cutoffs. Old direct callers remain compatible.
def parse_cutoff(value):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z', value):
        raise SystemExit('Invalid retention cutoff')
    body=value[:-1]
    return dt.datetime.strptime(body, '%Y-%m-%dT%H:%M:%S.%f' if '.' in body else '%Y-%m-%dT%H:%M:%S').replace(tzinfo=dt.timezone.utc)
obs_cutoff=parse_cutoff(sys.argv[7]) if len(sys.argv)>7 else cutoff
gfs_cutoff=parse_cutoff(sys.argv[8]) if len(sys.argv)>8 else cutoff
dry_run = sys.argv[3] == '1'
# Generated exclusively from validated local configuration; never accepts paths.
try:
    protected_names = json.loads(sys.argv[4])
    if (not isinstance(protected_names, list) or
        any(not isinstance(name, str) or
            not re.fullmatch(r'(rrfs|refs_mean|refs_m00[1-5]|hrrr|gfs_v16|gfs_v17)_\d{8}_(00|06|12|18)00\.zarr', name)
            for name in protected_names)):
        raise ValueError('Invalid protected model name')
    protected = set(protected_names)
except (IndexError, TypeError, ValueError) as exc:
    raise SystemExit('Refusing unsafe protected-cycle configuration: ' + str(exc))
try:
    obs_rules = json.loads(sys.argv[5])
    if not isinstance(obs_rules,list) or any(
        not isinstance(r,list) or len(r)!=2 or
        not all(isinstance(s,str) and re.fullmatch(r'\d{10}',s) for s in r) or r[0]>r[1]
        for r in obs_rules):
        raise ValueError('Invalid protected observation windows')
except (IndexError,TypeError,ValueError) as exc:
    raise SystemExit('Unsafe observation protection configuration: '+str(exc))
if not root.is_absolute() or root.name != 'data' or root.is_symlink() or not root.is_dir():
    raise SystemExit('Refusing unsafe or nonexistent remote data directory: ' + str(root))
# Source-specific analysis rules are required to protect native/aligned analyses
# and RAP analysis soundings. Older standalone callers may omit this argument,
# in which case the auxiliary sweep is disabled rather than risking case data.
products={'mrms_refc','mrms_prate','mrms_apcp', 'mrms_rotation_ml60','stageiv_apcp','rap_surface',
          'rap_pressure','urma','rtma','nohrsc_6h','nohrsc_24h',
          'nohrsc_48h','nohrsc_72h','ccpa_3h','ccpa_6h','gfs_nohrsc_6h','gfs_nohrsc_24h','gfs_ccpa_3h','gfs_ccpa_6h','gfs_stageiv_apcp','gfs_mrms_apcp','gfs_mrms_prate'}
aux_enabled=len(sys.argv)>6
try:
    analysis_rules=json.loads(sys.argv[6]) if aux_enabled else []
    if not isinstance(analysis_rules,list):raise ValueError('Expected analysis rule list')
    for item in analysis_rules:
        if not isinstance(item,list) or len(item)!=3:raise ValueError('Invalid interval')
        a,b,names=item
        if not isinstance(a,str) or not isinstance(b,str) or not re.fullmatch(r'\d{10}',a) or not re.fullmatch(r'\d{10}',b):
            raise ValueError('Invalid analysis hours')
        start=dt.datetime.strptime(a,'%Y%m%d%H');end=dt.datetime.strptime(b,'%Y%m%d%H')
        if end<start or end-start>dt.timedelta(days=366):raise ValueError('Invalid interval span')
        if not isinstance(names,list) or not names or any(n not in products for n in names):
            raise ValueError('Invalid analysis product list')
except (IndexError,TypeError,ValueError) as exc:
    raise SystemExit('Refusing unsafe protected-analysis configuration: '+str(exc))
def protects_product(name,hour):
    return any(a<=hour<=b and name in names for a,b,names in analysis_rules)
# No inferred paths: all auxiliary Zarr names have an exact regex and a
# timestamp read from their name, NEVER from directory mtime.
analysis_re=re.compile(r'^analysis_(mrms_refc|mrms_prate|mrms_apcp|mrms_rotation_ml60|stageiv_apcp|rap_surface|rap_pressure|urma|rtma|nohrsc_6h|nohrsc_24h|nohrsc_48h|nohrsc_72h|ccpa_3h|ccpa_6h|gfs_nohrsc_6h|gfs_nohrsc_24h|gfs_ccpa_3h|gfs_ccpa_6h|gfs_stageiv_apcp|gfs_mrms_apcp|gfs_mrms_prate)_(\d{10})\.zarr$')
profile_re=re.compile(r'^sounding_(rrfs|refs_mean|refs_m00[1-5]|hrrr|rap_analysis|gfs_v16|gfs_v17)_(\d{8})_(\d{2})00\.zarr$')
def auxiliary_expired(name,kind):
    match=(profile_re if kind=='profiles' else analysis_re).fullmatch(name)
    if not match:return False
    try:
        if kind=='profiles':
            model,date,cycle=match.groups()
            timestamp=dt.datetime.strptime(date+cycle,'%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
            if timestamp >= (obs_cutoff if model=='rap_analysis' else gfs_cutoff if model.startswith('gfs_') else cutoff):return False
            if model=='rap_analysis':return not protects_product('rap_pressure',date+cycle)
            return model+'_'+date+'_'+cycle+'00.zarr' not in protected
        product,hour=match.groups()
        timestamp=dt.datetime.strptime(hour,'%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
        return timestamp<obs_cutoff and not protects_product(product,hour)
    except ValueError:
        return False
# Preflight all auxiliary catalogs before any model/observation deletion.
# A missing catalog is allowed (first publish), but an unsafe one is not.
aux_specs=(('analysis','catalog_analysis.json','analysis'),
           ('aligned','catalog_aligned_analysis.json','aligned'),
           ('profiles','catalog_profiles.json',''))
aux_catalogs=[]
if aux_enabled:
    for kind,catname,prefix in aux_specs:
        cat=root/catname
        if cat.is_symlink():raise SystemExit('Unsafe auxiliary catalog symlink: '+str(cat))
        if not cat.exists():continue
        if not cat.is_file():raise SystemExit('Unsafe auxiliary catalog: '+str(cat))
        try:obj=json.loads(cat.read_text(encoding='utf-8'))
        except Exception as exc:raise SystemExit('Cannot inspect auxiliary catalog '+str(cat)+': '+str(exc))
        if not isinstance(obj,dict) or not isinstance(obj.get('datasets'),list):
            raise SystemExit('Invalid auxiliary catalog: '+str(cat))
        aux_catalogs.append((kind,cat,prefix,obj))
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
pruned_observations = 0
for child in sorted(root.iterdir()):
    match = pattern.fullmatch(child.name)
    if not match or child.is_symlink() or not child.is_dir():
        continue
    try:
        init = dt.datetime.strptime(match.group(2) + match.group(3), '%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
    except ValueError:
        continue
    if init >= cutoff or child.name in protected:
        continue
    print(('WOULD REMOVE ' if dry_run else 'REMOVE ') + str(child), flush=True)
    pruned.add(child.name)
    if not dry_run:
        shutil.rmtree(child)
# Observation JSON and abandoned transfer archives have their own strict
# allow-lists. Never touch protected valid hours or recently modified files.
obs_dir = root / 'ptype_observations'
if obs_dir.is_dir() and not obs_dir.is_symlink():
    for item in sorted(obs_dir.iterdir()):
        if item.is_symlink() or not item.is_file(): continue
        m = re.fullmatch(r'(\d{10})\.json',item.name)
        if not m:continue
        try: valid=dt.datetime.strptime(m.group(1),'%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
        except ValueError:continue
        if valid >= obs_cutoff or any(a<=m.group(1)<=b for a,b in obs_rules):continue
        print(('WOULD REMOVE ' if dry_run else 'REMOVE ')+str(item),flush=True)
        pruned_observations += 1
        if not dry_run:item.unlink()
# SPC report days run from 12 UTC to 12 UTC. Retain any SPC day
# overlapping a protected model/analysis valid-time range.
spc_dir = root / 'spc_reports'
if spc_dir.is_dir() and not spc_dir.is_symlink():
    for item in sorted(spc_dir.iterdir()):
        if item.is_symlink() or not item.is_file():continue
        m = re.fullmatch(r'(\d{6})\.json',item.name)
        if not m:continue
        try:begin=dt.datetime.strptime(m.group(1),'%y%m%d').replace(tzinfo=dt.timezone.utc,hour=12)
        except ValueError:continue
        end=begin+dt.timedelta(days=1)
        if end>=obs_cutoff or any(
                begin < dt.datetime.strptime(b,'%Y%m%d%H').replace(tzinfo=dt.timezone.utc)+dt.timedelta(hours=1)
                and end > dt.datetime.strptime(a,'%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
                for a,b in obs_rules):continue
        print(('WOULD REMOVE ' if dry_run else 'REMOVE ')+str(item),flush=True)
        pruned_observations += 1
        if not dry_run:item.unlink()
    for item in sorted(spc_dir.iterdir()):
        if item.is_symlink() or not item.is_file() or not re.fullmatch(r'\.\d{6}\.json\.incoming',item.name):continue
        if dt.datetime.fromtimestamp(item.stat().st_mtime,dt.timezone.utc)>=obs_cutoff:continue
        print(('WOULD REMOVE ' if dry_run else 'REMOVE ')+str(item),flush=True)
        pruned_observations += 1
        if not dry_run:item.unlink()
# GLM lightning is keyed by the selected forecast valid hour. Preserve
# protected case hours; stale publisher temp files are safe to remove by mtime.
lightning_dir = root / 'lightning'
if lightning_dir.is_dir() and not lightning_dir.is_symlink():
    for item in sorted(lightning_dir.iterdir()):
        if item.is_symlink() or not item.is_file(): continue
        m = re.fullmatch(r'(\d{10})\.json', item.name)
        if not m: continue
        try: valid = dt.datetime.strptime(m.group(1), '%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
        except ValueError: continue
        if valid >= obs_cutoff or any(a <= m.group(1) <= b for a, b in obs_rules): continue
        print(('WOULD REMOVE ' if dry_run else 'REMOVE ') + str(item), flush=True)
        pruned_observations += 1
        if not dry_run: item.unlink()
    for item in sorted(lightning_dir.iterdir()):
        if item.is_symlink() or not item.is_file() or not re.fullmatch(r'\.\d{10}\.json\.incoming', item.name): continue
        if dt.datetime.fromtimestamp(item.stat().st_mtime, dt.timezone.utc) >= obs_cutoff: continue
        print(('WOULD REMOVE ' if dry_run else 'REMOVE ') + str(item), flush=True)
        pruned_observations += 1
        if not dry_run: item.unlink()
# Interrupted mPING/METAR uploads may leave .YYYYMMDDHH.json.incoming.
if obs_dir.is_dir() and not obs_dir.is_symlink():
    for item in sorted(obs_dir.iterdir()):
        if item.is_symlink() or not item.is_file() or not re.fullmatch(r'\.\d{10}\.json\.incoming',item.name):continue
        changed=dt.datetime.fromtimestamp(item.stat().st_mtime,dt.timezone.utc)
        if changed>=obs_cutoff:continue
        print(('WOULD REMOVE ' if dry_run else 'REMOVE ')+str(item),flush=True)
        pruned_observations += 1
        if not dry_run:item.unlink()
# Only stale uploader files matching exact names may be pruned. Do not
# remove active/partially transferred files even if their date is old.
archive_re = re.compile(
    r'^\.(?:(?:rrfs|refs_mean|refs_m00[1-5]|hrrr)_\d{8}_(?:00|06|12|18)00(?:\.delta)?'
    r'|analysis_(?:mrms_refc|mrms_prate|mrms_apcp|mrms_rotation_ml60|stageiv_apcp|rap_surface|rap_pressure|urma|rtma|nohrsc_6h|nohrsc_24h|nohrsc_48h|nohrsc_72h|ccpa_3h|ccpa_6h|gfs_nohrsc_6h|gfs_nohrsc_24h|gfs_ccpa_3h|gfs_ccpa_6h|gfs_stageiv_apcp|gfs_mrms_apcp|gfs_mrms_prate)_\d{10}'
    r'|sounding_(?:rrfs|refs_mean|refs_m00[1-5]|hrrr|rap_analysis|gfs_v16|gfs_v17)_\d{8}_\d{2}00)\.tar\.incoming$')
stage_re = re.compile(r'^\.delta-stage-[A-Za-z0-9]{8}$')
for stage in sorted(root.iterdir()):
    if stage.is_symlink() or not stage.is_dir() or not stage_re.fullmatch(stage.name):continue
    modified=dt.datetime.fromtimestamp(stage.stat().st_mtime,dt.timezone.utc)
    if modified>=min(cutoff,obs_cutoff,gfs_cutoff):continue
    print(('WOULD REMOVE ' if dry_run else 'REMOVE ')+str(stage),flush=True)
    if not dry_run:shutil.rmtree(stage)
for item in sorted(root.iterdir()):
    if item.is_symlink() or not item.is_file() or not archive_re.fullmatch(item.name):continue
    modified=dt.datetime.fromtimestamp(item.stat().st_mtime,dt.timezone.utc)
    if modified>=min(cutoff,obs_cutoff,gfs_cutoff):continue
    print(('WOULD REMOVE ' if dry_run else 'REMOVE ')+str(item),flush=True)
    if not dry_run:item.unlink()
# Stale catalog upload/temp files can survive interrupted remote uploads.
# Never touch the live catalogs, and never interrupt a recent transfer.
for item in sorted(root.iterdir()):
    if item.is_symlink() or not item.is_file():continue
    if not (re.fullmatch(r'\.(?:catalog(?:_(?:rrfs|refs_mean|refs_m00[1-5]|hrrr|analysis|aligned_analysis|profiles))?\.json)(?:\.incoming|\.retention\.\d+)',item.name) or re.fullmatch(r'\.catalog_profiles\.json\.(?:[a-f0-9]{32}\.incoming|commit\.[A-Za-z0-9_-]+)',item.name)):continue
    if dt.datetime.fromtimestamp(item.stat().st_mtime,dt.timezone.utc)>=obs_cutoff:continue
    print(('WOULD REMOVE ' if dry_run else 'REMOVE ')+str(item),flush=True)
    if not dry_run:item.unlink()
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
        if (cycle_init >= cutoff or name in protected) and (root / name).is_dir() and not (root / name).is_symlink():
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
# Reconcile source-specific catalogs FIRST, then remove the exact expired
# auxiliary stores. This also catches web-server orphans no longer present in
# the processing directory (which local-deletion-list cleanup cannot find).
aux_counts={}
if aux_enabled:
    for kind,cat,prefix,obj in aux_catalogs:
        entries=obj['datasets'];keep=[]
        for entry in entries:
            path=entry.get('path','') if isinstance(entry,dict) else ''
            name=path[len(prefix)+1:] if prefix and path.startswith(prefix+'/') else path
            valid_path=path==(prefix+'/'+name if prefix else name)
            matches=(profile_re if kind=='profiles' else analysis_re).fullmatch(name)
            if valid_path and matches:
                location = root/path
                if auxiliary_expired(name,kind) or not location.is_dir() or location.is_symlink():
                    continue
            keep.append(entry)
        if len(keep)!=len(entries):
            print(('WOULD PRUNE ' if dry_run else 'PRUNE ')+str(cat)+': '+str(len(entries)-len(keep))+' entries',flush=True)
            if not dry_run:
                obj['datasets']=keep
                temp=cat.with_name('.'+cat.name+'.retention.'+str(os.getpid()))
                try:
                    with temp.open('w',encoding='utf-8') as handle:
                        json.dump(obj,handle,indent=2,sort_keys=True)
                        handle.write('\n')
                    os.replace(temp,cat)
                finally:
                    if temp.exists():temp.unlink()
    for kind,_,prefix in aux_specs:
        folder=root if kind=='profiles' else root/prefix
        if not folder.is_dir() or folder.is_symlink():continue
        count=0
        for store in sorted(folder.iterdir()):
            if store.is_symlink() or not store.is_dir() or not auxiliary_expired(store.name,kind):continue
            print(('WOULD REMOVE ' if dry_run else 'REMOVE ')+str(store),flush=True)
            if not dry_run:shutil.rmtree(store)
            count+=1
        aux_counts[kind]=count
print(('DRY RUN ' if dry_run else '') + 'Retention complete: ' + str(len(pruned)) + ' stores and ' + str(pruned_observations) + ' observation/report files; auxiliary expired stores ' + str(aux_counts), flush=True)
'''


# Local retention uses the same UTC initialization-time cutoff as the remote server.
# Every name must match an exact allow-list; nothing is removed recursively
# unless it is a recognized dated Zarr store or a dated work-cycle directory.
LOCAL_SELECTION_RE = re.compile(
    r'^(rrfs|refs_mean|refs_m00[1-5]|hrrr)_(\d{8})_(00|06|12|18)00\.selection\.json$')
LOCAL_ARCHIVE_RE = re.compile(
    r'^(rrfs|refs_mean|refs_m00[1-5]|hrrr)_(\d{8})_(00|06|12|18)00(?:\.delta)?\.tar(?:\.bz2)?(?:\.tmp)?$')
LOCAL_SUMMARY_RE = re.compile(r'^run_summary_(\d{8})_(00|06|12|18)\.json$')
LOCAL_WORK_CYCLE_RE = re.compile(r'^(\d{8})_(00|06|12|18)$')
LOCAL_LOG_RE = re.compile(r'^(?:multisource|model_incremental|gfs_incremental|cron_errors|model_cron_errors)_(\d{8})_(\d{6})(?:_\d+)?\.log$')
LOCAL_PUBLISH_STATE_RE = re.compile(r'^(rrfs|refs_mean|refs_m00[1-5]|hrrr)_(\d{8})_(00|06|12|18)00\.zarr\.json$')
LOCAL_OBS_RE = re.compile(r'^(\d{10})\.json$')
REMOTE_STALE_ARCHIVE_RE = re.compile(r'^\.(?:rrfs|refs_mean|refs_m00[1-5]|hrrr)_\d{8}_(?:00|06|12|18)00(?:\.delta)?\.tar\.incoming$')
LOCAL_CATALOG_RE = re.compile(r'^catalog(?:_(?:rrfs|refs_mean|refs_m00[1-5]|hrrr))?\.json$')
LOCAL_ANALYSIS_PRODUCTS = ('mrms_refc', 'mrms_prate', 'mrms_apcp', 'mrms_rotation_ml60', 'stageiv_apcp',
                           'rap_surface', 'rap_pressure', 'urma', 'rtma',
                           'nohrsc_6h', 'nohrsc_24h', 'nohrsc_48h', 'nohrsc_72h','ccpa_3h','ccpa_6h','gfs_nohrsc_6h','gfs_nohrsc_24h','gfs_ccpa_3h','gfs_ccpa_6h','gfs_stageiv_apcp','gfs_mrms_apcp','gfs_mrms_prate')
LOCAL_ANALYSIS_RE = re.compile(
    r'^analysis_(' + '|'.join(LOCAL_ANALYSIS_PRODUCTS) + r')_(\d{10})\.zarr$')
LOCAL_PROFILE_RE = re.compile(
    r'^sounding_(rrfs|refs_mean|refs_m00[1-5]|hrrr|rap_analysis|gfs_v16|gfs_v17)_(\d{8})_(\d{2})00\.zarr$')
LOCAL_ANALYSIS_ARCHIVE_RE = re.compile(
    r'^analysis_(' + '|'.join(LOCAL_ANALYSIS_PRODUCTS) + r')_(\d{10})\.tar(?:\.bz2)?(?:\.tmp)?$')
LOCAL_PROFILE_ARCHIVE_RE = re.compile(
    r'^sounding_(rrfs|refs_mean|refs_m00[1-5]|hrrr|rap_analysis|gfs_v16|gfs_v17)_(\d{8})_(\d{2})00\.tar(?:\.bz2)?(?:\.tmp)?$')
LOCAL_AUX_CATALOGS = (('catalog_analysis.json', 'analysis', LOCAL_ANALYSIS_RE),
                      ('catalog_aligned_analysis.json', 'aligned', LOCAL_ANALYSIS_RE),
                      ('catalog_profiles.json', '', LOCAL_PROFILE_RE))


def _safe_local_root(path, label):
    """Reject dangerous directory targets while allowing normal absolute processing paths."""
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
                  now=None, logs_dir=None, protected_stores=(), analysis_rules=(),
                  obs_retention_days=None, gfs_retention_days=None):
    """Expire recognized stores, observations, and work artifacts using UTC time.

    Retention is allow-list based. Nonmatching files, undated legacy work
    directories, symlinks, protected case hours, and current data are preserved.
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
    obs_retention_days = retention_days if obs_retention_days is None else obs_retention_days
    gfs_retention_days = retention_days if gfs_retention_days is None else gfs_retention_days
    if any(not isinstance(n, int) or not 1 <= n <= 366 for n in (obs_retention_days, gfs_retention_days)):
        raise ValueError('Observation and GFS retention must be 1..366 days')
    cutoff = now.astimezone(dt.timezone.utc) - dt.timedelta(days=retention_days)
    obs_cutoff = now.astimezone(dt.timezone.utc) - dt.timedelta(days=obs_retention_days)
    gfs_cutoff = now.astimezone(dt.timezone.utc) - dt.timedelta(days=gfs_retention_days)
    stats = {'stores': 0, 'manifests': 0, 'summaries': 0,
             'archives': 0, 'work_cycles': 0, 'logs': 0, 'catalog_entries': 0,
             'observations': 0, 'glm_lightning': 0, 'publish_states': 0, 'cron_stdout': 0,
             'analysis': 0, 'aligned': 0, 'profiles': 0,
             'staging': 0, 'profile_work': 0, 'case_logs': 0,
             'publication_archives': 0, 'aux_catalog_entries': 0, 'aux_publish_states': 0,
             'performance_logs': 0, 'catalog_temps': 0}

    def eligible(child, match, date_index, cycle_index, model_index=1, fixed_model=None):
        if child.is_symlink():
            return False
        date, cycle = match.group(date_index), match.group(cycle_index)
        init = _cycle_from_parts(date, cycle)
        model = fixed_model if fixed_model is not None else (match.group(model_index) if model_index else None)
        return init is not None and init < cutoff and not is_protected(protected_stores, date, cycle, model)

    def remove(path, category, directory):
        if path.is_symlink() or (not path.is_dir() if directory else not path.is_file()):
            return
        logging.info('Local %s %s', 'WOULD REMOVE' if dry_run else 'REMOVE', path)
        stats[category] += 1
        if not dry_run:
            if directory:
                shutil.rmtree(path)
            else:
                path.unlink()

    # Preflight all recognized local catalogs before deleting any data.
    # This keeps local catalog.json and catalog_MODEL.json coherent, just as
    # the remote retention script does for its catalogs.
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

    # Preflight auxiliary catalogs and publish-state maps *before* deleting
    # anything. A damaged catalog must not cause a partially executed sweep.
    aux_catalogs = []
    aux_states = []
    for name, prefix, pattern in LOCAL_AUX_CATALOGS:
        cat = out / name
        if cat.is_symlink():
            raise RuntimeError(f'Unsafe local auxiliary catalog symlink: {cat}')
        if not cat.exists():
            continue
        if not cat.is_file():
            raise RuntimeError(f'Unsafe local auxiliary catalog: {cat}')
        try:
            obj = json.loads(cat.read_text(encoding='utf-8'))
            if not isinstance(obj, dict) or not isinstance(obj.get('datasets'), list):
                raise ValueError('Expected a catalog with a datasets list')
        except (OSError, ValueError) as exc:
            raise RuntimeError(f'Cannot safely inspect auxiliary catalog {cat}: {exc}') from exc
        aux_catalogs.append((cat, prefix, pattern, obj))
    for name in ('.analysis_publish_state.json', '.aligned_publish_state.json'):
        path = out / name
        if path.is_symlink():
            raise RuntimeError(f'Unsafe local publication state symlink: {path}')
        if not path.exists():
            continue
        try:
            obj = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(obj, dict):
                raise ValueError('Expected a JSON object')
        except (OSError, ValueError) as exc:
            raise RuntimeError(f'Cannot safely inspect publication state {path}: {exc}') from exc
        aux_states.append((path, obj))

    def old_hour(stamp, category='model'):
        try:
            when = dt.datetime.strptime(stamp, '%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
            limit = obs_cutoff if category == 'obs' else gfs_cutoff if category == 'gfs' else cutoff
            return when < limit
        except ValueError:
            return False

    def protected_analysis(product, stamp):
        return any(a <= stamp <= b and product in products
                   for a, b, products in analysis_rules)

    def protected_profile(model, date, hour):
        if model == 'rap_analysis':
            return protected_analysis('rap_pressure', date + hour)
        return f'{model}_{date}_{hour}00.zarr' in protected_stores

    def expired_auxiliary(name, kind):
        if kind == 'profiles':
            match = LOCAL_PROFILE_RE.fullmatch(name)
            if not match:
                return False
            model, date, hour = match.groups()
            return old_hour(date + hour, 'obs' if model == 'rap_analysis' else 'gfs' if model.startswith('gfs_') else 'model') and not protected_profile(model, date, hour)
        match = LOCAL_ANALYSIS_RE.fullmatch(name)
        if not match:
            return False
        product, stamp = match.groups()
        return old_hour(stamp, 'obs') and not protected_analysis(product, stamp)

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
            if match and eligible(child, match, 1, 2, model_index=None):
                remove(child, 'summaries', False)

    # Orphaned atomic catalog uploads/rewrite temporaries are not real
    # catalogs. Expire only known names and only after a full retention window
    # without a write, leaving active publishers untouched.
    if out.exists():
        for child in sorted(out.iterdir()):
            if child.is_symlink() or not child.is_file():
                continue
            if not re.fullmatch(
                r'\.catalog(?:_(?:rrfs|refs_mean|refs_m00[1-5]|hrrr|analysis|aligned_analysis|profiles))?\.json(?:\.incoming|\.retention\.\d+)',
                child.name):
                continue
            if dt.datetime.fromtimestamp(child.stat().st_mtime, dt.timezone.utc) < cutoff:
                remove(child, 'catalog_temps', False)

    # Model, native-analysis, aligned-analysis, and sounding stores all
    # follow the same clock and explicit protection rules. The daily sweep is
    # independent of the success of their respective ingestion workflows.
    if out.exists():
        for kind, folder, category in (('analysis', out/'analysis', 'analysis'),
                                       ('aligned', out/'aligned', 'aligned'),
                                       ('profiles', out, 'profiles')):
            if not folder.is_dir() or folder.is_symlink():
                continue
            for store in sorted(folder.iterdir()):
                if expired_auxiliary(store.name, kind):
                    remove(store, category, True)

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
                if match and eligible(child, match, 1, 2, fixed_model=model):
                    remove(child, 'work_cycles', True)

    if work.exists():
        # Independently expire interrupted native-analysis/profile publication
        # archives, which model-only cleanup used to leave behind.
        for child in sorted(work.iterdir()):
            m = LOCAL_ANALYSIS_ARCHIVE_RE.fullmatch(child.name)
            if m and old_hour(m.group(2), 'obs') and not protected_analysis(m.group(1), m.group(2)):
                remove(child, 'publication_archives', False)
                continue
            m = LOCAL_PROFILE_ARCHIVE_RE.fullmatch(child.name)
            if m and old_hour(m.group(2) + m.group(3), 'obs' if m.group(1) == 'rap_analysis' else 'gfs' if m.group(1).startswith('gfs_') else 'model') and not protected_profile(*m.groups()):
                remove(child, 'publication_archives', False)

        # Staged verification GRIBs/manifests, including Stage IV, MRMS, RAP,
        # URMA, RTMA and NOHRSC. A protected range can cross only part of a
        # day; remove individual unprotected hours in that case.
        staging = work / 'verification_staging'
        if staging.is_dir() and not staging.is_symlink():
            for product in LOCAL_ANALYSIS_PRODUCTS:
                source = staging / product
                if not source.is_dir() or source.is_symlink():
                    continue
                for day in sorted(source.iterdir()):
                    if day.is_symlink() or not day.is_dir() or not re.fullmatch(r'\d{8}', day.name):
                        continue
                    try:
                        day_end = dt.datetime.strptime(day.name, '%Y%m%d').replace(tzinfo=dt.timezone.utc) + dt.timedelta(days=1)
                    except ValueError:
                        continue
                    pinned = any(protected_analysis(product, day.name + f'{hour:02d}') for hour in range(24))
                    # Fully expired, unprotected day folders can be deleted
                    # together. On the rolling cutoff day, expire hours
                    # individually so nothing lingers an extra 24 hours.
                    if day_end <= obs_cutoff and not pinned:
                        remove(day, 'staging', True)
                        continue
                    pattern = re.compile(re.escape(product) + r'_(\d{10})\.(?:json|grib2|source\.grib2|source\.grib2\.gz|conversion_error\.json)$')
                    for child in sorted(day.iterdir()):
                        match = pattern.fullmatch(child.name)
                        if match and old_hour(match.group(1), 'obs') and not protected_analysis(product, match.group(1)):
                            remove(child, 'staging', False)
                    if not dry_run and not any(day.iterdir()):
                        day.rmdir()

        # Sounding preparation and historical case-run diagnostics are also
        # dated work products. Preserve any explicit case cycle, including RAP
        # profile hours in its protected verification interval.
        profile_work = work / 'sounding_profiles'
        if profile_work.is_dir() and not profile_work.is_symlink():
            for model in (*MODELS, 'rap_analysis'):
                model_dir = profile_work / model
                if not model_dir.is_dir() or model_dir.is_symlink():
                    continue
                for cycle in sorted(model_dir.iterdir()):
                    match = re.fullmatch(r'(\d{8})_(\d{2})', cycle.name)
                    if (match and old_hour(''.join(match.groups()), 'obs' if model == 'rap_analysis' else 'gfs' if model.startswith('gfs_') else 'model') and
                        not protected_profile(model, *match.groups())):
                        remove(cycle, 'profile_work', True)
        case_logs = work / 'case_stage_logs'
        if case_logs.is_dir() and not case_logs.is_symlink():
            for run in sorted(case_logs.iterdir()):
                # create_case uses YYYYMMDDHH_<run timestamp>; accept the
                # older underscore-separated cycle spelling too.
                match = re.fullmatch(r'(\d{8})_?(00|06|12|18)_\d{8}T\d{6}\d{0,6}Z', run.name)
                if (match and old_hour(''.join(match.groups())) and
                    not is_protected(protected_stores, *match.groups()) and
                    not any(a <= ''.join(match.groups()) <= b for a,b,_ in analysis_rules) and
                    not run.is_symlink() and run.is_dir() and
                    dt.datetime.fromtimestamp(run.stat().st_mtime, dt.timezone.utc) < cutoff):
                    remove(run, 'case_logs', True)

    # Remove old hourly observation files only when outside every protected
    # verification valid-time window. One cycle protects F000-F084 here.
    observations=out/'ptype_observations'
    if observations.is_dir() and not observations.is_symlink():
        for child in sorted(observations.iterdir()):
            m=LOCAL_OBS_RE.fullmatch(child.name)
            if not m or child.is_symlink() or not child.is_file():continue
            try: valid=dt.datetime.strptime(m.group(1),'%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
            except ValueError:continue
            if valid<obs_cutoff and not any(start<=m.group(1)<=end for start,end,_ in analysis_rules):
                remove(child,'observations',False)
    # Interrupted local ptype/METAR/mPING output writes use a dated pending
    # filename. Preserve active transfers; remove only old leftovers.
    if observations.is_dir() and not observations.is_symlink():
        for child in sorted(observations.iterdir()):
            if child.is_symlink() or not child.is_file() or not re.fullmatch(r'\d{10}\.json\.incoming', child.name):
                continue
            if dt.datetime.fromtimestamp(child.stat().st_mtime, dt.timezone.utc) < obs_cutoff:
                remove(child, 'observations', False)

    # GLM hourly files are keyed by forecast valid time and use the same
    # protected valid-hour windows as other observation GeoJSON.
    lightning=out/'lightning'
    if lightning.is_dir() and not lightning.is_symlink():
        for child in sorted(lightning.iterdir()):
            m=LOCAL_OBS_RE.fullmatch(child.name)
            if not m or child.is_symlink() or not child.is_file():continue
            try: valid=dt.datetime.strptime(m.group(1),'%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
            except ValueError:continue
            if valid<obs_cutoff and not any(start<=m.group(1)<=end for start,end,_ in analysis_rules):
                remove(child,'glm_lightning',False)
        # save_atomic() uses a random suffix to avoid colliding with concurrent writers.
        for child in sorted(lightning.iterdir()):
            if (child.is_symlink() or not child.is_file() or
                    not re.fullmatch(r'\.\d{10}\.json\.[A-Za-z0-9_-]+\.incoming',child.name)):
                continue
            if dt.datetime.fromtimestamp(child.stat().st_mtime,dt.timezone.utc)<obs_cutoff:
                remove(child,'glm_lightning',False)
    # NOAA SPC 12 UTC–12 UTC preliminary report files are retained when
    # their report-day window intersects any protected case valid-time window.
    spc=out/'spc_reports'
    if spc.is_dir() and not spc.is_symlink():
        for child in sorted(spc.iterdir()):
            if child.is_symlink() or not child.is_file():continue
            m=re.fullmatch(r'(\d{6})\.json',child.name)
            if not m:continue
            try:day=dt.datetime.strptime(m.group(1),'%y%m%d').replace(tzinfo=dt.timezone.utc,hour=12)
            except ValueError:continue
            end=day+dt.timedelta(days=1)
            pinned=any(day<dt.datetime.strptime(b,'%Y%m%d%H').replace(tzinfo=dt.timezone.utc)+dt.timedelta(hours=1)
                       and end>dt.datetime.strptime(a,'%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
                       for a,b,_ in analysis_rules)
            if end<obs_cutoff and not pinned:remove(child,'observations',False)
    # Remove stale interrupted local SPC JSON writes after configured retention;
    # these files are never treated as valid observations.
    if spc.is_dir() and not spc.is_symlink():
        for child in sorted(spc.iterdir()):
            if child.is_symlink() or not child.is_file() or not re.fullmatch(r'\d{6}\.json\.incoming',child.name):continue
            if dt.datetime.fromtimestamp(child.stat().st_mtime,dt.timezone.utc)<obs_cutoff:
                remove(child,'observations',False)
    published=work/'.published'
    if published.is_dir() and not published.is_symlink():
        for child in sorted(published.iterdir()):
            m=LOCAL_PUBLISH_STATE_RE.fullmatch(child.name)
            if m and eligible(child,m,2,3):remove(child,'publish_states',False)

    if published.is_dir() and not published.is_symlink():
        for child in sorted(published.iterdir()):
            if not child.name.endswith('.json'):
                continue
            store = child.name[:-5]
            if expired_auxiliary(store, 'profiles'):
                remove(child, 'publish_states', False)

    # Fixed stderr captures in the historical crontab are append-only. Rotate
    # them into UTC-dated logs during the daily retention pass, then apply the
    # same configured retention policy as the other logs. A process with the file open will
    # safely finish writing the renamed inode; the next cron run opens a new
    # fixed-name file. Never touch lock files, unknown paths, or symlinks.
    if logs is not None and logs.is_dir() and logs.parent == out.parent == work.parent:
        for base_name in ('cron_errors','model_cron_errors'):
            current=logs.parent/(base_name+'.log')
            if not current.is_file() or current.is_symlink():continue
            if current.stat().st_size<=0:continue
            dest=logs/(base_name+'_'+now.strftime('%Y%m%d_%H%M%S')+'.log')
            if dest.exists():continue
            logging.info('Local %s %s -> %s', 'WOULD ROTATE' if dry_run else 'ROTATE',current,dest)
            stats['cron_stdout']+=1
            if not dry_run:os.replace(current,dest)
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
            if ts < cutoff and dt.datetime.fromtimestamp(child.stat().st_mtime,dt.timezone.utc) < cutoff:
                remove(child, 'logs', False)

    # Process-tree monitoring writes dated JSONL/JSON/text diagnostics with a
    # separate historical default. Honor the operational retention period here,
    # including when a legacy config still sets a longer monitoring lookback.
    if logs is not None:
        performance = logs / 'performance'
        if performance.is_dir() and not performance.is_symlink():
            for child in sorted(performance.iterdir()):
                if child.is_symlink() or not child.is_file():
                    continue
                match = re.fullmatch(
                    r'evu_perf_(\d{8}T\d{12}Z)_[A-Za-z0-9_-]+_\d+_(?:samples\.jsonl|events\.jsonl|summary\.json|summary\.txt)',
                    child.name)
                if not match:
                    continue
                try:
                    stamp = dt.datetime.strptime(match.group(1), '%Y%m%dT%H%M%S%fZ').replace(tzinfo=dt.timezone.utc)
                except ValueError:
                    continue
                if stamp < cutoff and dt.datetime.fromtimestamp(child.stat().st_mtime, dt.timezone.utc) < cutoff:
                    remove(child, 'performance_logs', False)

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
            if (init is not None and (init >= cutoff or is_protected(protected_stores, match.group(2), match.group(3), match.group(1))) and store_path.is_dir() and not store_path.is_symlink()):
                keep.append(entry)
        removed = len(entries) - len(keep)
        if removed:
            stats['catalog_entries'] += removed
            logging.info('Local %s %s: %d expired/missing entries',
                         'WOULD PRUNE' if dry_run else 'PRUNE', catalog_path, removed)
            if not dry_run:
                obj['datasets'] = keep
                core.json_write(catalog_path, obj)

    # Reconcile the three auxiliary catalogs and upload-cache maps with
    # their actual surviving stores. This prevents stale links and false
    # publication acknowledgements after unattended cleanup.
    for cat, prefix, pattern, obj in aux_catalogs:
        keep = []
        for entry in obj['datasets']:
            path = entry.get('path', '') if isinstance(entry, dict) else ''
            name = path[len(prefix)+1:] if prefix and path.startswith(prefix+'/') else path
            valid_path = path == ((prefix + '/' + name) if prefix else name)
            match = pattern.fullmatch(name) if isinstance(name, str) else None
            kind = 'profiles' if not prefix else prefix
            if valid_path and match:
                location = out / path
                if expired_auxiliary(name, kind) or (not dry_run and not location.is_dir()):
                    stats['aux_catalog_entries'] += 1
                    continue
            keep.append(entry)
        if len(keep) != len(obj['datasets']):
            logging.info('Local %s %s: %d auxiliary entries',
                         'WOULD PRUNE' if dry_run else 'PRUNE', cat,
                         len(obj['datasets']) - len(keep))
            if not dry_run:
                obj['datasets'] = keep
                core.json_write(cat, obj)
    for path, state in aux_states:
        keep = {}
        for key, revision in state.items():
            # Native states use dataset ids; aligned states use store names.
            store = key if key.endswith('.zarr') else key + '.zarr'
            m = LOCAL_ANALYSIS_RE.fullmatch(store)
            kind = 'aligned' if path.name.startswith('.aligned') else 'analysis'
            if m and expired_auxiliary(store, kind):
                stats['aux_publish_states'] += 1
            else:
                keep[key] = revision
        if len(keep) != len(state) and not dry_run:
            core.json_write(path, keep)

    logging.info('Local retention %s cutoff=%s (UTC): %s',
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


def cleanup_remote(host, remote_dir, retention_days=7, dry_run=False, now=None, protected_stores=(), analysis_rules=(), obs_retention_days=None, gfs_retention_days=None):
    """Expire allow-listed model stores and observation files on the web server."""
    validate_publisher(host, remote_dir)
    if not shutil.which('ssh'):
        raise RuntimeError('Remote retention requires ssh')
    now = now or dt.datetime.now(dt.timezone.utc)
    obs_retention_days = retention_days if obs_retention_days is None else obs_retention_days
    gfs_retention_days = retention_days if gfs_retention_days is None else gfs_retention_days
    if any(not isinstance(n, int) or not 1 <= n <= 366 for n in (obs_retention_days, gfs_retention_days)):
        raise ValueError('Observation and GFS retention must be 1..366 days')
    cutoff = (now - dt.timedelta(days=retention_days)).isoformat().replace('+00:00', 'Z')
    obs_cutoff=(now-dt.timedelta(days=obs_retention_days)).isoformat().replace('+00:00','Z')
    gfs_cutoff=(now-dt.timedelta(days=gfs_retention_days)).isoformat().replace('+00:00','Z')
    cmd = ('cd ' + shlex.quote(remote_dir) + ' && flock -w 120 .evu_profiles_catalog.lock python3 - ' + shlex.quote(remote_dir) + ' ' + shlex.quote(cutoff) + ' ' + ('1' if dry_run else '0') + ' ' + shlex.quote(json.dumps(sorted(protected_stores))) + ' ' + shlex.quote(json.dumps([[start,end] for start,end,_ in analysis_rules])) + ' ' + shlex.quote(json.dumps([[start,end,list(products)] for start,end,products in analysis_rules])) + ' ' + shlex.quote(obs_cutoff) + ' ' + shlex.quote(gfs_cutoff))
    logging.info('Remote retention: expired stores/observations before %s (%s)', cutoff, remote_dir)
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
    p=argparse.ArgumentParser(description='Parallel RRFS / REFS / HRRR GRIB -> Zarr v2 with optional remote publishing')
    p.add_argument('--date',required=True,help='YYYYMMDD or auto (with --cycle auto)')
    p.add_argument('--cycle',required=True,choices=('00','06','12','18','auto'))
    p.add_argument('--lag-hours',type=float,default=3.5,help='UTC auto-cycle availability delay in hours (default 3.5 h = 210 min)')
    p.add_argument('--models',default='all',help='all or rrfs,refs_mean,refs_m001,...,hrrr')
    p.add_argument('--hours',default=None,help='Forecast-hour subset, e.g. 0-3 (default model-specific full range)')
    p.add_argument('--workers',type=int,default=8,help='Concurrent forecast-hour processes, any positive integer (default 8)')
    p.add_argument('--output-root',default='./zarr')
    p.add_argument('--work-root',default='./work')
    p.add_argument('--wgrib2',default=None)
    p.add_argument('--compression-level',type=int,default=3,choices=range(10))
    p.add_argument('--timeout',type=int,default=120)
    p.add_argument('--retries',type=int,default=5)
    p.add_argument('--insecure',action='store_true',help='Disable TLS verification for local CA problems only')
    p.add_argument('--keep-grib',action='store_true')
    p.add_argument('--incremental',action='store_true',
                   help='Availability-driven bounded frontier: fetch new hours and retry missing fields without overwriting existing chunks')
    p.add_argument('--max-hour-jobs',type=int,default=0,
                   help='Maximum forecast-hour jobs per incremental run (0 means all eligible jobs; concurrency is limited by --workers)')
    p.add_argument('--lookahead-hours',type=int,default=12,
                   help='Forecast-hour discovery window beyond last acquired hour (1-84, default 12)')
    p.add_argument('--force-missing-check',action='store_true',
                   help='In incremental mode, bypass the missing-field retry cooldown (manual case backfills)')
    p.add_argument('--missing-retry-minutes',type=int,default=60,
                   help='Minimum gap between repeated probes for incomplete processed hours (default 60 min)')
    p.add_argument('--publish-mode',choices=('auto','full'),default='auto',
                   help='auto transfers only changed Zarr files after an initial full upload; full always uploads entire store')
    p.add_argument('--overwrite-hours',action='store_true')
    p.add_argument('--repair-refs-pressure',action='store_true',
                   help='Safely add missing t925/t850/t700 REFS-member fields to already processed hours, retaining existing fields')
    p.add_argument('--repair-upper-air',action='store_true',
                   help='Add missing upper-air and severe-weather GRIB fields to previously completed forecast hours')
    p.add_argument('--repair-refs-pmm-apcp',action='store_true',
                   help='Replace previously ingested REFS Mean ordinary mean APCP with PMM APCP (targeted repair)')
    p.add_argument('--repair-lowlevel-shear',action='store_true',
                   help='Repair completed RRFS/REFS-member 0-1 km shear from native 3-km winds and pressure without reprocessing other fields')
    p.add_argument('--repair-mslp',action='store_true',
                   help='Add missing native MSLP fields to previously processed model hours, preserving all other Zarr data')
    p.add_argument('--repair-snowfall',action='store_true',
                   help='Restore missing accumulated ASNOW from verified staging or GRIB, preserving completed fields')
    p.add_argument('--repair-refs-mean',action='store_true',
                   help='Safely add missing REFS mean categorical fields, REFC from PMMN, WEASD and ASNOW to completed forecast hours')
    p.add_argument('--strict-fields',action='store_true',help='Fail if an hour has no supported fields')
    p.add_argument('--no-publish',action='store_true',help='Do not publish to the configured remote server')
    p.add_argument('--inventory-only',action='store_true',help='Inspect GRIB .idx files only; no downloads, Zarr output or publishing (default: first hour per model)')
    p.add_argument('--catalog-only',action='store_true',help='Rebuild per-model catalogs without downloading/publishing')
    p.add_argument('--publish-only',action='store_true',help='Publish existing stores and model catalogs, without processing')
    p.add_argument('--publish-host',default=core.DEFAULT_PUBLISH_HOST)
    p.add_argument('--publish-dir',default=core.DEFAULT_PUBLISH_DIR)
    p.add_argument('--publish-workers',type=int,default=int(os.environ.get('PUBLISH_WORKERS','3')),
                   help='Concurrent independent model-store SCP/SSH transfers (positive integer; default 3)')
    p.add_argument('--keep-publish-archive',action='store_true')
    p.add_argument('--retention-days',type=int,default=7,help='RRFS/REFS/HRRR retention days (default 7)')
    p.add_argument('--obs-retention-days',type=int,default=None,help='Observation/analysis retention; defaults to model retention')
    p.add_argument('--gfs-retention-days',type=int,default=None,help='GFS profile retention during shared cleanup; defaults to model retention')
    p.add_argument('--protected-cycles-file',type=Path,default=DEFAULT_PROTECTED_CYCLES_FILE,
                   help='JSON whitelist of cycles/models preserved indefinitely; required for safe cleanup')
    p.add_argument('--no-local-cleanup',action='store_true',help='Disable local Zarr, scratch, and summary retention')
    p.add_argument('--logs-dir',default=None,help='Optional cron log directory; expire only multisource_YYYYMMDD_HHMMSS.log files')
    p.add_argument('--no-remote-cleanup',action='store_true',help='Disable SSH retention cleanup')
    p.add_argument('--cleanup-dry-run',action='store_true',help='Preview local and remote retention without deleting')
    p.add_argument('--cleanup-only',action='store_true',help='Run enabled local/remote retention without processing or publishing')
    p.add_argument('-v','--verbose',action='store_true')
    a=p.parse_args(argv)
    if a.publish_workers<1:p.error('--publish-workers must be positive')
    try:
        if not (math.isfinite(a.lag_hours) and 0 <= a.lag_hours <= 24):
            raise ValueError('--lag-hours must be within 0-24')
        if not (1 <= a.retention_days <= 366):
            raise ValueError('--retention-days must be within 1-366')
        if any(n is not None and not 1 <= n <= 366 for n in (a.obs_retention_days, a.gfs_retention_days)):
            raise ValueError('--obs-retention-days and --gfs-retention-days must be within 1-366')
        resolve_cycle(a)
        a.models=parse_models(a.models)
        if a.hours is not None:
            a.hours=core.parse_hours(a.hours)
        if a.workers<1:
            raise ValueError('--workers must be positive')
        if not 0<=a.max_hour_jobs<=1000:
            raise ValueError('--max-hour-jobs must be 0-1000 (0 means no selection cap)')
        if not 1<=a.lookahead_hours<=84:
            raise ValueError('--lookahead-hours must be 1-84')
        if not 5<=a.missing_retry_minutes<=1440:
            raise ValueError('--missing-retry-minutes must be 5-1440')
        if a.incremental and (a.overwrite_hours or a.hours is not None):
            raise ValueError('--incremental cannot be combined with --overwrite-hours or --hours')
        if a.incremental and (a.inventory_only or a.catalog_only or a.publish_only or a.cleanup_only):
            raise ValueError('--incremental cannot be combined with inventory/catalog/publish/cleanup-only')

        if sum((a.repair_refs_pressure,a.repair_refs_mean,a.repair_upper_air,a.repair_refs_pmm_apcp,a.repair_snowfall,a.repair_mslp,a.repair_lowlevel_shear))>1:
            raise ValueError('Select only one repair mode')
        if a.incremental and any((a.repair_refs_pressure,a.repair_refs_mean,a.repair_upper_air,a.repair_refs_pmm_apcp,a.repair_snowfall,a.repair_mslp,a.repair_lowlevel_shear)):
            raise ValueError('--incremental handles missing fields; do not combine it with repair modes')
        if a.repair_refs_pmm_apcp and a.hours is not None:
            raise ValueError('--repair-refs-pmm-apcp must cover the entire REFS Mean cycle; omit --hours')
        if (a.repair_refs_pressure or a.repair_refs_mean or a.repair_upper_air or a.repair_refs_pmm_apcp or a.repair_snowfall or a.repair_mslp) and (a.overwrite_hours or a.inventory_only or a.catalog_only or a.publish_only):
            raise ValueError('Repair modes cannot be combined with overwrite/inventory/catalog/publish-only')
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
    try:
        a.protected_stores = load_protected_cycles(a.protected_cycles_file)
        from retention_policy import load_policy
        a.protected_analysis_rules = load_policy(a.protected_cycles_file).analysis_rules
    except ValueError as exc:
        p.error(str(exc))
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
                if name in ('wspd10','wspd300','mxuphl_run'):
                    continue
                if name == 'mslp':
                    selected = select_mslp_record(inventories, model)
                    if selected:
                        product, record = selected
                        matches[name] = (product, record['text'])
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


def missing_fields_for_hour(store, model, fhr, manifest_hour=None):
    """On-disk chunk presence is the authority, not the coarse processed flag.

    Unknown accumulation intervals already staged are not re-fetched every
    cycle; they remain recoverable by the dedicated snowfall repair logic.
    """
    missing=[]
    for name in FIELDS:
        if name in ('wspd10','wspd300','mxuphl_run','shear01','shear06'):
            continue
        if fhr==0 and (name in ACCUMULATIONS or name=='mxuphl'):
            continue
        if is_profile_shear_model(model) and name in ('shru01','shrv01'):
            continue  # derived from pressure-level winds, never native source
        if name=='psfc' and not (is_profile_shear_model(model) or model=='hrrr'):continue
        if model=='refs_mean' and name=='absv500':
            # REFS mean omits native ABSV; derive this from mean winds later.
            continue
        if (store/name/f'{fhr}.0.0').is_file():continue
        if (store/'.accum_intervals'/name/f'{fhr}.zlib').is_file():continue
        missing.append(name)
    # Derive/revise suspect RRFS/REFS low-level shear from native 3-km
    # pressure-level winds, never re-ingest the known-bad GRIB shear records.
    if is_profile_shear_model(model):
        existing=all((store/name/f'{fhr}.0.0').is_file() for name in PROFILE_SHEAR_OUTPUTS)
        if (not existing or manifest_revision(manifest_hour, 'shear01_revision')<PROFILE_SHEAR_REVISION):
            for name in PROFILE_SHEAR_INPUTS:
                if not (store/name/f'{fhr}.0.0').is_file() and name not in missing:
                    missing.append(name)
            if 'shear01' not in missing:missing.append('shear01')
    # Older stores of other models may still require genuine native component
    # normalization (HRRR and all validated 0–6 km shear are unchanged).
    for suffix in ('01','06'):
        if suffix=='01' and is_profile_shear_model(model):continue
        pair=('shru'+suffix,'shrv'+suffix)
        if (manifest_revision(manifest_hour, 'shear'+suffix+'_revision')<4 and
            all((store/n/f'{fhr}.0.0').is_file() for n in pair)):
            missing.extend(n for n in pair if n not in missing)
    return tuple(missing)


def incremental_hours(model, flags, max_fhr, lookahead):
    """Probe only a bounded frontier; advance as files actually arrive."""
    start,end=hour_bounds(model)
    frontier=max((h for h in range(start,end+1) if flags[h]),default=start-1)
    end=min(end,frontier+lookahead)
    return range(start,end+1)



def merge_repair_result(previous, result):
    """Merge recovered fields with a completed forecast hour safely.

    Incremental missing-field retries intentionally have repair_kind=None;
    ``dict.get(key, default)`` does not use the default for an explicit None.
    Retain prior field coverage and provenance while recording the retry.
    """
    combined = dict(previous)
    combined['status'] = 'complete'
    combined['fields'] = sorted(set(previous.get('fields') or ()) | set(result.get('fields') or ()))
    for key in ('selections', 'products'):
        combined[key] = {**(previous.get(key) or {}), **(result.get(key) or {})}
    pending_prior = dict(previous.get('pending') or {})
    kind = result.get('repair_kind') or 'fields'
    if kind == 'snowfall':
        pending_prior.pop('asnow', None)
    pending_prior.update(result.get('pending') or {})
    if pending_prior:
        combined['pending'] = pending_prior
    else:
        combined.pop('pending', None)
    combined[f'repair_{kind}_added'] = list(result.get('fields') or ())
    for suffix in ('01','06'):
        if result.get('shear'+suffix+'_revision') in (4,5,6):
            combined['shear'+suffix+'_revision']=result['shear'+suffix+'_revision']
            if suffix=='01':
                combined['shear01_orientation']=result.get('shear01_orientation','not checked')
                if result.get('shear01_valid_points') is not None:
                    combined['shear01_valid_points']=result['shear01_valid_points']
    return combined


@perf_monitor.monitored('model_conversion')
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
                          a.cleanup_dry_run, logs_dir=a.logs_dir,
                          protected_stores=a.protected_stores, analysis_rules=a.protected_analysis_rules,
                          obs_retention_days=a.obs_retention_days, gfs_retention_days=a.gfs_retention_days)
        if not (a.no_publish or a.no_remote_cleanup):
            cleanup_remote(a.publish_host, a.publish_dir, a.retention_days,
                           a.cleanup_dry_run, protected_stores=a.protected_stores, analysis_rules=a.protected_analysis_rules,
                          obs_retention_days=a.obs_retention_days, gfs_retention_days=a.gfs_retention_days)
        return
    if not (a.no_publish or a.no_remote_cleanup):
        validate_publisher(a.publish_host, a.publish_dir)
    perf_monitor.record_event('run_config', workflow='forecast_conversion',
        models=list(a.models), forecast_workers=a.workers, publish_workers=a.publish_workers,
        max_hour_jobs=a.max_hour_jobs, incremental=bool(a.incremental),
        compression_level=a.compression_level)
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
    recovered_models=set()
    if not(a.catalog_only or a.publish_only):
        wgrib2=core.find_wgrib2(a.wgrib2)
        logging.info('Using %s; up to %d parallel forecast-hour workers',wgrib2,a.workers)
    else:
        wgrib2=None
    for model in a.models:
        if a.repair_refs_pressure and family(model) != 'refs_member':
            continue
        if a.repair_refs_mean and model!='refs_mean':
            continue
        if a.repair_refs_pmm_apcp and model!='refs_mean':
            continue
        lo,hi=hour_bounds(model)
        hours=range(lo,hi+1) if a.hours is None else [h for h in a.hours if lo<=h<=hi]
        if not hours:
            logging.warning('%s: no hours requested within %d-%d',model,lo,hi)
            continue
        store=out/store_name(model,a.date,a.cycle)
        if a.repair_snowfall and not store.is_dir():
            logging.info('%s: no existing cycle store; skipping snowfall-only repair',store.name)
            continue
        stores[model]=store
        if a.catalog_only or a.publish_only:
            continue
        init_store(store,model,a.date,a.cycle,a.compression_level)
        if model=='refs_mean':
            for item,new_label in (('refc','PMM composite reflectivity'),('apcp','PMM accumulated precipitation')):
                attrs_path=store/item/'.zattrs'
                if attrs_path.exists():
                    attrs=json.loads(attrs_path.read_text())
                    # Never misrepresent historic APCP from the ordinary mean:
                    # change existing APCP attributes only AFTER the PMM chunk
                    # has been written, inside the successful job coordinator.
                    if item=='refc':
                        attrs.update({'long_name':new_label,'probability_matched_mean':True,'GRIB_product':'pmmn'})
                        core.json_write(attrs_path,attrs)
            for cname in core.PTYPE_INPUTS:
                attrs_path=store/cname/'.zattrs'
                if attrs_path.exists():
                    attrs=json.loads(attrs_path.read_text())
                    attrs['ensemble_mean_fraction']=True
                    attrs['long_name']='Weighted ensemble-mean '+FIELDS[cname]['long_name'].lower()+' (not a deterministic category)'
                    core.json_write(attrs_path,attrs)
        flags=read_processed(store,hi+1)
        processed[model]=flags
        if a.incremental:
            hours=incremental_hours(model,flags,hi,a.lookahead_hours)
            logging.info('%s incremental discovery: checking through F%03d (last processed F%03d)',
                         model,max(hours),max((h for h in range(lo,hi+1) if flags[h]),default=-1))

        manifest_path=out/f'{store.stem}.selection.json'
        manifest_paths[model]=manifest_path
        manifest=json.loads(manifest_path.read_text()) if manifest_path.exists() else {
            'model':model,'date':a.date,'cycle':a.cycle,'forecast_hours':{}}
        manifests[model]=manifest
        if a.repair_snowfall:
            done_hours=[h for h in hours if flags[h]]
            restored=restore_staged_snowfall(store,manifest,a.compression_level,done_hours)
            if restored:
                recovered_models.add(model)
                core.json_write(manifest_path,manifest)
                logging.info('%s: restored %d ASNOW hours from validated staging: %s',
                             model,len(restored),restored)
        for h in hours:
            repair_fields = None
            if a.repair_lowlevel_shear:
                if not flags[h] or not is_profile_shear_model(model):continue
                old=manifest.get('forecast_hours',{}).get(f'{h:03d}',{})
                if (manifest_revision(old, 'shear01_revision')>=PROFILE_SHEAR_REVISION and
                    all((store/name/f'{h}.0.0').is_file() for name in PROFILE_SHEAR_OUTPUTS)):
                    continue
                # Missing inputs are fetched selectively; if all exist this
                # repair is entirely offline and requires no GRIB downloads.
                missing_input=profile_shear_missing_inputs(store,h)
                repair_fields=tuple(missing_input+('shear01',))
            elif a.repair_refs_pressure:
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
                repair_fields=tuple(name for name in ('refc','crain','cfrzr','cicep','csnow','weasd','asnow',
                                                       'h500','u500','v500')
                                    if not (store/name/f'{h}.0.0').is_file()
                                    and not (store/'.accum_intervals'/name/f'{h}.zlib').is_file())
                if not repair_fields:
                    continue
            elif a.repair_mslp:
                if not flags[h]:
                    continue
                if (store/'mslp'/f'{h}.0.0').is_file():
                    continue
                repair_fields=('mslp',)
            elif a.repair_snowfall:
                if not flags[h]:
                    continue
                if (store/'asnow'/f'{h}.0.0').is_file():
                    continue
                # Failed or unavailable staged recoveries are retried via GRIB
                # index; the repair downloads only the ASNOW record.
                repair_fields=('asnow',)
            elif a.repair_upper_air:
                if not flags[h]:continue
                selected_names=tuple(f'{prefix}{pressure}' for pressure in (925,850,700,500,300)
                    for prefix in ('h','u','v')) + ('absv500','wspd300','q700','q850','q925','td700','td850','td925') + tuple(SEVERE_SPECS) + tuple(SHEAR_COMPONENTS) + ('ustm','vstm','shear01','shear06')
                repair_fields=tuple(name for name in selected_names
                    if not (store/name/f'{h}.0.0').is_file())
                if not repair_fields:continue
            elif a.incremental:
                if flags[h]:
                    repair_fields=missing_fields_for_hour(store,model,h,manifest.get('forecast_hours',{}).get(f'{h:03d}',{}))
                    if not repair_fields: continue
                old=manifest.get('forecast_hours',{}).get(f'{h:03d}',{})
                checked=old.get('last_missing_check_utc')
                if checked and not a.force_missing_check:
                    try:
                        age=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(checked)).total_seconds()
                        # Missing fields in completed hours change less often;
                        # unavailable future hours may be retried next poll.
                        wait=(a.missing_retry_minutes if flags[h] else 10)*60
                        if age < wait:
                            continue
                    except (ValueError,TypeError):pass
                # Fresh hours have repair_fields=None; unavailable F-hours remain
                # unprocessed and will be probed again on the next scheduled scan.
            elif a.repair_refs_pmm_apcp:
                if not flags[h]:continue
                # Keep the legacy ensemble mean under a private backup path;
                # remove it before reconstructing PMM totals. A failed PMM
                # hour stays MISSING rather than silently reappearing as mean.
                original=store/'apcp'/f'{h}.0.0'
                backup=store/'.legacy_mean_apcp'/f'{h}.0.0'
                if original.is_file():
                    backup.parent.mkdir(parents=True,exist_ok=True)
                    if not backup.is_file():shutil.copy2(original,backup)
                    original.unlink()
                staged=store/'.accum_intervals'/'apcp'/f'{h}.zlib'
                staged.unlink(missing_ok=True)
                repair_fields=('apcp',)
            else:
                if flags[h] and not a.overwrite_hours:
                    logging.info('%s F%03d already processed; skipping',model,h)
                    continue
                if flags[h]:
                    flags[h]=0
                    write_processed(store,flags,a.compression_level)
            jobs.append((model,a.date,a.cycle,h,str(store),str(work),wgrib2,
                         a.compression_level,a.timeout,a.retries,a.insecure,a.keep_grib,a.strict_fields,
                         repair_fields, 'profile_shear' if a.repair_lowlevel_shear else 'pressure' if a.repair_refs_pressure else 'mean' if a.repair_refs_mean else 'upper_air' if a.repair_upper_air else 'pmm_apcp' if a.repair_refs_pmm_apcp else 'snowfall' if a.repair_snowfall else 'mslp' if a.repair_mslp else None))
    if a.incremental and a.max_hour_jobs > 0 and len(jobs)>a.max_hour_jobs:
        # Round-robin across models: prevent RRFS (many available f-hours) from
        # starving REFS and HRRR while limiting the duration of a 15-min poll.
        by_model={m:[] for m in a.models}
        for job in jobs:by_model[job[0]].append(job)
        for model in by_model:
            by_model[model].sort(key=lambda job:(bool(processed[model][job[3]]),job[3]))
        selected=[]
        while len(selected)<a.max_hour_jobs and any(by_model.values()):
            for m in a.models:
                if by_model[m] and len(selected)<a.max_hour_jobs:
                    selected.append(by_model[m].pop(0))
        logging.info('Incremental time budget: processing %d of %d eligible forecast-hour tasks',
                     len(selected),len(jobs))
        jobs=selected
    failed=[]
    unavailable=[]
    no_match_repairs=[]
    incomplete_products=[]
    published_errors=[]
    changed_models=set(recovered_models)
    if jobs:
        logging.info('Starting %d forecast-hour tasks across %d models',len(jobs),len(stores))
        pool_started = time.monotonic()
        perf_monitor.record_event('stage_start', stage='forecast_hour_pool',
            jobs=len(jobs), workers=min(a.workers, len(jobs)))
        with ProcessPoolExecutor(max_workers=min(a.workers,len(jobs))) as pool:
            future_map={pool.submit(process_job,job + (time.time(),)):(job[0],job[3]) for job in jobs}
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
                    if a.incremental:
                        manifests[model]['forecast_hours'].setdefault(f'{h:03d}',{})[
                            'last_missing_check_utc']=dt.datetime.now(dt.timezone.utc).isoformat()
                    if result.get('repair_only'):
                        previous = manifests[model]['forecast_hours'].get(f'{h:03d}',{})
                        manifests[model]['forecast_hours'][f'{h:03d}'] = merge_repair_result(previous, result)
                    else:
                        manifests[model]['forecast_hours'][f'{h:03d}']=result
                    if a.incremental:
                        manifests[model]['forecast_hours'][f'{h:03d}']['last_missing_check_utc']=dt.datetime.now(dt.timezone.utc).isoformat()
                    core.json_write(manifest_paths[model],manifests[model])
                    if (is_profile_shear_model(model) and
                            result.get('shear01_revision')==PROFILE_SHEAR_REVISION):
                        for name in PROFILE_SHEAR_OUTPUTS:
                            attrpath=stores[model]/name/'.zattrs'
                            attrs=json.loads(attrpath.read_text())
                            attrs.update({'derivation':PROFILE_SHEAR_SOURCE,
                                          'source_fields':list(PROFILE_SHEAR_INPUTS),
                                          'source_provenance':'See per-hour manifest for completed revisions',
                                          'GRIB_shortName':'DERIVED',
                                          'units':'m s-1'})
                            core.json_write(attrpath,attrs)
                    missing = result.get('missing_products', {})
                    if missing:
                        incomplete_products.append((model,h,missing))
                        logging.warning('%s F%03d ingested %d fields, but product(s) absent: %s. '
                                        'Remaining fields will show unavailable; use --overwrite-hours '
                                        'to revisit after publication.',
                                        model,h,result['count'], ', '.join(sorted(missing)))
                    else:
                        logging.info('%s F%03d complete (%d fields)',model,h,result['count'])
                elif result['status']=='repair_no_match':
                    # No matching optional fields in an already completed hour.
                    # This is a normal negative check, NOT an unavailable hour.
                    # Advance the retry timer without altering completion state.
                    if a.incremental:
                        manifests[model]['forecast_hours'].setdefault(f'{h:03d}',{})[
                            'last_missing_check_utc']=dt.datetime.now(dt.timezone.utc).isoformat()
                        core.json_write(manifest_paths[model],manifests[model])
                    no_match_repairs.append((model,h))
                    logging.debug('%s F%03d: none of %d remaining fields supplied by available products; '
                                  'retaining existing completed-hour data',
                                  model,h,len(result.get('requested_fields',())))
                elif result['status']=='unavailable':
                    if a.incremental:
                        manifests[model]['forecast_hours'].setdefault(f'{h:03d}',{})[
                            'last_missing_check_utc']=dt.datetime.now(dt.timezone.utc).isoformat()
                        core.json_write(manifest_paths[model],manifests[model])
                    unavailable.append((model,h,result.get('reason','Unavailable'),
                                        result.get('missing_products',{})))
                    logging.warning('%s F%03d unavailable (not fatal): %s; %s',
                                    model,h,result['reason'],result.get('missing_products',{}))
                else:
                    failed.append((model,h,result['error']))
                    logging.error('%s F%03d failed: %s',model,h,result['error'])
        perf_monitor.record_event('stage_end', stage='forecast_hour_pool',
            elapsed_seconds=round(time.monotonic() - pool_started, 4), status='failed' if failed else 'complete',
            completed=len(jobs) - len(failed), failed=len(failed))
    if not (a.catalog_only or a.publish_only):
        for model,store in stores.items():
            if model in manifests:
                if finalize_accums(store,manifests[model],a.compression_level,warn_unknown=not a.incremental):
                    changed_models.add(model)
                if derive_run_max_uphl(store, manifests[model], a.compression_level):
                    changed_models.add(model)
                added300=fill_missing_300_wind(store,manifests[model],a.compression_level)
                if added300:
                    changed_models.add(model)
                    core.json_write(manifest_paths[model],manifests[model])
                    logging.info('%s: derived 300-hPa wind speed for %d forecast hours',model,len(added300))
                if model=='refs_mean':
                    derived=fill_refs_mean_500_vorticity(store,manifests[model],a.compression_level)
                    if derived:
                        changed_models.add(model)
                        core.json_write(manifest_paths[model],manifests[model])
                        logging.info('REFS Mean derived 500-hPa vorticity from mean U/V for %d hours',len(derived))
                if model=='refs_mean':
                    # Provenance is true only when every on-disk APCP chunk is
                    # explicitly selected from PMMN (or absent). A per-array
                    # .zattrs flag must never mislabel legacy mean chunks.
                    records=manifests[model].get('forecast_hours',{})
                    apcp_chunks=list((store/'apcp').glob('*.0.0'))
                    matches=lambda h:records.get(f'{h:03d}',{}).get('selections',{}).get('apcp',{}).get('product')=='pmmn'
                    if apcp_chunks and all(matches(int(ch.name.split('.')[0])) for ch in apcp_chunks):
                        path=store/'apcp'/'.zattrs';attrs=json.loads(path.read_text())
                        attrs.update({'long_name':'PMM accumulated precipitation',
                                      'probability_matched_mean':True,'GRIB_product':'pmmn'})
                        core.json_write(path,attrs)
            if model=='refs_mean' and store.is_dir():
                # Existing completed hours can be missing PTYPE even when no
                # remote GRIB repair is required.  Regenerate from local
                # categorical-mean chunks without any download.
                flags=read_processed(store,hour_bounds(model)[1]+1)
                added=repair_mean_ptype(store,flags,a.compression_level)
                if added:
                    logging.info('REFS mean derived dominant ensemble PTYPE for %d hours: %s',len(added),added)
                    changed_models.add(model)
                    if model in manifests:
                        for h in added:
                            record=manifests[model]['forecast_hours'].get(f'{h:03d}')
                            if record and record.get('status')=='complete':
                                record['fields']=sorted(set(record.get('fields',[]))|{'ptype'})
                                record['ptype_derivation']='dominant_ensemble_fraction_v1'
                        core.json_write(manifest_paths[model],manifests[model])

    if a.incremental and not changed_models:
        # A previous publish may have failed even though local processing
        # succeeded. Compare the acknowledged snapshot so the next poll
        # automatically retries publication instead of silently giving up.
        for model,store in stores.items():
            if not store.is_dir():continue
            flags=read_processed(store,hour_bounds(model)[1]+1)
            if not any(flags):continue
            state=work/'.published'/f'{store.name}.json'
            try:
                previous=json.loads(state.read_text())['files']
            except (OSError,ValueError,KeyError,TypeError):
                changed_models.add(model)
                continue
            if previous!=_published_files_snapshot(store):
                changed_models.add(model)
        if not changed_models:
            logging.info('Incremental poll has no unpublished model changes; skipping catalog rebuild')
            return
    catalog_paths=scan_stores(out, retention_days=a.retention_days,
                              protected_stores=a.protected_stores,
                              only_stores=[stores[m] for m in changed_models if m in stores] if a.incremental else None)
    logging.info('Wrote %d model-specific catalogs: %s',len(catalog_paths),', '.join(catalog_paths))
    if not (a.no_publish or a.catalog_only):
        # Prune before copying the new stores. This frees old data first and
        # lowers the risk of exhausting remote disk quota during large SCPs.
        if not a.no_remote_cleanup:
            try:
                cleanup_remote(a.publish_host,a.publish_dir,a.retention_days,a.cleanup_dry_run,
                               protected_stores=a.protected_stores, analysis_rules=a.protected_analysis_rules,
                          obs_retention_days=a.obs_retention_days, gfs_retention_days=a.gfs_retention_days)
            except Exception as exc:
                published_errors.append(('remote_cleanup', str(exc)))
                logging.error('Remote retention cleanup failed: %s', exc)
        pending_publications=[]
        for model,store in stores.items():
            if model not in changed_models and not a.publish_only:
                logging.info('%s: no new hour data; skipping expensive repeat upload',model)
                continue
            catalog=catalog_paths.get(model)
            if not store.is_dir() or catalog is None:
                logging.warning('Cannot publish %s: no completed Zarr store/catalog',model)
                continue
            pending_publications.append((model,store,catalog))
        def transfer(job):
            model,store,catalog=job
            publish_one(store,catalog,work,a.publish_host,a.publish_dir,
                        a.keep_publish_archive,publish_mode=a.publish_mode)
            return model
        # All remote cleanup has completed first. Each thread owns a unique
        # store archive, upload staging path and independent per-model catalog.
        # Catalogs are never installed until their own store is committed.
        with publish_transport.session():
            if a.publish_workers==1 or len(pending_publications)<2:
                for job in pending_publications:
                    try:transfer(job)
                    except Exception as exc:
                        published_errors.append((job[0],str(exc)))
                        logging.error('Publishing %s failed: %s',job[0],exc)
            else:
                with ThreadPoolExecutor(max_workers=a.publish_workers) as pool:
                    future_map={pool.submit(transfer,job):job[0] for job in pending_publications}
                    for future in as_completed(future_map):
                        model=future_map[future]
                        try:future.result()
                        except Exception as exc:
                            published_errors.append((model,str(exc)))
                            logging.error('Publishing %s failed: %s',model,exc)
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
               'completed_hours_checked_no_new_fields':len(no_match_repairs),
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
                          logs_dir=a.logs_dir, protected_stores=a.protected_stores, analysis_rules=a.protected_analysis_rules,
                          obs_retention_days=a.obs_retention_days, gfs_retention_days=a.gfs_retention_days)
        except Exception as exc:
            published_errors.append(('local_cleanup', str(exc)))
            logging.error('Local retention cleanup failed: %s', exc)
    logging.info('Run outcome: %d genuine hour failures, %d unavailable NEW hours, '
                 '%d previously completed hours checked with no new matching fields, '
                 '%d hours with missing optional products, %d publishing errors',
                 len(failed),len(unavailable),len(no_match_repairs),
                 len(incomplete_products),len(published_errors))
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
