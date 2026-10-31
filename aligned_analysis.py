#!/usr/bin/env python3
"""Regrid operational one-hour analysis Zarr to the common RRFS/HRRR/REFS Lambert grid.

Stdlib-only fallback; NumPy accelerates both projection and interpolation when
already present in the WCOSS Python runtime (no pip/conda installation).
Never modifies the native analysis, model, or legacy catalogs.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from array import array
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
import tarfile
import tempfile
import zlib

try:
    import numpy as np
except ImportError:
    np = None

from multi_model_to_zarr_wcoss2 import core, validate_publisher
from retention_policy import load_policy, protects_analysis

LOG = logging.getLogger('aligned-analysis')
PRODUCTS = ('mrms_refc','mrms_prate','mrms_apcp','urma','rtma','rap_surface','rap_pressure','nohrsc_6h','nohrsc_24h','nohrsc_48h','nohrsc_72h')
PATTERN = re.compile(r'^analysis_(mrms_refc|mrms_prate|mrms_apcp|urma|rtma|rap_surface|rap_pressure|nohrsc_6h|nohrsc_24h|nohrsc_48h|nohrsc_72h)_(\d{10})\.zarr$')
TARGET = dict(core.GRID_ATTRS)
NX, NY = int(TARGET['GRIB_Nx']), int(TARGET['GRIB_Ny'])
GRID_NAMES = ('GRIB_gridType','GRIB_Nx','GRIB_Ny','GRIB_DxInMetres','GRIB_DyInMetres','GRIB_LoVInDegrees','GRIB_LaDInDegrees','GRIB_Latin1InDegrees','GRIB_Latin2InDegrees','GRIB_longitudeOfFirstGridPointInDegrees','GRIB_latitudeOfFirstGridPointInDegrees','longitude_min','longitude_max','latitude_min','latitude_max')


def readjson(path):
    return json.loads(path.read_text(encoding='utf-8'))


def atomic_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.'+path.name+'.tmp')
    tmp.write_text(json.dumps(obj,indent=2,sort_keys=True,allow_nan=False)+'\n',encoding='utf-8')
    os.replace(tmp,path)


def lambert(g):
    rad=math.pi/180
    phi1=float(g['GRIB_Latin1InDegrees'])*rad
    phi2=float(g['GRIB_Latin2InDegrees'])*rad
    phi0=float(g['GRIB_LaDInDegrees'])*rad
    if abs(phi1-phi2)<1e-10:n=math.sin(phi1)
    else:n=math.log(math.cos(phi1)/math.cos(phi2))/math.log(math.tan(math.pi/4+phi2/2)/math.tan(math.pi/4+phi1/2))
    f=math.cos(phi1)*math.tan(math.pi/4+phi1/2)**n/n
    radius=6371229.0
    rho0=radius*f/math.tan(math.pi/4+phi0/2)**n
    lon0=float(g['GRIB_LoVInDegrees'])*rad
    def xy(lon,lat):
        lat=max(-89.999999,min(89.999999,lat))
        rho=radius*f/math.tan(math.pi/4+lat*rad/2)**n
        return rho*math.sin(n*(lon*rad-lon0)),rho0-rho*math.cos(n*(lon*rad-lon0))
    def ll(x,y):
        xx=x; yy=rho0-y; rho=math.copysign(math.hypot(xx,yy),n)
        lat=(2*math.atan((radius*f/rho)**(1/n))-math.pi/2)/rad
        lon=(lon0+math.atan2(xx,yy)/n)/rad
        return lon,lat
    x0,y0=xy(float(g['GRIB_longitudeOfFirstGridPointInDegrees']),float(g['GRIB_latitudeOfFirstGridPointInDegrees']))
    return xy,ll,x0,y0


def index_map_numpy(source):
    """Return mapping of every target gridpoint into source grid, in source indices."""
    n=NX*NY
    targetxy,_,tx0,ty0=lambert(TARGET)
    sgrid=source['GRIB_gridType']
    if sgrid=='lambert':
        sxy,_,sx0,sy0=lambert(source)
    elif sgrid!='regular_ll':raise ValueError('Unsupported analysis projection '+repr(sgrid))
    # Target Lambert inverse in vectorized NumPy; avoids installing pyproj.
    g=TARGET; rad=np.pi/180
    p1=float(g['GRIB_Latin1InDegrees'])*rad;p2=float(g['GRIB_Latin2InDegrees'])*rad
    nfac=np.sin(p1) if abs(p1-p2)<1e-10 else np.log(np.cos(p1)/np.cos(p2))/np.log(np.tan(np.pi/4+p2/2)/np.tan(np.pi/4+p1/2))
    ff=np.cos(p1)*np.tan(np.pi/4+p1/2)**nfac/nfac
    rho0=6371229.0*ff/np.tan(np.pi/4+float(g['GRIB_LaDInDegrees'])*rad/2)**nfac
    x=(np.arange(NX,dtype=np.float64)*float(g['GRIB_DxInMetres'])+tx0)[None,:]
    y=(np.arange(NY,dtype=np.float64)*float(g['GRIB_DyInMetres'])+ty0)[:,None]
    rho=np.copysign(np.hypot(x,rho0-y),nfac)
    lat=(2*np.arctan((6371229.0*ff/rho)**(1/nfac))-np.pi/2)/rad
    lon=(float(g['GRIB_LoVInDegrees'])*rad+np.arctan2(x,rho0-y)/nfac)/rad
    if sgrid=='regular_ll':
        lo=float(source['longitude_min']);hi=float(source['longitude_max'])
        start=lo+((lon-lo+180)%360-180)
        if hi>180:start=np.where(start<lo,start+360,start)
        sx=(start-lo)*(float(source['GRIB_Nx'])-1)/(hi-lo)
        sy=(lat-float(source['latitude_min']))*(float(source['GRIB_Ny'])-1)/(float(source['latitude_max'])-float(source['latitude_min']))
    else:
        sg=source
        r=np.pi/180;p1=float(sg['GRIB_Latin1InDegrees'])*r;p2=float(sg['GRIB_Latin2InDegrees'])*r
        ns=np.sin(p1) if abs(p1-p2)<1e-10 else np.log(np.cos(p1)/np.cos(p2))/np.log(np.tan(np.pi/4+p2/2)/np.tan(np.pi/4+p1/2))
        fs=np.cos(p1)*np.tan(np.pi/4+p1/2)**ns/ns
        rhos=6371229.0*fs/np.tan(np.pi/4+lat*r/2)**ns
        delta=(lon-float(sg['GRIB_LoVInDegrees']))*r
        sx=(rhos*np.sin(ns*delta)-sx0)/float(sg['GRIB_DxInMetres'])
        sy=(6371229.0*fs/np.tan(np.pi/4+float(sg['GRIB_LaDInDegrees'])*r/2)**ns-rhos*np.cos(ns*delta)-sy0)/float(sg['GRIB_DyInMetres'])
    # Bilinear cells require 4 valid corner values; clip only after recording mask.
    valid=np.isfinite(sx)&np.isfinite(sy)&(sx>=0)&(sy>=0)&(sx<source['GRIB_Nx']-1)&(sy<source['GRIB_Ny']-1)
    ix=np.floor(np.where(valid,sx,0)).astype(np.int32);iy=np.floor(np.where(valid,sy,0)).astype(np.int32)
    fracx=np.where(valid,sx-ix,0).astype(np.float32)
    fracy=np.where(valid,sy-iy,0).astype(np.float32)
    return (iy*int(source['GRIB_Nx'])+ix).ravel(),fracx.ravel(),fracy.ravel(),valid.ravel()


def index_map_python(source):
    """No third-party Python dependencies required, slower one-time mapping."""
    _,ll,x0,y0=lambert(TARGET)
    sx0=sy0=0
    if source['GRIB_gridType']=='lambert':sxy,_,sx0,sy0=lambert(source)
    elif source['GRIB_gridType']!='regular_ll':raise ValueError('Unknown source projection')
    indices=array('i'); fx=array('f');fy=array('f');mask=bytearray()
    for j in range(NY):
        for i in range(NX):
            lon,lat=ll(x0+i*TARGET['GRIB_DxInMetres'],y0+j*TARGET['GRIB_DyInMetres'])
            if source['GRIB_gridType']=='regular_ll':
                lo=float(source['longitude_min']);hi=float(source['longitude_max']);llon=lo+((lon-lo+180)%360-180)
                if hi>180 and llon<lo:llon+=360
                x=(llon-lo)*(source['GRIB_Nx']-1)/(hi-lo)
                y=(lat-source['latitude_min'])*(source['GRIB_Ny']-1)/(source['latitude_max']-source['latitude_min'])
            else:
                xx,yy=sxy(lon,lat);x=(xx-sx0)/source['GRIB_DxInMetres'];y=(yy-sy0)/source['GRIB_DyInMetres']
            good=math.isfinite(x) and math.isfinite(y) and 0<=x<source['GRIB_Nx']-1 and 0<=y<source['GRIB_Ny']-1
            xi=int(x) if good else 0;yi=int(y) if good else 0
            indices.append(yi*source['GRIB_Nx']+xi);fx.append(x-xi if good else 0.);fy.append(y-yi if good else 0.);mask.append(int(good))
    return indices,fx,fy,mask


def remap(raw,meta,mapping,field):
    src_nx=meta['GRIB_Nx'];src_ny=meta['GRIB_Ny'];expected=src_nx*src_ny*4
    if len(raw)!=expected:raise ValueError('Source chunk byte length does not match its grid')
    if np is not None:
        a=np.frombuffer(raw,dtype='<f4'); idx,fx,fy,mask=(np.asarray(x) for x in mapping)
        # Memory-bounded vectorization by rows; prevents giant intermediates.
        out=np.empty(NX*NY,dtype='<f4')
        for start in range(0,len(out),NX*24):
            sl=slice(start,min(len(out),start+NX*24))
            ii=idx[sl];x=fx[sl];y=fy[sl];ok=mask[sl]
            if field=='refc':
                nearest=ii+(x>=.5).astype(np.int32)+(y>=.5).astype(np.int32)*src_nx
                vals=a[nearest];ok=ok & np.isfinite(vals)&(vals>-90)&(np.abs(vals)<1e15)
                out[sl]=np.where(ok,vals,np.nan)
            else:
                a0=a[ii];a1=a[ii+1];a2=a[ii+src_nx];a3=a[ii+src_nx+1]
                ok=ok & np.isfinite(a0)&np.isfinite(a1)&np.isfinite(a2)&np.isfinite(a3)&(np.maximum.reduce([np.abs(a0),np.abs(a1),np.abs(a2),np.abs(a3)])<1e15)
                out[sl]=np.where(ok, a0*(1-x)*(1-y)+a1*x*(1-y)+a2*(1-x)*y+a3*x*y, np.nan)
        return out.tobytes()
    a=array('f');a.frombytes(raw)
    if sys.byteorder!='little':a.byteswap()
    idx,fx,fy,mask=mapping
    out=array('f')
    for k,(i,x,y,ok) in enumerate(zip(idx,fx,fy,mask)):
        if not ok:out.append(float('nan'));continue
        if field=='refc':
            v=a[i+int(x>=.5)+int(y>=.5)*src_nx]
            out.append(v if math.isfinite(v) and v>-90 and abs(v)<1e15 else float('nan'))
        else:
            vals=(a[i],a[i+1],a[i+src_nx],a[i+src_nx+1])
            if not all(math.isfinite(v) and abs(v)<1e15 for v in vals):out.append(float('nan'));continue
            out.append(vals[0]*(1-x)*(1-y)+vals[1]*x*(1-y)+vals[2]*(1-x)*y+vals[3]*x*y)
    if sys.byteorder!='little':out.byteswap()
    return out.tobytes()


def process(store, output, mapping_cache):
    attrs=readjson(store/'.zattrs');source=attrs.get('grid')
    if not source or source.get('GRIB_gridType') not in ('lambert','regular_ll'):raise ValueError('Missing/unknown source analysis grid')
    source=dict(source);source['GRIB_Nx']=int(source['GRIB_Nx']);source['GRIB_Ny']=int(source['GRIB_Ny'])
    stamp=PATTERN.fullmatch(store.name).group(2)
    fields=sorted(x for x in store.iterdir() if x.is_dir() and (x/'.zarray').exists() and (x/'0.0.0').exists())
    if not fields:return None
    output.mkdir(parents=True,exist_ok=True)
    if not (output/'.zgroup').exists():atomic_json(output/'.zgroup',{'zarr_format':2})
    result_attrs={**attrs,'grid':TARGET,'dimensions':{'valid_time':1,'x':NX,'y':NY},'aligned_to':'RRFS/HRRR/REFS 3-km Lambert','source_grid':source}
    atomic_json(output/'.zattrs',result_attrs)
    sig=json.dumps({k:source.get(k) for k in GRID_NAMES},sort_keys=True)
    if sig not in mapping_cache:
        LOG.info('Generating projection mapping for %s (%s)',store.name,'NumPy' if np is not None else 'stdlib')
        mapping_cache[sig]=index_map_numpy(source) if np is not None else index_map_python(source)
    mapping=mapping_cache[sig]
    changed=False
    for fd in fields:
        src=fd/'0.0.0';dest=output/fd.name/'0.0.0'
        # Atomically regenerate if source was refreshed (preliminary RTMA replacement).
        if dest.is_file() and dest.stat().st_mtime_ns>=src.stat().st_mtime_ns:continue
        arr=remap(zlib.decompress(src.read_bytes()),source,mapping,fd.name)
        dest.parent.mkdir(parents=True,exist_ok=True)
        atomic_json(dest.parent/'.zarray',{'zarr_format':2,'shape':[1,NY,NX],'chunks':[1,NY,NX],'dtype':'<f4','compressor':{'id':'zlib','level':3},'fill_value':'NaN','order':'C','filters':None,'dimension_separator':'.'})
        atomic_json(dest.parent/'.zattrs',readjson(fd/'.zattrs'))
        tmp=dest.with_name('0.0.0.tmp')
        tmp.write_bytes(zlib.compress(arr,3));os.replace(tmp,dest)
        # Copy source mtime to avoid repeated conversions across cron runs.
        os.utime(dest,ns=(src.stat().st_mtime_ns,src.stat().st_mtime_ns))
        changed=True
    atomic_json(output/'.aligned.json',{'source':store.name,'source_time':attrs.get('source_time'),'fields':[fd.name for fd in fields]})
    return changed


# Each worker owns a private projection-map cache. The shared main process
# remains solely responsible for catalog construction, retention and RZDM
# publishing; workers only touch distinct valid-hour Zarr output directories.
_ALIGN_WORKER_MAPPING_CACHE = {}


def _align_store_worker(store, dest):
    return process(store, dest, _ALIGN_WORKER_MAPPING_CACHE)


def align_stores(source, root, max_new_hours, workers):
    """Bounded parallel alignment; count only changed stores toward the budget.

    Small batches prevent a large case-study backfill from scheduling thousands
    of stores ahead of the configured cap. Stores already up to date are
    inspected without consuming the changed-store budget.
    """
    changed = []
    completed = 0
    if workers == 1:
        cache = {}
        for _, store in source:
            dest = root / 'aligned' / store.name
            if process(store, dest, cache):
                changed.append(dest)
                completed += 1
            if completed >= max_new_hours:
                break
        return changed
    with ProcessPoolExecutor(max_workers=workers) as pool:
        offset = 0
        while offset < len(source) and completed < max_new_hours:
            batch_size = min(workers, max_new_hours-completed, len(source)-offset)
            batch = source[offset:offset+batch_size]
            offset += len(batch)
            futures = {
                pool.submit(_align_store_worker, store, root/'aligned'/store.name):store
                for _, store in batch
            }
            for future in as_completed(futures):
                store = futures[future]
                try:
                    updated = future.result()
                except Exception:
                    LOG.exception('Failed aligned analysis conversion %s', store)
                    raise
                if updated:
                    changed.append(root/'aligned'/store.name)
                    completed += 1
    return changed


def catalog(root,retention,protected):
    cutoff=dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=retention)
    entries=[]
    for store in sorted((root/'aligned').glob('analysis_*.zarr')):
        m=PATTERN.fullmatch(store.name)
        if not m or store.is_symlink():continue
        t=dt.datetime.strptime(m.group(2),'%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
        if t<cutoff and not protects_analysis(store.name,protected):continue
        try:
            a=readjson(store/'.zattrs')
            variables=[]
            for d in store.iterdir():
                if not d.is_dir() or not (d/'0.0.0').is_file():continue
                md=readjson(d/'.zarray')
                variables.append({'name':d.name,'shape':md['shape'],'chunks':md['chunks'],
                                  'dtype':md['dtype'],'compressor':md['compressor'],
                                  'attrs':readjson(d/'.zattrs'),'available_hours':[0]})
            if variables:entries.append({'id':store.stem,'path':'aligned/'+store.name,
                   'init_time':a['init_time'],'model':'ANALYSIS','product':a['source_product'],
                   'source_time':a.get('source_time'),'grid':a['grid'],'variables':variables})
        except (OSError,ValueError,KeyError) as exc:LOG.warning('Skipping %s: %s',store,exc)
    entries.sort(key=lambda x:x['init_time'],reverse=True)
    output=root/'catalog_aligned_analysis.json'
    atomic_json(output,{'schema':'rrfs-zarr-viewer-1','generated_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'datasets':entries})
    return output


def publish_stores(stores,root,work,host,remote_dir):
    # Reuse the operational transactional tar/ssh transport, preserving
    # original native analysis and all other RZDM catalogs.
    from multi_model_to_zarr_wcoss2 import publish_one
    for store in stores:
        publish_one(store,None,work,host,remote_dir,store_arcname='aligned/'+store.name)
    # Publish the index only after all its referenced stores were transferred.
    index=root/'catalog_aligned_analysis.json'
    incoming=remote_dir+'/.catalog_aligned_analysis.json.incoming'
    subprocess.run(['scp',str(index),f'{host}:{incoming}'],check=True)
    cmd='set -eu; test -f '+__import__('shlex').quote(incoming)+'; mv '+__import__('shlex').quote(incoming)+' '+__import__('shlex').quote(remote_dir+'/catalog_aligned_analysis.json')
    subprocess.run(['ssh',host,cmd],check=True)


def run(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-root',type=Path,required=True);p.add_argument('--work-root',type=Path,required=True)
    p.add_argument('--retention-days',type=int,default=7);p.add_argument('--protected-cycles-file',type=Path,required=True)
    p.add_argument('--publish-host',default=core.DEFAULT_PUBLISH_HOST);p.add_argument('--publish-dir',default=core.DEFAULT_PUBLISH_DIR)
    p.add_argument('--no-publish',action='store_true');p.add_argument('--max-new-hours',type=int,default=24)
    p.add_argument('--workers',type=int,default=1,help='Concurrent independent aligned stores, 1-4 (default 1)')
    p.add_argument('--catalog-only',action='store_true');p.add_argument('--verbose',action='store_true')
    p.add_argument('--repair-web-permissions',action='store_true',help='One-time RZDM chmod repair of aligned analysis files')
    p.add_argument('--products',default='all',help='Comma-separated source products (e.g. rtma,urma), or all')
    p.add_argument('--since',help='Inclusive UTC valid hour YYYYMMDDHH (targeted backfill)')
    p.add_argument('--until',help='Inclusive UTC valid hour YYYYMMDDHH (targeted backfill)')
    a=p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    if a.retention_days<1 or a.retention_days>366:p.error('Invalid retention days')
    if a.max_new_hours<1:p.error('max-new-hours must be >=1')
    if not 1<=a.workers<=4:p.error('--workers must be 1-4')
    selected=set(PRODUCTS) if a.products=='all' else set(a.products.split(','))
    if not selected or selected.difference(PRODUCTS):p.error('Invalid --products; use: '+','.join(PRODUCTS))
    try:
        start=dt.datetime.strptime(a.since,'%Y%m%d%H').replace(tzinfo=dt.timezone.utc) if a.since else None
        end=dt.datetime.strptime(a.until,'%Y%m%d%H').replace(tzinfo=dt.timezone.utc) if a.until else None
    except ValueError:p.error('--since/--until must be YYYYMMDDHH in UTC')
    if start and end and start>end:p.error('--since is after --until')
    policy=load_policy(a.protected_cycles_file)
    root=a.output_root.expanduser().resolve();work=a.work_root.expanduser().resolve();root.mkdir(parents=True,exist_ok=True)
    cutoff=dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=a.retention_days)
    source=[]
    for store in (root/'analysis').glob('analysis_*.zarr'):
        m=PATTERN.fullmatch(store.name)
        if not m or store.is_symlink():continue
        t=dt.datetime.strptime(m.group(2),'%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
        if m.group(1) not in selected or (start and t<start) or (end and t>end):continue
        if t>=cutoff or protects_analysis(store.name,policy.analysis_rules):source.append((t,store))
    # Most recent valid times take priority, old analyses fill in as capacity allows.
    source.sort(reverse=True)
    changed=[] if a.catalog_only else align_stores(source,root,a.max_new_hours,a.workers)
    # Expire independent derived stores; do NOT touch the original analysis.
    for store in (root/'aligned').glob('analysis_*.zarr'):
        m=PATTERN.fullmatch(store.name)
        if not m or store.is_symlink():continue
        t=dt.datetime.strptime(m.group(2),'%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
        if t<cutoff and not protects_analysis(store.name,policy.analysis_rules):shutil.rmtree(store)
    index=catalog(root,a.retention_days,policy.analysis_rules)
    LOG.info('Aligned analysis: %s changed stores; %s catalog datasets',len(changed),len(readjson(index)['datasets']))
    if not a.no_publish:
        validate_publisher(a.publish_host,a.publish_dir)
        # A failed SSH/TAR/catalog upload must be retried even if the derived
        # local chunk files are already current on the next hourly cron.
        state_file=root/'.aligned_publish_state.json'
        try:published=readjson(state_file) if state_file.is_file() else {}
        except (ValueError,OSError):published={}
        if not isinstance(published,dict):published={}
        available={}
        need=[]
        for e in readjson(index)['datasets']:
            name=Path(e['path']).name
            store=root/'aligned'/name
            fingerprint=sorted((d.name,(d/'0.0.0').stat().st_size,(d/'0.0.0').stat().st_mtime_ns)
                                for d in store.iterdir() if d.is_dir() and (d/'0.0.0').is_file())
            revision=__import__('hashlib').sha256(json.dumps(fingerprint).encode('utf-8')).hexdigest()
            available[name]=revision
            if published.get(name)!=revision:need.append(store)
        LOG.info('Aligned analysis to publish: %d new or changed stores',len(need))
        publish_stores(need,root,work,a.publish_host,a.publish_dir)
        atomic_json(state_file,available)
        # Existing v6.8/v0.8.1 aligned stores could have been transferred with
        # restrictive umask-derived permissions. Fix them in place, not just
        # newly transferred stores. RZDM runs Python 3.6.8.
        remote_permissions = r'''import os,pathlib,re,sys
root=pathlib.Path(sys.argv[1]); base=root/'aligned'
if base.is_symlink():raise SystemExit('Unsafe aligned path')
if not base.exists():raise SystemExit(0)
base.chmod(0o755)
pattern=re.compile(r'^analysis_(mrms_refc|mrms_prate|mrms_apcp|urma|rtma|rap_surface|rap_pressure|nohrsc_6h|nohrsc_24h|nohrsc_48h|nohrsc_72h)_[0-9]{10}\.zarr$')
fixed=0
for store in base.iterdir():
 if not pattern.fullmatch(store.name) or store.is_symlink() or not store.is_dir():continue
 for current,dirs,files in os.walk(str(store),followlinks=False):
  node=pathlib.Path(current)
  if node.is_symlink():continue
  node.chmod(0o755)
  dirs[:]=[d for d in dirs if not (node/d).is_symlink()]
  for d in dirs:(node/d).chmod(0o755)
  for f in files:
   leaf=node/f
   if leaf.is_file() and not leaf.is_symlink():leaf.chmod(0o644);fixed+=1
print('Aligned analysis web-access repair: %d files' % fixed)
'''
        if a.repair_web_permissions:
            import shlex
            permission_cmd='python3 - '+shlex.quote(a.publish_dir)
            subprocess.run(['ssh',a.publish_host,permission_cmd],input=remote_permissions,text=True,check=True)
        # Maintain remote derived-only retention with exact regex and whitelist.
        import shlex
        remote_py='''import datetime as d,json,pathlib,re,shutil,sys
root=pathlib.Path(sys.argv[1]);cut=d.datetime.strptime(sys.argv[2],'%Y%m%d%H%M%S').replace(tzinfo=d.timezone.utc);pins=set(json.loads(sys.argv[3]))
for x in (root/'aligned').glob('analysis_*.zarr'):
 m=re.fullmatch(r'analysis_(mrms_refc|mrms_prate|mrms_apcp|urma|rtma|rap_surface|rap_pressure|nohrsc_6h|nohrsc_24h|nohrsc_48h|nohrsc_72h)_(\\d{10})\\.zarr',x.name)
 if not m or x.is_symlink() or not x.is_dir():continue
 t=d.datetime.strptime(m.group(2),'%Y%m%d%H').replace(tzinfo=d.timezone.utc)
 if t<cut and x.name not in pins:shutil.rmtree(x)
'''
        pins=[s.name for s in (root/'aligned').glob('analysis_*.zarr')
              if s.is_dir() and protects_analysis(s.name,policy.analysis_rules)]
        cmd='python3 - '+shlex.quote(a.publish_dir)+' '+shlex.quote(cutoff.strftime('%Y%m%d%H%M%S'))+' '+shlex.quote(json.dumps(pins))
        subprocess.run(['ssh',a.publish_host,cmd],input=remote_py,text=True,check=True)


if __name__=='__main__':run()
