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
    'mslp': dict(long_name='Mean sea level pressure', short='PRMSL', level='mean sea level', units='Pa', product='2dfld'),
    'u10': dict(long_name='10-m U wind component', short='UGRD', level='10 m above ground', units='m s-1', product='2dfld'),
    'v10': dict(long_name='10-m V wind component', short='VGRD', level='10 m above ground', units='m s-1', product='2dfld'),
    'gust': dict(long_name='10-m wind gust', short='GUST', level='surface', units='m s-1', product='2dfld'),
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
    # UPP GRIB2 table 4.2 category 7 parameter IDs 210/208/211.
    # Effective-layer STP with CIN and fixed-layer STP are DIFFERENT indices.
    'scp': ('SCCP', (), '1', 'Supercell Composite Parameter (effective layer)'),
    'stp': ('STPC', (), '1', 'Significant Tornado Parameter with CIN (effective layer)'),
    'stp_fixed': ('SIGT', (), '1', 'Significant Tornado Parameter (fixed layer)'),
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
                        level=layers[0],levels=layers,units='s-1',product='2dfld')
for suffix in ('01','06'):
    FIELDS['shear'+suffix] = dict(long_name=f'0–{1 if suffix=="01" else 6} km bulk wind shear',
                                 short='DERIVED',level='0 m above ground', units='m s-1',product='derived')
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


def choose_field(rows, name, fhr):
    info = FIELDS[name]
    if name == 'mslp':
        selected = select_mslp_record({'inventory': rows}, 'rrfs')
        return selected[1] if selected else None
    # Avoid mistaking wind gust at 10 m for arbitrary GUST levels in GRIB.
    cands = [r for r in rows if r['short_name'].upper() == info['short']
             and info['level'].lower() in r['text'].lower()]
    if name in SHEAR_COMPONENTS:
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

    Some RRFS/REFS feeds encode the wind vector *difference* while HRRR/RAP
    generally encode a vertical derivative, despite shared GRIB short names.
    Strongly diagnostic magnitudes override model expectations; low-amplitude
    ambiguous fields use the known model's operational convention. No values
    are silently rescaled without recording the mode in the repair utility.
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
    # Strongly non-gradient magnitudes (>0.2) mean the vectors are m/s.
    # Gradients typically occupy 0-0.02 s^-1. Weak fields cannot decide it.
    if median>=0.2:return 'm/s',median
    if median>0 and median<=0.03:return 's-1',median
    return ('s-1' if model=='hrrr' else 'm/s'),median


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
            if name in ('wspd10','wspd300','mxuphl_run'):
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
        if strict and not chosen:
            raise RuntimeError('No requested fields in available GRIB inventories')
        if not chosen:
            # Incremental missing-field checks revisit hours that were ALREADY
            # processed.  A source can be available but have none of the
            # remaining optional parameters (e.g. STP or CAPE at F000). This
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
                    else:
                        core.write_compressed_chunk(store / name / f'{fhr}.0.0', raw, level)
                    if name in core.PTYPE_INPUTS and model != 'refs_mean':
                        raw_categories[name] = raw
                    if name in ('u10','v10','u300','v300',*SHEAR_COMPONENTS):
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
        if all(k in wind_raw for k in ('u300','v300')):
            core.write_compressed_chunk(store/'wspd300'/f'{fhr}.0.0',
                                        decode_wspd(wind_raw['u300'],wind_raw['v300']),level)
        for suffix in ('01','06'):
            u,v='shru'+suffix,'shrv'+suffix
            if u in wind_raw and v in wind_raw:
                core.write_compressed_chunk(store/('shear'+suffix)/f'{fhr}.0.0',
                                            derive_bulk_shear_bytes(wind_raw[u],wind_raw[v],
                                                1000 if suffix=='01' else 6000,model),level)
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


# Retention exceptions are defined once on WCOSS2 and passed explicitly to
# RZDM, so both hosts make identical deletion/catalog decisions.
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
                    grouped[model].append(entry)
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
        subprocess.run(['ssh',host,'mkdir -p '+shlex.quote(remote_dir)],check=True)
        subprocess.run(['scp',str(archive),f'{host}:{remote_dir}/{remote_archive}'],check=True)
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
        subprocess.run(['ssh',host,script],check=True)
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
                # Reconstruction intermediates are for WCOSS2 only, not the viewer.
                if any(marker in t.name for marker in ('/.accum_intervals/', '/.legacy_mean_apcp/')) or t.name.endswith(('/.accum_intervals','/.legacy_mean_apcp')):
                    return None
                if not (t.isfile() or t.isdir()):
                    raise RuntimeError('Refusing non-regular file in published Zarr: '+t.name)
                # Analysis stores originate in tempfile.mkdtemp(), which has
                # mode 0700 by default.  Without normalizing TAR entry modes,
                # Apache cannot traverse the published .zarr directory and
                # browser chunk GETs fail with HTTP 403.  Apply only to web
                # analysis artifacts, not private WCOSS2 work directories.
                if store_arcname and store_arcname.startswith(('analysis/', 'aligned/')):
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
# Generated exclusively from validated WCOSS2 configuration; never accepts paths.
try:
    protected_names = json.loads(sys.argv[4])
    if (not isinstance(protected_names, list) or
        any(not isinstance(name, str) or
            not re.fullmatch(r'(rrfs|refs_mean|refs_m00[1-5]|hrrr)_\d{8}_(00|06|12|18)00\.zarr', name)
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
        if valid >= cutoff or any(a<=m.group(1)<=b for a,b in obs_rules):continue
        print(('WOULD REMOVE ' if dry_run else 'REMOVE ')+str(item),flush=True)
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
        if end>=cutoff or any(
                begin < dt.datetime.strptime(b,'%Y%m%d%H').replace(tzinfo=dt.timezone.utc)+dt.timedelta(hours=1)
                and end > dt.datetime.strptime(a,'%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
                for a,b in obs_rules):continue
        print(('WOULD REMOVE ' if dry_run else 'REMOVE ')+str(item),flush=True)
        if not dry_run:item.unlink()
    for item in sorted(spc_dir.iterdir()):
        if item.is_symlink() or not item.is_file() or not re.fullmatch(r'\.\d{6}\.json\.incoming',item.name):continue
        if dt.datetime.fromtimestamp(item.stat().st_mtime,dt.timezone.utc)>=cutoff:continue
        print(('WOULD REMOVE ' if dry_run else 'REMOVE ')+str(item),flush=True)
        if not dry_run:item.unlink()
# Interrupted mPING/METAR uploads may leave .YYYYMMDDHH.json.incoming.
if obs_dir.is_dir() and not obs_dir.is_symlink():
    for item in sorted(obs_dir.iterdir()):
        if item.is_symlink() or not item.is_file() or not re.fullmatch(r'\.\d{10}\.json\.incoming',item.name):continue
        changed=dt.datetime.fromtimestamp(item.stat().st_mtime,dt.timezone.utc)
        if changed>=cutoff:continue
        print(('WOULD REMOVE ' if dry_run else 'REMOVE ')+str(item),flush=True)
        if not dry_run:item.unlink()
# Only stale uploader files matching exact names may be pruned. Do not
# remove active/partially transferred files even if their date is old.
archive_re = re.compile(r'^\.(?:rrfs|refs_mean|refs_m00[1-5]|hrrr)_\d{8}_(?:00|06|12|18)00(?:\.delta)?\.tar\.incoming$')
stage_re = re.compile(r'^\.delta-stage-[A-Za-z0-9]{8}$')
for stage in sorted(root.iterdir()):
    if stage.is_symlink() or not stage.is_dir() or not stage_re.fullmatch(stage.name):continue
    modified=dt.datetime.fromtimestamp(stage.stat().st_mtime,dt.timezone.utc)
    if modified>=cutoff:continue
    print(('WOULD REMOVE ' if dry_run else 'REMOVE ')+str(stage),flush=True)
    if not dry_run:shutil.rmtree(stage)
for item in sorted(root.iterdir()):
    if item.is_symlink() or not item.is_file() or not archive_re.fullmatch(item.name):continue
    modified=dt.datetime.fromtimestamp(item.stat().st_mtime,dt.timezone.utc)
    if modified>=cutoff:continue
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
print(('DRY RUN ' if dry_run else '') + 'Retention complete: ' + str(len(pruned)) + ' stores', flush=True)
'''


# Local retention uses the same UTC initialization-time cutoff as RZDM.
# Every name must match an exact allow-list; nothing is removed recursively
# unless it is a recognized dated Zarr store or a dated work-cycle directory.
LOCAL_SELECTION_RE = re.compile(
    r'^(rrfs|refs_mean|refs_m00[1-5]|hrrr)_(\d{8})_(00|06|12|18)00\.selection\.json$')
LOCAL_ARCHIVE_RE = re.compile(
    r'^(rrfs|refs_mean|refs_m00[1-5]|hrrr)_(\d{8})_(00|06|12|18)00(?:\.delta)?\.tar(?:\.bz2)?(?:\.tmp)?$')
LOCAL_SUMMARY_RE = re.compile(r'^run_summary_(\d{8})_(00|06|12|18)\.json$')
LOCAL_WORK_CYCLE_RE = re.compile(r'^(\d{8})_(00|06|12|18)$')
LOCAL_LOG_RE = re.compile(r'^(?:multisource|model_incremental|cron_errors|model_cron_errors)_(\d{8})_(\d{6})\.log$')
LOCAL_PUBLISH_STATE_RE = re.compile(r'^(rrfs|refs_mean|refs_m00[1-5]|hrrr)_(\d{8})_(00|06|12|18)00\.zarr\.json$')
LOCAL_OBS_RE = re.compile(r'^(\d{10})\.json$')
REMOTE_STALE_ARCHIVE_RE = re.compile(r'^\.(?:rrfs|refs_mean|refs_m00[1-5]|hrrr)_\d{8}_(?:00|06|12|18)00(?:\.delta)?\.tar\.incoming$')
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
                  now=None, logs_dir=None, protected_stores=(), analysis_rules=()):
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
             'archives': 0, 'work_cycles': 0, 'logs': 0, 'catalog_entries': 0,
             'observations': 0, 'publish_states': 0, 'cron_stdout': 0}

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
            if match and eligible(child, match, 1, 2, model_index=None):
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
                if match and eligible(child, match, 1, 2, fixed_model=model):
                    remove(child, 'work_cycles', True)

    # Remove old hourly observation files only when outside every protected
    # verification valid-time window. One cycle protects F000-F084 here.
    observations=out/'ptype_observations'
    if observations.is_dir() and not observations.is_symlink():
        for child in sorted(observations.iterdir()):
            m=LOCAL_OBS_RE.fullmatch(child.name)
            if not m or child.is_symlink() or not child.is_file():continue
            try: valid=dt.datetime.strptime(m.group(1),'%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
            except ValueError:continue
            if valid<cutoff and not any(start<=m.group(1)<=end for start,end,_ in analysis_rules):
                remove(child,'observations',False)
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
            if end<cutoff and not pinned:remove(child,'observations',False)
    # Remove stale interrupted local SPC JSON writes after configured retention;
    # these files are never treated as valid observations.
    if spc.is_dir() and not spc.is_symlink():
        for child in sorted(spc.iterdir()):
            if child.is_symlink() or not child.is_file() or not re.fullmatch(r'\d{6}\.json\.incoming',child.name):continue
            if dt.datetime.fromtimestamp(child.stat().st_mtime,dt.timezone.utc)<cutoff:
                remove(child,'observations',False)
    published=work/'.published'
    if published.is_dir() and not published.is_symlink():
        for child in sorted(published.iterdir()):
            m=LOCAL_PUBLISH_STATE_RE.fullmatch(child.name)
            if m and eligible(child,m,2,3):remove(child,'publish_states',False)

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
            logging.info('WCOSS2 %s %s -> %s', 'WOULD ROTATE' if dry_run else 'ROTATE',current,dest)
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


def cleanup_remote(host, remote_dir, retention_days=7, dry_run=False, now=None, protected_stores=(), analysis_rules=()):
    """Remove only date-stamped modeled .zarr stores older than retention."""
    validate_publisher(host, remote_dir)
    if not shutil.which('ssh'):
        raise RuntimeError('Remote retention requires ssh')
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = (now - dt.timedelta(days=retention_days)).isoformat().replace('+00:00', 'Z')
    cmd = ('python3 - ' + shlex.quote(remote_dir) + ' ' + shlex.quote(cutoff) + ' ' + ('1' if dry_run else '0') + ' ' + shlex.quote(json.dumps(sorted(protected_stores))) + ' ' + shlex.quote(json.dumps([[start,end] for start,end,_ in analysis_rules])))
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
    p.add_argument('--incremental',action='store_true',
                   help='Availability-driven bounded frontier: fetch new hours and retry missing fields without overwriting existing chunks')
    p.add_argument('--max-hour-jobs',type=int,default=48,
                   help='Maximum forecast-hour jobs in one incremental invocation (default 48)')
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
    p.add_argument('--repair-mslp',action='store_true',
                   help='Add missing native MSLP fields to previously processed model hours, preserving all other Zarr data')
    p.add_argument('--repair-snowfall',action='store_true',
                   help='Restore missing accumulated ASNOW from verified staging or GRIB, preserving completed fields')
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
    p.add_argument('--protected-cycles-file',type=Path,default=DEFAULT_PROTECTED_CYCLES_FILE,
                   help='JSON whitelist of cycles/models preserved indefinitely; required for safe cleanup')
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
        if not 8<=a.max_hour_jobs<=1000:
            raise ValueError('--max-hour-jobs must be 8-1000')
        if not 1<=a.lookahead_hours<=84:
            raise ValueError('--lookahead-hours must be 1-84')
        if not 5<=a.missing_retry_minutes<=1440:
            raise ValueError('--missing-retry-minutes must be 5-1440')
        if a.incremental and (a.overwrite_hours or a.hours is not None):
            raise ValueError('--incremental cannot be combined with --overwrite-hours or --hours')
        if a.incremental and (a.inventory_only or a.catalog_only or a.publish_only or a.cleanup_only):
            raise ValueError('--incremental cannot be combined with inventory/catalog/publish/cleanup-only')

        if sum((a.repair_refs_pressure,a.repair_refs_mean,a.repair_upper_air,a.repair_refs_pmm_apcp,a.repair_snowfall,a.repair_mslp))>1:
            raise ValueError('Select only one repair mode')
        if a.incremental and any((a.repair_refs_pressure,a.repair_refs_mean,a.repair_upper_air,a.repair_refs_pmm_apcp,a.repair_snowfall,a.repair_mslp)):
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


def missing_fields_for_hour(store, model, fhr):
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
        if model=='refs_mean' and name=='absv500':
            # REFS mean omits native ABSV; derive this from mean winds later.
            continue
        if (store/name/f'{fhr}.0.0').is_file():continue
        if (store/'.accum_intervals'/name/f'{fhr}.zlib').is_file():continue
        missing.append(name)
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
    return combined


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
                          protected_stores=a.protected_stores, analysis_rules=a.protected_analysis_rules)
        if not (a.no_publish or a.no_remote_cleanup):
            cleanup_remote(a.publish_host, a.publish_dir, a.retention_days,
                           a.cleanup_dry_run, protected_stores=a.protected_stores, analysis_rules=a.protected_analysis_rules)
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
                    for prefix in ('h','u','v')) + ('absv500','wspd300',) + tuple(SEVERE_SPECS) + tuple(SHEAR_COMPONENTS) + ('shear01','shear06')
                repair_fields=tuple(name for name in selected_names
                    if not (store/name/f'{h}.0.0').is_file())
                if not repair_fields:continue
            elif a.incremental:
                if flags[h]:
                    repair_fields=missing_fields_for_hour(store,model,h)
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
                         repair_fields, 'pressure' if a.repair_refs_pressure else 'mean' if a.repair_refs_mean else 'upper_air' if a.repair_upper_air else 'pmm_apcp' if a.repair_refs_pmm_apcp else 'snowfall' if a.repair_snowfall else 'mslp' if a.repair_mslp else None))
    if a.incremental and len(jobs)>a.max_hour_jobs:
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
        # lowers the risk of exhausting RZDM disk quota during large SCPs.
        if not a.no_remote_cleanup:
            try:
                cleanup_remote(a.publish_host,a.publish_dir,a.retention_days,a.cleanup_dry_run,
                               protected_stores=a.protected_stores, analysis_rules=a.protected_analysis_rules)
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
                publish_one(store,catalog,work,a.publish_host,a.publish_dir,a.keep_publish_archive,publish_mode=a.publish_mode)
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
                          logs_dir=a.logs_dir, protected_stores=a.protected_stores, analysis_rules=a.protected_analysis_rules)
        except Exception as exc:
            published_errors.append(('local_cleanup', str(exc)))
            logging.error('WCOSS2 retention cleanup failed: %s', exc)
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
