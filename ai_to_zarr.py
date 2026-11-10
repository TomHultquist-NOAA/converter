#!/usr/bin/env python3
"""Selective, native-grid AIGFS/AIGEFS/HGEFS/GEFS-mean GRIB2 -> viewer Zarr.

Independent of the RRFS and GFS pollers. Python >=3.10, wgrib2, standard
library only. Downloads *indexed GRIB message ranges*, not ensemble members.
Missing optional variables are never fabricated. GFSv16 F000 is shared from
its existing catalog and is not acquired by this program.
"""
from __future__ import annotations

import argparse
from array import array
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
import datetime as dt
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import sys
import tempfile
import time
import urllib.error
import zlib

for _key in ('OMP_NUM_THREADS', 'OMP_THREAD_LIMIT', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[_key] = '1'

import perf_monitor
import multi_model_to_zarr_wcoss2 as shared
from retention_policy import load_policy

core = shared.core
LOG = logging.getLogger('ai_to_zarr')
MODELS = ('aigfs', 'aigefs_mean', 'hgefs_mean', 'gefs_mean', 'gefs_mean_p25')
# Separate native-resolution stores share one GEFS Mean catalog and panel.
# Keep 0.5-degree pressure fields and prefer 0.25-degree surface fields.
GEFS_PUBLIC_MODEL = 'gefs_mean'
MODEL_LABELS = {'aigfs': 'AIGFS', 'aigefs_mean': 'AIGEFS Mean',
                'hgefs_mean': 'HGEFS Mean', 'gefs_mean': 'GEFS Mean',
                'gefs_mean_p25': 'GEFS Mean'}
MAX_HOUR = {'aigfs': 384, 'aigefs_mean': 384, 'hgefs_mean': 240, 'gefs_mean': 384, 'gefs_mean_p25': 240}
# Operational AI products are on the shared filesystem. Read them there by
# default; never make a NOMADS request for these models unless explicitly
# requested (--ai-source remote or --nomads-fallback).
LOCAL_BASES = {
    'aigfs': '/lfs/h1/ops/prod/com/aigfs/v1.1',
    'aigefs_mean': '/lfs/h1/ops/prod/com/aigefs/v1.0',
    'hgefs_mean': '/lfs/h1/ops/prod/com/hgefs/v1.0',
}
LOCAL_MODELS = frozenset(LOCAL_BASES)
BASES = {
    'aigfs': 'https://nomads.ncep.noaa.gov/pub/data/nccf/com/aigfs/prod',
    'aigefs_mean': 'https://nomads.ncep.noaa.gov/pub/data/nccf/com/aigefs/prod',
    'hgefs_mean': 'https://nomads.ncep.noaa.gov/pub/data/nccf/com/hgefs/prod',
    'gefs_mean': 'https://noaa-gefs-pds.s3.amazonaws.com',
    'gefs_mean_p25': 'https://noaa-gefs-pds.s3.amazonaws.com',
}
WEST, EAST, SOUTH, NORTH = 160.0, 310.0, 5.0, 75.0
CYCLE_RE = re.compile(r'^(00|06|12|18)$')
STORE_RE = re.compile(r'^(aigfs|aigefs_mean|hgefs_mean|gefs_mean_p25|gefs_mean)_(\d{8})_(00|06|12|18)00\.zarr$')
CHUNK_RE = re.compile(r'^(\d+)\.0\.0$')
# One source record can be used by multiple derived fields. The exact level
# comparison prevents 1000-hPa temperatures from matching a 100-hPa record.
NATIVE = {
    'h250': ('HGT', '250 mb'), 'u250': ('UGRD', '250 mb'), 'v250': ('VGRD', '250 mb'),
    'h300': ('HGT', '300 mb'), 'u300': ('UGRD', '300 mb'), 'v300': ('VGRD', '300 mb'),
    'h500': ('HGT', '500 mb'), 'u500': ('UGRD', '500 mb'), 'v500': ('VGRD', '500 mb'),
    't700': ('TMP', '700 mb'), 'h700': ('HGT', '700 mb'),
    'u700': ('UGRD', '700 mb'), 'v700': ('VGRD', '700 mb'),
    'q700': ('SPFH', '700 mb'), 'td700': ('DPT', '700 mb'),
    't850': ('TMP', '850 mb'), 'h850': ('HGT', '850 mb'),
    'u850': ('UGRD', '850 mb'), 'v850': ('VGRD', '850 mb'),
    'q850': ('SPFH', '850 mb'), 'td850': ('DPT', '850 mb'),
    't925': ('TMP', '925 mb'), 'h925': ('HGT', '925 mb'),
    'q925': ('SPFH', '925 mb'), 'td925': ('DPT', '925 mb'),
    'u925': ('UGRD', '925 mb'), 'v925': ('VGRD', '925 mb'),
    't1000': ('TMP', '1000 mb'), 'h1000': ('HGT', '1000 mb'),
    'u1000': ('UGRD', '1000 mb'), 'v1000': ('VGRD', '1000 mb'),
    'mslp': ('PRMSL', 'mean sea level'), 'psfc': ('PRES', 'surface'), 't2m': ('TMP', '2 m above ground'),
    'dpt2m': ('DPT', '2 m above ground'),
    'u10': ('UGRD', '10 m above ground'), 'v10': ('VGRD', '10 m above ground'),
    'apcp': ('APCP', 'surface'),
}
UNITS = {name: ('gpm' if name.startswith('h') else 'K' if name in ('t700','t850','t925','t1000','t2m','dpt2m','td700','td850','td925')
                else 'Pa' if name in ('mslp','psfc') else 'kg kg-1' if name in ('q2m','q700','q850','q925')
                else 'kg m-2' if name == 'apcp' else 'm s-1') for name in NATIVE}
UNITS.update({'wspd250': 'm s-1', 'wspd300': 'm s-1', 'wspd10': 'm s-1', 'absv500': 's-1'})


def spacing(model):
    return 0.5 if model == 'gefs_mean' else 0.25


def dimensions(model):
    d = spacing(model)
    return (int(round((EAST-WEST)/d))+1, int(round((NORTH-SOUTH)/d))+1)


def grid_attrs(model):
    dx = spacing(model)
    nx, ny = dimensions(model)
    return {'GRIB_gridType': 'regular_ll', 'GRIB_Nx': nx, 'GRIB_Ny': ny,
            'GRIB_DxInDegrees': dx, 'GRIB_DyInDegrees': dx,
            'GRIB_longitudeOfFirstGridPointInDegrees': WEST,
            'GRIB_longitudeOfLastGridPointInDegrees': EAST,
            'GRIB_latitudeOfFirstGridPointInDegrees': SOUTH,
            'GRIB_latitudeOfLastGridPointInDegrees': NORTH}


def source_urls(model, date, cycle, hour, bases=None):
    if model not in MODELS or not CYCLE_RE.fullmatch(str(cycle)):
        raise ValueError('Invalid AI model or cycle')
    base = ((bases or {}).get(model) or BASES[model]).rstrip('/')
    h = f'{hour:03d}'
    if model == 'aigfs':
        root = f'{base}/aigfs.{date}/{cycle}/model/atmos/grib2/aigfs.t{cycle}z.'
        return {'pres': f'{root}pres.f{h}.grib2', 'sfc': f'{root}sfc.f{h}.grib2'}
    if model in ('aigefs_mean','hgefs_mean'):
        family = 'aigefs' if model == 'aigefs_mean' else 'hgefs'
        root = f'{base}/{family}.{date}/{cycle}/ensstat/products/atmos/grib2/{family}.t{cycle}z.'
        return {'pres': f'{root}pres.avg.f{h}.grib2', 'sfc': f'{root}sfc.avg.f{h}.grib2'}
    if model == 'gefs_mean_p25':
        root = f'{base}/gefs.{date}/{cycle}/atmos/pgrb2sp25/'
        return {'surface025': f'{root}geavg.t{cycle}z.pgrb2s.0p25.f{h}'}
    root = f'{base}/gefs.{date}/{cycle}/atmos/pgrb2ap5/'
    return {'combined': f'{root}geavg.t{cycle}z.pgrb2a.0p50.f{h}'}


def source_local_paths(model, date, cycle, hour, local_bases=None):
    """Build local operational input paths without assuming .idx sidecars."""
    if model not in LOCAL_MODELS:
        return {}
    base = (local_bases or {}).get(model) or LOCAL_BASES[model]
    return {part: Path(value) for part, value in
            source_urls(model, date, cycle, hour, {model: str(base)}).items()}


@perf_monitor.timed_task('ai_local_inventory', include_metrics=False)
def local_inventory(path, wgrib2):
    """Inventory a read-only local GRIB; .idx if present, wgrib2 -s otherwise.

    Never write generated indexes into operational production directories.
    A changing/incomplete GRIB is retried on a later poll, not published.
    """
    path = Path(path)
    stat_before = path.stat()
    if not path.is_file() or stat_before.st_size < 16:
        raise OSError('Local GRIB is missing or empty: ' + str(path))
    idx_path = Path(str(path) + '.idx')
    if idx_path.is_file():
        inventory = idx_path.read_text(errors='replace')
    else:
        proc = subprocess.run([wgrib2, str(path), '-s'], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        inventory = proc.stdout
    stat_after = path.stat()
    if (stat_before.st_size != stat_after.st_size or
            stat_before.st_mtime_ns != stat_after.st_mtime_ns):
        raise OSError('Local GRIB changed while inventorying: ' + str(path))
    rows = core.parse_idx(inventory, stat_after.st_size)
    if any(row['offset'] < 0 or row['end'] >= stat_after.st_size or
           row['end'] < row['offset'] for row in rows):
        raise OSError('Local GRIB/index offsets do not match the file: ' + str(path))
    return rows


@perf_monitor.timed_task('ai_local_acquisition', include_metrics=False)
def copy_local_selected(path, ordered, output_path):
    """Copy only selected GRIB messages from local data, streaming ranges."""
    chosen = [{'record': item['record']} for item in ordered]
    groups = []
    for item in chosen:
        if groups and item['record']['msg'] == groups[-1][-1]['record']['msg'] + 1:
            groups[-1].append(item)
        else:
            groups.append([item])
    with Path(path).open('rb') as src, Path(output_path).open('wb') as dst:
        for group in groups:
            start = group[0]['record']['offset']
            end = group[-1]['record']['end']
            if start < 0 or end < start:
                raise ValueError('Invalid local GRIB index range')
            src.seek(start)
            remaining = end - start + 1
            while remaining:
                buf = src.read(min(4 * 1024 * 1024, remaining))
                if not buf:
                    raise IOError('Local GRIB became truncated during a selected read')
                dst.write(buf)
                remaining -= len(buf)


def select_records(rows, hour, model):
    result = {}
    # A GRIB index can contain hundreds of records. Bucket each message once
    # instead of rescanning the full inventory for every requested variable.
    by_short = {}
    for record in rows:
        by_short.setdefault(record['short_name'].upper(), []).append(record)
    for name, (short, level) in NATIVE.items():
        marker = ':' + level.lower() + ':'
        matches = [r for r in by_short.get(short, ()) if marker in r['text'].lower()]
        if name == 'mslp' and not matches:
            matches = [r for alias in ('MSLET','MSLMA') for r in by_short.get(alias, ())
                       if ':mean sea level:' in r['text'].lower()]
        if name == 'apcp':
            if hour == 0:
                continue
            timed = [(r, shared.parse_acc_interval(r['text'], hour)) for r in matches]
            timed = [(r, it) for r, it in timed if isinstance(it, tuple) and
                     it[1] == hour and 0 <= it[0] < hour]
            if not timed:
                continue
            # A 0-F accumulation is already a run total. Otherwise prefer a
            # complete six-hour interval to limit cumulative dependencies.
            rec, interval = max(timed, key=lambda pair:
                (pair[1][0] == 0, pair[1][1]-pair[1][0] == 6,
                 pair[1][1]-pair[1][0], -pair[0]['msg']))
            result[name] = (rec, interval)
        elif matches:
            result[name] = (min(matches,key=lambda r:r['msg']), None)
    return result


def pack(values):
    vals = array('f', values)
    if sys.byteorder != 'little': vals.byteswap()
    return vals.tobytes()


def floats(raw, npts):
    out = array('f'); out.frombytes(raw)
    if sys.byteorder != 'little': out.byteswap()
    if len(out) != npts:
        raise ValueError('Invalid cropped native-grid array length')
    return out


def wind_speed(u, v, npts):
    a, b = floats(u, npts), floats(v, npts)
    return pack(math.hypot(x, y) if math.isfinite(x) and math.isfinite(y) else math.nan
                for x, y in zip(a,b))


def vorticity_500(u, v, nx, ny, step):
    """Absolute vorticity on regular lat/lon, in s^-1, including metric term.

    Only calculate valid cells; do not replace missing winds with zero. This
    diagnoses the *ensemble mean wind* in the mean panels, not mean vorticity.
    """
    uu, vv = floats(u,nx*ny), floats(v,nx*ny)
    result = array('f', [math.nan]) * (nx*ny)
    rad = math.pi/180.0
    radius = 6371229.0
    for j in range(ny):
        phi = (SOUTH+j*step)*rad
        cosphi = math.cos(phi)
        if abs(cosphi) < 0.01: continue
        jm, jp = max(0,j-1), min(ny-1,j+1)
        delta_y = (jp-jm)*step*rad*radius
        for i in range(nx):
            im, ip = max(0,i-1), min(nx-1,i+1)
            here=j*nx+i; vl=j*nx+im; vr=j*nx+ip
            ub=jm*nx+i; ut=jp*nx+i
            values=(uu[here],uu[ub],uu[ut],vv[here],vv[vl],vv[vr])
            if any(not math.isfinite(x) or abs(x)>500 for x in values):continue
            delta_x=(ip-im)*step*rad*radius*cosphi
            if not delta_x or not delta_y:continue
            dv_dx=(vv[vr]-vv[vl])/delta_x
            du_dy=(uu[ut]-uu[ub])/delta_y
            result[here]=dv_dx-du_dy+uu[here]*math.tan(phi)/radius+2*7.2921159e-5*math.sin(phi)
    return pack(result)


@perf_monitor.timed_task('ai_grib_decode', lambda a,k: a[5], include_metrics=False)
def cropped_ieee(wgrib2, selected, cropped, ieee, nrec, model):
    # Retain native grid points; never bilinearly regrid ensemble mean products.
    subprocess.run([wgrib2, str(selected), '-small_grib',f'{WEST:g}:{EAST:g}',
                    f'{SOUTH:g}:{NORTH:g}',str(cropped)],check=True,stdout=subprocess.DEVNULL)
    proc=subprocess.run([wgrib2,str(cropped),'-d','1','-grid'],check=True,
                        stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    nx, ny=dimensions(model)
    found=re.search(r'\(\s*(\d+)\s*x\s*(\d+)\s*\)',proc.stdout)
    if not found or (int(found.group(1)),int(found.group(2)))!=(nx,ny):
        raise ValueError('Unexpected '+model+' native cropped grid: '+proc.stdout[:500])
    core.decode_to_ieee(wgrib2,cropped,ieee)
    size=ieee.stat().st_size
    if size!=nrec*nx*ny*4:
        raise ValueError(f'{model}: unexpected decoded IEEE bytes {size}, expected {nrec*nx*ny*4}')


def decode_inventory(rows, hour, model, wgrib2, scratch, populate_subset):
    chosen = select_records(rows, hour, model)
    if not chosen:
        return {'available': True, 'records': {}, 'reason': 'No selected variables'}
    by_num = {}
    for name, (r, interval) in chosen.items():
        by_num.setdefault(r['msg'], {'record': r, 'names': []})['names'].append((name, interval))
    ordered = sorted(by_num.values(), key=lambda item: item['record']['msg'])
    selected = scratch / 'subset.grib2'
    cropped = scratch / 'crop.grib2'
    ieee = scratch / 'fields.ieee'
    populate_subset(ordered, selected)
    cropped_ieee(wgrib2, selected, cropped, ieee, len(ordered), model)
    nx, ny = dimensions(model)
    found = {}
    with ieee.open('rb') as inp:
        for item in ordered:
            raw = inp.read(nx * ny * 4)
            if len(raw) != nx * ny * 4:
                raise IOError('Short decoded GRIB field')
            for name, interval in item['names']:
                found[name] = (raw, interval)
    return {'available': True, 'records': found, 'reason': 'ok',
            'inventory_size': len(rows)}


@perf_monitor.timed_task('ai_remote_part', lambda a,k: '%s F%03d' % (a[3],a[2]), include_metrics=False)
def retrieve_part(client, url, hour, model, wgrib2, scratch):
    """HTTP/S3 acquisition (GEFS always, other AI only when opted in)."""
    try:
        idx = client.text(url + '.idx')
        length = client.size(url)
        rows = core.parse_idx(idx, length)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, RuntimeError, OSError) as exc:
        return {'available': False, 'reason': str(exc), 'records': {}}
    return decode_inventory(rows, hour, model, wgrib2, scratch,
        lambda ordered, dest: core.download_partial(client, url,
            [{'record': item['record']} for item in ordered], dest))


@perf_monitor.timed_task('ai_local_part', lambda a,k: '%s F%03d' % (a[2],a[1]), include_metrics=False)
def retrieve_local_part(path, hour, model, wgrib2, scratch):
    """Acquire just the required records from the shared operational GRIB."""
    try:
        rows = local_inventory(path, wgrib2)
        return decode_inventory(rows, hour, model, wgrib2, scratch,
            lambda ordered, dest: copy_local_selected(path, ordered, dest))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        return {'available': False, 'reason': 'Local file: ' + str(exc), 'records': {}}


@perf_monitor.timed_task('ai_forecast_hour', lambda a,k: '%s %s%s F%03d' % (a[0],a[1],a[2],a[3]))
def process_hour(model, date, cycle, hour, bases, wgrib2, timeout, retries,
                 local_bases=None, ai_source='local', nomads_fallback=False,
                 network_gate=None):
    client = core.HTTPClient(timeout=timeout, retries=retries)
    records = {}
    parts = {}
    local_files = (source_local_paths(model, date, cycle, hour, local_bases)
                   if ai_source == 'local' else {})
    with tempfile.TemporaryDirectory(prefix=f'evu_ai_{model}_{date}_{cycle}_{hour:03d}_') as td:
        for part, url in source_urls(model, date, cycle, hour, bases).items():
            scratch = Path(td) / part
            scratch.mkdir()
            try:
                if part in local_files:
                    result = retrieve_local_part(local_files[part], hour, model, wgrib2, scratch)
                    if not result['available'] and nomads_fallback:
                        # Remote requests have a separate, shared concurrency
                        # ceiling, even when local workers are much higher.
                        LOG.info('%s F%03d %s local unavailable; trying NOMADS: %s',
                                 model, hour, part, result['reason'])
                        with (network_gate or nullcontext()):
                            result = retrieve_part(client, url, hour, model, wgrib2, scratch)
                else:
                    with (network_gate or nullcontext()):
                        result = retrieve_part(client, url, hour, model, wgrib2, scratch)
                parts[part] = result['available']
                for name, item in result['records'].items():
                    # Surface source takes precedence for surface fields;
                    # pressure source takes precedence for isobaric fields.
                    prefer_sfc = name in ('mslp', 'psfc', 't2m', 'dpt2m', 'q2m', 'u10', 'v10', 'apcp')
                    if name not in records or (part == 'sfc' and prefer_sfc):
                        records[name] = item
                if not result['available']:
                    LOG.info('%s %s F%03d %s unavailable: %s', model,
                             date + cycle, hour, part, result['reason'])
            except Exception as exc:
                LOG.warning('%s F%03d %s processing failed: %s', model, hour, part, exc)
                parts[part] = False
    result_status = ('complete' if parts and all(parts.values()) else
                     ('partial' if any(parts.values()) else 'unavailable'))
    return {'model': model, 'hour': hour, 'records': records, 'parts': parts,
            'status': result_status}


def create_store(store,model,date,cycle,compression):
    nx,ny=dimensions(model);max_h=MAX_HOUR[model];space=spacing(model)
    init=shared.iso_init(date,cycle)
    attrs={
        'model':MODEL_LABELS[model], 'model_id':model, 'domain':'North America / Alaska / Hawaii / Puerto Rico',
        'native_grid':True, 'regridded':False, 'grid_spacing_degrees':space,
        'grid_order':'WE:SN','init_time':init.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'source':('NODD GEFS geavg 0.25 degree surface' if model=='gefs_mean_p25'
                  else 'NODD GEFS geavg 0.5 degree' if model=='gefs_mean'
                  else 'NOMADS ensemble statistics / AIGFS'),
        'dimensions':{'valid_time':max_h+1,'y':ny,'x':nx},**grid_attrs(model)}
    store.mkdir(parents=True,exist_ok=True)
    if (store/'.zattrs').is_file():
        old=json.loads((store/'.zattrs').read_text())
        if old.get('model_id')!=model or old.get('init_time')!=attrs['init_time'] or any(old.get(k)!=v for k,v in grid_attrs(model).items()):
            raise ValueError('Existing AI store has incompatible grid or init; refusing overwrite')
    else:
        core.json_write(store/'.zgroup',{'zarr_format':2})
        core.json_write(store/'.zattrs',attrs)
    hours=max_h+1
    for field,length,dtype,raw,unit,dim in (
        ('forecast_hour',hours,'<i2',core.pack_int16_le(range(hours)),'hours','valid_time'),
        ('valid_time',hours,'<i8',core.pack_int64_le(int((init+dt.timedelta(hours=h)).timestamp()) for h in range(hours)),'seconds since 1970-01-01','valid_time'),
        ('latitude',ny,'<f4',pack(SOUTH+space*j for j in range(ny)),'degrees_north','y'),
        ('longitude',nx,'<f4',pack(WEST+space*i for i in range(nx)),'degrees_east','x')):
        core.ensure_array(store,field,(length,),(length,),dtype,None,
                          {'_ARRAY_DIMENSIONS':[dim],'units':unit},compression)
        if not (store/field/'0').is_file():core.write_compressed_chunk(store/field/'0',raw,compression)
    core.ensure_array(store,'processed',(hours,),(hours,),'|u1',0,
                      {'_ARRAY_DIMENSIONS':['valid_time']},compression)
    if not (store/'processed'/'0').exists():
        core.write_compressed_chunk(store/'processed'/'0',bytes(hours),compression)


def ensure_field(store,model,name,compression):
    nx,ny=dimensions(model); length=MAX_HOUR[model]+1
    short,level=NATIVE.get(name,('DERIVED','derived'))
    core.ensure_array(store,name,(length,ny,nx),(1,ny,nx),'<f4','NaN',
        {'_ARRAY_DIMENSIONS':['valid_time','y','x'],'long_name':name,
         'units':UNITS[name],'GRIB_shortName':short,'GRIB_level':level,
         **({'derivation':'native U/V spherical finite difference plus Coriolis',
             'source_fields':['u500','v500']} if name=='absv500' else {})},compression)


@perf_monitor.timed_task('ai_zarr_commit', lambda a,k: '%s F%03d' % (a[1],a[2]['hour']))
def commit_hour(store,model,result,compression):
    h=result['hour'];nx,ny=dimensions(model);n=nx*ny
    new=[];pending=[]
    for name,(raw,interval) in result['records'].items():
        target=store/name/f'{h}.0.0'
        if target.is_file(): continue
        if name=='apcp':
            start,end=interval
            # Only stage unresolved accumulation intervals. Native 0-F totals
            # and complete windows do not need duplicate temporary storage.
            if start:
                prior=store/'apcp'/f'{start}.0.0'
                if not prior.is_file():
                    staged=store/'.accum_intervals'/'apcp'/f'{h}.zlib'
                    core.write_compressed_chunk(staged,raw,compression)
                    pending.append(h)
                    continue
                a,b=floats(zlib.decompress(prior.read_bytes()),n),floats(raw,n)
                raw=pack(x+y if math.isfinite(x) and math.isfinite(y) else math.nan for x,y in zip(a,b))
        ensure_field(store,model,name,compression)
        core.write_compressed_chunk(target,raw,compression)
        new.append(name)
    def derive(out,from_u,from_v,fn):
        dest=store/out/f'{h}.0.0'
        if dest.is_file():return
        up=store/from_u/f'{h}.0.0';vp=store/from_v/f'{h}.0.0'
        if not up.is_file() or not vp.is_file():return
        data=fn(zlib.decompress(up.read_bytes()),zlib.decompress(vp.read_bytes()))
        ensure_field(store,model,out,compression)
        core.write_compressed_chunk(dest,data,compression)
        new.append(out)
    derive('wspd10','u10','v10',lambda u,v:wind_speed(u,v,n))
    derive('wspd250','u250','v250',lambda u,v:wind_speed(u,v,n))
    derive('wspd300','u300','v300',lambda u,v:wind_speed(u,v,n))
    from upper_air_moisture import derive_dewpoint_bytes
    for pressure in (700,850,925):
        output=f'td{pressure}'
        dest=store/output/f'{h}.0.0'
        if dest.is_file():continue
        q=store/f'q{pressure}'/f'{h}.0.0'
        t=store/f't{pressure}'/f'{h}.0.0'
        if not(q.is_file() and t.is_file()):continue
        result=derive_dewpoint_bytes(zlib.decompress(q.read_bytes()),zlib.decompress(t.read_bytes()),pressure)
        ensure_field(store,model,output,compression)
        core.write_compressed_chunk(dest,result,compression)
        new.append(output)
    derive('absv500','u500','v500',lambda u,v:vorticity_500(u,v,nx,ny,spacing(model)))
    return new,pending


def recover_precip(store,model,compression):
    """Recover cumulative totals when earlier lead arrives after later lead."""
    candidates=sorted((store/'.accum_intervals'/'apcp').glob('*.zlib')) if (store/'.accum_intervals'/'apcp').exists() else []
    changed=[]; nx,ny=dimensions(model);n=nx*ny
    for item in candidates:
        h=int(item.stem)
        target=store/'apcp'/f'{h}.0.0'
        if target.exists():continue
        # Interval metadata is written in a separate file by the coordinator.
        meta=store/'.accum_intervals'/'apcp'/f'{h}.json'
        if not meta.exists():continue
        start,end=json.loads(meta.read_text())
        if end!=h:continue
        raw=zlib.decompress(item.read_bytes())
        if start:
            prior=store/'apcp'/f'{start}.0.0'
            if not prior.is_file():continue
            a,b=floats(zlib.decompress(prior.read_bytes()),n),floats(raw,n)
            raw=pack(x+y if math.isfinite(x) and math.isfinite(y) else math.nan for x,y in zip(a,b))
        ensure_field(store,model,'apcp',compression)
        core.write_compressed_chunk(target,raw,compression)
        changed.append(h)
        # The published run-total supersedes the temporary interval. Reclaim
        # scratch disk once the cumulative field has been safely written.
        item.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
    return changed


def scan_stores(output,protected=(),retention_days=7):
    cutoff=dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=retention_days)
    results={name:[] for name in MODELS}
    for store in sorted(output.iterdir()) if output.is_dir() else []:
        m=STORE_RE.fullmatch(store.name)
        if not m or not store.is_dir() or store.is_symlink():continue
        model,day,cc=m.groups()
        if shared.iso_init(day,cc)<cutoff and store.name not in protected:continue
        try:
            attrs=json.loads((store/'.zattrs').read_text())
            done=core.read_processed(store,MAX_HOUR[model]+1) if hasattr(core,'read_processed') else shared.read_processed(store,MAX_HOUR[model]+1)
            valid={h for h,x in enumerate(done) if x}
            if not valid:continue
            variables=[]
            for field in store.iterdir():
                if not field.is_dir() or not (field/'.zarray').is_file() or field.name in ('processed','forecast_hour','valid_time','latitude','longitude'):continue
                meta=json.loads((field/'.zarray').read_text())
                hours=sorted(int(mat.group(1)) for f in field.iterdir() if f.is_file() and (mat:=CHUNK_RE.fullmatch(f.name)) and int(mat.group(1)) in valid)
                if hours: variables.append({'name':field.name,'shape':meta['shape'],'chunks':meta['chunks'],
                    'dtype':meta['dtype'],'compressor':meta['compressor'],
                    'attrs':json.loads((field/'.zattrs').read_text()),'available_hours':hours})
            if variables:results[model].append({'id':store.stem,'path':store.name,'model':attrs['model'],
                'domain':attrs['domain'],'init_time':attrs['init_time'],'grid':grid_attrs(model),
                'variables':variables,'is_case':store.name in protected})
        except (OSError,ValueError,KeyError,zlib.error) as exc:
            LOG.warning('Skipping incomplete catalog store %s: %s',store,exc)
    catalogs={}
    for model in MODELS:
        if model=='gefs_mean_p25':
            continue  # GEFS resolution variants belong in the SAME viewer source.
        datasets=list(results[model])
        if model=='gefs_mean':
            datasets.extend(results['gefs_mean_p25'])
            # Stable within each initialization: prefer native 0.25 degree.
            datasets.sort(key=lambda d:(d['init_time'],
                -float(d['grid']['GRIB_DxInDegrees'])),reverse=True)
        else:
            datasets.sort(key=lambda d:d['init_time'],reverse=True)
        catalog=output/f'catalog_{model}.json'
        core.json_write(catalog,{'schema':'rrfs-zarr-viewer-1',
            'generated_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
            'datasets':datasets})
        catalogs[model]=catalog
    catalogs['gefs_mean_p25']=catalogs['gefs_mean']
    return catalogs


def cleanup_local(out,work,protected,retention_days,dry=False):
    cutoff=dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=retention_days)
    removed=[]
    for root,pattern in ((out,'*.zarr'),(work/'.published','*.zarr.json'),(work/'ai_manifests','*.json')):
        if not root.is_dir() or root.is_symlink():continue
        for obj in root.glob(pattern):
            stem=obj.name[:-5] if obj.name.endswith('.json') else obj.name
            match=STORE_RE.fullmatch(stem)
            if not match or obj.is_symlink():continue
            model,day,cyc=match.groups()
            if shared.iso_init(day,cyc)>=cutoff or stem in protected:continue
            removed.append(str(obj))
            if not dry:
                if obj.is_dir():shutil.rmtree(obj)
                else:obj.unlink()
    return removed


@perf_monitor.timed_task('ai_catalog_publication')
def publish_catalog(catalog,host,remote_dir):
    import shlex
    import publish_transport
    shared.validate_publisher(host,remote_dir)
    target='.'+catalog.name+'.'+str(os.getpid())+'.incoming'
    publish_transport.run(['scp',str(catalog),host+':'+remote_dir+'/'+target],check=True)
    publish_transport.run(['ssh',host,'cd '+shlex.quote(remote_dir)+' && mv -f '+shlex.quote(target)+' '+shlex.quote(catalog.name)],check=True)


@perf_monitor.timed_task('ai_remote_cleanup')
def remote_cleanup(host,remote_dir,protected,days,dry=False):
    import publish_transport
    import shlex
    shared.validate_publisher(host,remote_dir)
    cutoff=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=days)).strftime('%Y%m%d%H')
    # Narrow allowlist: never touch RRFS, GFS, verification or historical cases.
    script=r'''import pathlib,re,shutil,sys
root=pathlib.Path(sys.argv[1]);cutoff=sys.argv[2];dry=sys.argv[3]=='1';protected=set(sys.argv[4].split(','))
pattern=re.compile(r'^(aigfs|aigefs_mean|hgefs_mean|gefs_mean_p25|gefs_mean)_(\d{8})_(00|06|12|18)00\.zarr$')
if root.name!='data' or root.is_symlink() or not root.is_dir():raise SystemExit('Unsafe AI remote root')
for p in root.iterdir():
 m=pattern.fullmatch(p.name)
 if m and p.is_dir() and not p.is_symlink() and m.group(2)+m.group(3)<cutoff and p.name not in protected:
  print(('WOULD REMOVE ' if dry else 'REMOVE ')+str(p))
  if not dry:shutil.rmtree(p)
'''
    publish_transport.run(['ssh',host,'python3 -c '+shlex.quote(script)+' '+
        ' '.join(shlex.quote(x) for x in (remote_dir,cutoff,'1' if dry else '0',','.join(sorted(protected))))],check=True)


def parse_hours(value,model):
    max_h=MAX_HOUR[model]
    # Default to the six-panel shared horizon; optional --hours may request
    # the published F384 tail for AIGFS, AIGEFS and GEFS independently.
    if not value:return tuple(range(0,min(max_h,240)+1,6))
    hours=set()
    for part in value.split(','):
        if '-' in part:
            a,b=map(int,part.split('-',1))
            if a>b or a%6 or b%6:raise ValueError('Invalid six-hourly range')
            hours.update(range(a,b+1,6))
        else:hours.add(int(part))
    if not hours or any(h<0 or h>max_h or h%6 for h in hours):
        raise ValueError('AI forecast hours must be multiples of 6 in model horizon')
    return tuple(sorted(hours))


@perf_monitor.monitored('ai_conversion')
def run(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date',required=True,help='YYYYMMDD')
    parser.add_argument('--cycle',required=True,choices=('00','06','12','18'))
    parser.add_argument('--models',default='all',help='all or comma-separated '+','.join(MODELS))
    parser.add_argument('--hours',help='comma/range of 6-hourly forecast leads, default 0..model maximum')
    parser.add_argument('--output-root',type=Path,default=Path.cwd()/'zarr')
    parser.add_argument('--work-root',type=Path,default=Path.cwd()/'work')
    parser.add_argument('--wgrib2',default=None)
    parser.add_argument('--workers',type=int,default=None,
                        help='Legacy worker override for both local and remote streams')
    parser.add_argument('--local-workers',type=int,default=None,
                        help='Concurrent hours for local AIGFS/AIGEFS/HGEFS (default 8)')
    parser.add_argument('--remote-workers',type=int,default=None,
                        help='Concurrent GEFS AWS hours / NOMADS fallback (default 3)')
    parser.add_argument('--ai-source',choices=('local','remote'),default='local',
                        help='Use local operational AI files by default, or force NOMADS')
    parser.add_argument('--nomads-fallback',action='store_true',
                        help='Opt-in remote fallback for missing local AI files')
    parser.add_argument('--max-hour-jobs',type=int,default=0)
    parser.add_argument('--compression-level',type=int,default=3)
    parser.add_argument('--retry-failed-minutes',type=int,default=45)
    parser.add_argument('--timeout',type=int,default=60)
    parser.add_argument('--retries',type=int,default=2)
    parser.add_argument('--retention-days',type=int,default=7)
    parser.add_argument('--protected-cycles-file',type=Path,default=Path(__file__).with_name('protected_cycles.json'))
    parser.add_argument('--publish-host',default=shared.core.DEFAULT_PUBLISH_HOST)
    parser.add_argument('--publish-dir',default=shared.core.DEFAULT_PUBLISH_DIR)
    parser.add_argument('--no-publish',action='store_true')
    parser.add_argument('--no-remote-cleanup',action='store_true')
    parser.add_argument('--cleanup-only',action='store_true')
    parser.add_argument('--cleanup-dry-run',action='store_true')
    parser.add_argument('--inventory-only',action='store_true')
    parser.add_argument('--force-hours',action='store_true')
    parser.add_argument('--plan',action='store_true',help='No network or data writes; print URLs and selection')
    parser.add_argument('--verbose',action='store_true')
    for model in MODELS: parser.add_argument('--'+model.replace('_','-')+'-base',default=None)
    for model in LOCAL_MODELS:
        parser.add_argument('--'+model.replace('_','-')+'-local-base',default=None)
    a=parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    try:
        dt.datetime.strptime(a.date,'%Y%m%d')
        models=MODELS if a.models=='all' else tuple(dict.fromkeys(x.strip() for x in a.models.split(',')))
        if not models or any(model not in MODELS for model in models):raise ValueError('Unknown AI models')
        # Existing AI_MODELS=gefs_mean configurations automatically acquire
        # the 0.25-degree companion; no cron or config change is required.
        if 'gefs_mean' in models and 'gefs_mean_p25' not in models:
            models=models+('gefs_mean_p25',)
        a.local_workers = a.local_workers or a.workers or 8
        a.remote_workers = a.remote_workers or a.workers or 3
        if not (a.local_workers >= 1 and a.remote_workers >= 1 and
                a.max_hour_jobs>=0 and 1<=a.retention_days<=366 and
                0<=a.compression_level<=9 and 1<=a.retry_failed_minutes<=1440):raise ValueError('Invalid worker/retention options')
        selected={m:(tuple(h for h in parse_hours(a.hours,'gefs_mean') if h<=MAX_HOUR[m])
                     if m=='gefs_mean_p25' and a.hours else parse_hours(a.hours,m))
                  for m in models}
        a.wgrib2=(core.find_wgrib2(a.wgrib2) if not (a.plan or a.cleanup_only) else a.wgrib2)
        # No missing whitelist means 'unprotected': fail closed instead.
        policy=load_policy(a.protected_cycles_file) if not a.plan else None
        protected=policy.model_stores if policy else frozenset()
    except (OSError,ValueError,RuntimeError) as exc:
        parser.error(str(exc))
    perf_monitor.record_event('run_config', workflow='ai_conversion', date=a.date, cycle=a.cycle,
        models=list(models), hours_by_model={key:len(value) for key,value in selected.items()},
        local_workers=a.local_workers, remote_workers=a.remote_workers,
        max_hour_jobs=a.max_hour_jobs, source_mode=a.ai_source,
        nomads_fallback=a.nomads_fallback, publish=not a.no_publish,
        inventory_only=a.inventory_only)
    bases={m:getattr(a,m+'_base',None) for m in MODELS}
    local_bases = {m: getattr(a, m+'_local_base', None) for m in LOCAL_MODELS}
    network_gate = threading.BoundedSemaphore(a.remote_workers)
    if not bases['gefs_mean_p25']:
        bases['gefs_mean_p25']=bases['gefs_mean']
    if a.plan:
        for model,hours in selected.items():
            print(model, 'resolution',spacing(model),'degrees; hours',len(hours),
                  'F006 remote:', source_urls(model,a.date,a.cycle,6,bases))
            local = (source_local_paths(model,a.date,a.cycle,6,local_bases)
                     if a.ai_source == 'local' else {})
            if local:
                print('  F006 local:', {k:str(v) for k,v in local.items()},
                      'NOMADS fallback:', bool(a.nomads_fallback))
            print('  workers:', a.local_workers if model in LOCAL_MODELS and a.ai_source == 'local'
                  else a.remote_workers)
        return 0
    out=a.output_root.expanduser().resolve();work=a.work_root.expanduser().resolve()
    if len(out.parts)<3 or len(work.parts)<3 or out==work or out.is_symlink() or work.is_symlink():
        raise ValueError('Unsafe AI output/work directories')
    out.mkdir(parents=True,exist_ok=True);work.mkdir(parents=True,exist_ok=True)
    changed=[]
    if not a.cleanup_only:
        for model,hours in selected.items():
            store=out/f'{model}_{a.date}_{a.cycle}00.zarr'
            if not a.inventory_only:create_store(store,model,a.date,a.cycle,a.compression_level)
            flags=shared.read_processed(store,MAX_HOUR[model]+1) if store.is_dir() else bytes(MAX_HOUR[model]+1)
            mpath=work/'ai_manifests'/f'{store.name}.json'
            manifest=json.loads(mpath.read_text()) if mpath.is_file() else {'schema':1,'hours':{}}
            now=dt.datetime.now(dt.timezone.utc)
            def eligible(h):
                if a.force_hours:return True
                entry=manifest['hours'].get(str(h),{})
                if flags[h] and entry.get('status')=='complete':return False
                if entry.get('status') not in ('unavailable','partial'):return True
                try:return now-dt.datetime.fromisoformat(entry['checked_utc'])>=dt.timedelta(minutes=a.retry_failed_minutes)
                except (ValueError,KeyError,TypeError):return True
            candidates=[h for h in hours if eligible(h)]
            if a.max_hour_jobs:candidates=candidates[:a.max_hour_jobs]
            LOG.info('%s %s %s selected %d of %d 6-hourly leads',model,a.date,a.cycle,len(candidates),len(hours))
            perf_monitor.record_event('work_selection', workflow='ai_conversion', model=model,
                eligible_hours=len(candidates), requested_hours=len(hours),
                already_complete_or_deferred=len(hours)-len(candidates))
            if a.inventory_only:
                client=core.HTTPClient(timeout=a.timeout,retries=a.retries)
                for h in candidates[:3]:
                    local_files=(source_local_paths(model,a.date,a.cycle,h,local_bases)
                                 if a.ai_source=='local' else {})
                    for part,url in source_urls(model,a.date,a.cycle,h,bases).items():
                        try:
                            if part in local_files:
                                try:
                                    rows=local_inventory(local_files[part],a.wgrib2)
                                    origin='local '+str(local_files[part])
                                except (OSError,RuntimeError,ValueError,subprocess.CalledProcessError):
                                    if not a.nomads_fallback:raise
                                    rows=core.parse_idx(client.text(url+'.idx'),client.size(url))
                                    origin='NOMADS fallback '+url
                            else:
                                rows=core.parse_idx(client.text(url+'.idx'),client.size(url))
                                origin='remote '+url
                            chosen=select_records(rows,h,model)
                            LOG.info('%s F%03d %s %s: %d inventory items, fields=%s',
                                     model,h,part,origin,len(rows),','.join(chosen))
                        except Exception as exc:
                            LOG.warning('%s F%03d %s inventory unavailable: %s',model,h,part,exc)
                continue
            workers=(a.local_workers if model in LOCAL_MODELS and a.ai_source=='local'
                     else a.remote_workers)
            LOG.info('%s uses %s source and %d workers (remote request cap %d)',
                     model, ('operational local' if model in LOCAL_MODELS and a.ai_source=='local'
                             else 'remote'), workers, a.remote_workers)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_map={pool.submit(process_hour,model,a.date,a.cycle,h,bases,
                     a.wgrib2,a.timeout,a.retries,local_bases,a.ai_source,
                     a.nomads_fallback,network_gate):h for h in candidates}
                for future in as_completed(future_map):
                    h=future_map[future]
                    try:
                        result=future.result()
                        # Never overwrite already validated chunks, even in
                        # force-hours mode; incremental catch-up remains safe.
                        new,pending=commit_hour(store,model,result,a.compression_level)
                        for field,(raw,interval) in result['records'].items():
                            if field=='apcp' and interval and h in pending:
                                core.json_write(store/'.accum_intervals'/'apcp'/f'{h}.json',list(interval))
                        if new:
                            flags=bytearray(flags);flags[h]=1
                            core.write_compressed_chunk(store/'processed'/'0',bytes(flags),a.compression_level)
                            changed.append(store)
                        state='complete' if result['parts'] and all(result['parts'].values()) and (new or flags[h]) else 'partial'
                        manifest['hours'][str(h)]={'status':state,'checked_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
                            'parts':result['parts'],'fields':sorted(new),'pending_precip':pending}
                        core.json_write(mpath,manifest)
                        if pending:LOG.info('%s F%03d awaiting preceding precipitation window %s',model,h,pending)
                    except Exception:
                        LOG.exception('%s F%03d failed',model,h)
            fixed=recover_precip(store,model,a.compression_level)
            if fixed:
                changed.append(store)
                LOG.info('%s: recovered cumulative APCP for F%s',model,','.join(map(str,fixed)))
    if a.inventory_only:return 0
    cleanup_local(out,work,protected,a.retention_days,dry=a.cleanup_dry_run)
    catalogs=scan_stores(out,protected,a.retention_days)
    if not a.no_publish and not a.cleanup_dry_run:
        shared.validate_publisher(a.publish_host,a.publish_dir)
        for store in dict.fromkeys(changed):
            model=STORE_RE.fullmatch(store.name).group(1)
            shared._publish_delta(store,catalogs[model],work,a.publish_host,a.publish_dir)
        for catalog in dict.fromkeys(catalogs.values()):publish_catalog(catalog,a.publish_host,a.publish_dir)
        if not a.no_remote_cleanup:remote_cleanup(a.publish_host,a.publish_dir,protected,a.retention_days)
    return 0


if __name__=='__main__':
    raise SystemExit(run())
