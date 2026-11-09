#!/usr/bin/env python3
"""Operational, dependency-free sounding-profile acquisition.

Selects native isobaric TMP/SPFH/UGRD/VGRD at 25 hPa intervals, deriving
Td from q and pressure where possible, with native DPT or RH fallback
when SPFH is absent. REFS Mean supplies native TMP/DPT
and U/V winds at sparse levels from the entire time-lagged ensemble.
Samples every Nth point
of the original 3-km CONUS grid, and writes separate browser-readable Zarr v2.
Map Zarr stores are never modified. No artificial vertical interpolation.
"""
from __future__ import annotations
import argparse
from array import array
import datetime as dt
import time
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import urllib.error
import shutil
import subprocess
import shlex
from concurrent.futures import ThreadPoolExecutor, as_completed
import publish_transport
import perf_monitor
from retention_policy import load_policy
import verification_convert as vc

import multi_model_to_zarr_wcoss2 as mm

PRESSURES=tuple(range(1000, 99, -25))
NATIVE={'t': 'TMP', 'q': 'SPFH', 'u': 'UGRD', 'v': 'VGRD'}
HUMIDITY_FALLBACK=(('td_native','DPT'),('rh','RH'))
# Native near-surface inputs on the identical model grid. No terrain estimates.
SURFACE_NATIVE={'sp':('PRES','surface'),'t2m':('TMP','2 m above ground'),
                'td2m':('DPT','2 m above ground'),
                'u10':('UGRD','10 m above ground'),'v10':('VGRD','10 m above ground')}
# Revision 4 upgrades completed hours via surface-only chunks, not costly
# isobaric reprocessing. Native 10-m vectors share the 2-m surface anchor.
PROFILE_REVISION=4
UNITS={'t': 'K', 'q': 'kg kg-1', 'td': 'K', 'u': 'm s-1', 'v': 'm s-1',
       'sp':'Pa','t2m':'K','td2m':'K','u10':'m s-1','v10':'m s-1'}
LONG={'t':'Native isobaric temperature', 'q':'Native isobaric specific humidity',
      'td':'Dewpoint from native SPFH, DPT, or RH at available pressure levels',
      'u':'Native isobaric U wind', 'v':'Native isobaric V wind'}
VALID_MODELS=mm.MODELS+('rap_analysis',)
# Operational REFS Mean has direct TMP/DPT/UGRD/VGRD fields at sparse levels.
# It does not publish SPFH in its mean inventory.
MEAN_NATIVE={'t':'TMP','td':'DPT','u':'UGRD','v':'VGRD'}
NAME_RE=re.compile(r'^sounding_(rrfs|refs_mean|refs_m00[1-5]|hrrr|rap_analysis|gfs_v16|gfs_v17)_(\d{8})_(\d{2})00\.zarr$')
RAP_ROOT='https://noaa-rap-pds.s3.amazonaws.com'



def read_processing_revision(stamp_path):
    """Read a sounding-hour stamp revision; null/invalid values are stale."""
    try:
        metadata=json.loads(Path(stamp_path).read_text())
        if not isinstance(metadata,dict):
            return 0
        return mm.safe_revision(metadata.get('processing_revision'),0)
    except (OSError,ValueError,TypeError,AttributeError,OverflowError):
        return 0


def dewpoint_kelvin(q, pressure_hpa):
    """Specific humidity (kg/kg) -> Td(K) using water-phase vapor pressure.

    e = qp / (epsilon + (1-epsilon)q), e and p in hPa; Magnus inversion.
    Return NaN for invalid/missing or saturated and supersaturated outliers.
    The relationship is appropriate for a preliminary skew-T, not a complete
    ice-phase or parcel thermodynamic analysis.
    """
    if not math.isfinite(q) or not 0 < q < .1 or not 0 < pressure_hpa <= 1100:
        return math.nan
    e=(q*pressure_hpa)/(.622+.378*q)
    if not 0 < e < pressure_hpa:
        return math.nan
    ratio=math.log(e/6.112)
    td_c=243.5*ratio/(17.67-ratio)
    return td_c+273.15 if -130 < td_c < 70 else math.nan



def dewpoint_from_rh_kelvin(t, rh):
    """Td from native T (K) and RH (%), when SPFH and DPT are unavailable."""
    if not (math.isfinite(t) and 150 <= t <= 360 and math.isfinite(rh) and 0 < rh <= 105):
        return math.nan
    c=t-273.15
    a=17.67*c/(c+243.5)+math.log(min(100.0,rh)/100)
    dew=243.5*a/(17.67-a)+273.15
    return dew if 140 <= dew <= t+2 else math.nan


def native_record(rows, short, pressure):
    """Exact GRIB short name AND exact pressure-level field (no partial 100/1000)."""
    pattern=re.compile(r'(?<!\d)'+str(pressure)+r'\s*(?:mb|hPa)\b',re.I)
    chosen=[]
    for r in rows:
        if r['short_name'].strip().upper()!=short:continue
        parts=r['text'].split(':')
        # Pressure must occur in the GRIB level token, not a later comment.
        if len(parts)>4 and pattern.fullmatch(parts[4].strip()):
            chosen.append(r)
    return min(chosen,key=lambda r:r['msg']) if chosen else None


def coarse_dims(step, nx=None, ny=None):
    nx=nx or mm.core.NX;ny=ny or mm.core.NY
    return (len(range(0,ny,step)),len(range(0,nx,step)))


def valid_sample(v, component):
    if not math.isfinite(v) or abs(v)>=1e9:return math.nan
    if component in ('t','td','td_native','t2m','td2m'):return v if 150 <=v<=360 else math.nan
    if component=='sp':return v if 40000<=v<=115000 else math.nan
    if component=='rh':return v if 0<=v<=105 else math.nan
    if component=='q':return v if 0<=v<.1 else math.nan
    return v if -250<=v<=250 else math.nan


def surface_record(rows, short, level):
    # Compare the level token exactly: surface pressure != MSLP or isotherm PRES.
    return min((r for r in rows if r['short_name'].upper()==short and
                len(r['text'].split(':'))>4 and r['text'].split(':')[4].strip().lower()==level),
               key=lambda r:r['msg'], default=None)


def downsample(raw, step, component, nx=None, ny=None):
    """wgrib2 IEEE order we:sn little endian; preserve Lambert first point."""
    nx=nx or mm.core.NX;ny=ny or mm.core.NY
    if len(raw)!=4*nx*ny:raise ValueError('Wrong native GRIB grid size')
    vals=array('f');vals.frombytes(raw)
    if sys.byteorder!='little':vals.byteswap()
    coarse=array('f')
    for j in range(0,ny,step):
        coarse.extend(valid_sample(vals[j*nx+i],component) for i in range(0,nx,step))
    if sys.byteorder!='little':coarse.byteswap()
    return coarse


def to_bytes(vals):
    if sys.byteorder=='little':return vals.tobytes()
    out=array('f',vals);out.byteswap();return out.tobytes()


def model_hour_list(root,model,date,cycle,requested,maximum=None):
    if model=="rap_analysis":
        if requested and any(int(x)!=0 for x in mm.core.parse_hours(requested)):
            raise ValueError("RAP analysis profiles accept F000 only")
        return [0]
    lower,upper=mm.hour_bounds(model)
    if requested:
        hrs=sorted(mm.core.parse_hours(requested))
    else:
        src=root/mm.store_name(model,date,cycle)
        proc=src/'processed'/'0'
        if not proc.exists():return []
        status=mm.read_processed(src,upper+1)
        hrs=[h for h in range(lower,upper+1) if status[h]]
    hrs=[h for h in hrs if lower<=h<=upper]
    return hrs[:maximum] if maximum else hrs


def create_store(root,model,date,cycle,stride,level,source_grid=None):
    name='sounding_{}_{}_{}00.zarr'.format(model,date,cycle)
    store=root/name
    store.mkdir(parents=True,exist_ok=True)
    native=source_grid or dict(mm.core.GRID_ATTRS,GRIB_Nx=mm.core.NX,GRIB_Ny=mm.core.NY)
    native_nx,native_ny=int(native['GRIB_Nx']),int(native['GRIB_Ny'])
    ny,nx=coarse_dims(stride,native_nx,native_ny)
    hours=1 if model=='rap_analysis' else mm.hour_bounds(model)[1]+1
    if (store/'.zattrs').is_file():
        old=json.loads((store/'.zattrs').read_text())
        if (old.get('grid_stride')!=stride or old.get('init_time')!=mm.iso_init(date,cycle).strftime('%Y-%m-%dT%H:%M:%SZ')
            or old.get('source_grid',{}).get('GRIB_Nx',mm.core.NX)!=native_nx
            or old.get('source_grid',{}).get('GRIB_Ny',mm.core.NY)!=native_ny):
            raise RuntimeError('Existing sounding store grid/cycle mismatch: '+str(store)+'; use migrate_profile_grid.py (dry-run first) before changing stride. Never combine incompatible grids in one store.')
    else:
        attrs=vc.strided_grid(native,stride)
        attrs['GRIB_Nx']=nx;attrs['GRIB_Ny']=ny
        mm.core.json_write(store/'.zgroup',{'zarr_format':2})
        mm.core.json_write(store/'.zattrs',dict(attrs,model_id=model,
            init_time=mm.iso_init(date,cycle).strftime('%Y-%m-%dT%H:%M:%SZ'),
            grid_stride=stride,grid_order='WE:SN',grid_spacing_km=float(attrs.get('GRIB_DxInMetres',3000))/1000,
            source_grid=native,
            pressure_levels_hpa=list(PRESSURES),
            dimensions={'valid_time':hours,'pressure_hpa':len(PRESSURES),'y':ny,'x':nx},
            source='Native GRIB2 isobaric profiles; 25 hPa grid only where native fields exist',
            missing_levels='NaN; no vertical interpolation',
            dewpoint_formula='Magnus water-phase inversion using native SPFH or RH; native DPT where available',
            notes='Reduced-resolution spatial sampling for point soundings; not a 3-km map field'))
    if model=='refs_mean':
        attrs=json.loads((store/'.zattrs').read_text())
        if attrs.get('ensemble_method') not in (None,'native operational time-lagged REFS Mean GRIB2'):
            raise RuntimeError('Existing REFS Mean profile store has incompatible derivation; archive the old experimental store before continuing')
        attrs.update(ensemble_method='native operational time-lagged REFS Mean GRIB2',
                     dewpoint_source='native mean DPT on available pressure levels',
                     specific_humidity_source='not published in native REFS Mean',
                     source='Native operational REFS Mean GRIB2; NO synthetic average from 5 members',
                     dewpoint_formula='not applicable: native REFS Mean DPT')
        mm.core.json_write(store/'.zattrs',attrs)
    for var in ('t','q','td','u','v'):
        mm.core.ensure_array(store,var,(hours,len(PRESSURES),ny,nx),(1,len(PRESSURES),ny,nx),'<f4','NaN',
                             {'_ARRAY_DIMENSIONS':['valid_time','pressure_hpa','y','x'],
                              'units':UNITS[var],'long_name':('Native REFS Mean dewpoint' if model=='refs_mean' and var=='td' else LONG[var]),
                              'provenance':('native REFS Mean DPT' if model=='refs_mean' else 'derived from q at native level') if var=='td' else ('not published in REFS Mean' if model=='refs_mean' and var=='q' else 'native GRIB field')},level)
    for var in SURFACE_NATIVE:
        mm.core.ensure_array(store,var,(hours,ny,nx),(1,ny,nx),'<f4','NaN',
                             {'_ARRAY_DIMENSIONS':['valid_time','y','x'],
                              'units':UNITS[var], 'provenance':('native GRIB2 10-m AGL vector on model grid' if var in ('u10','v10') else 'native GRIB2 surface/2-m field on model grid')},level)
    # Small coordinate arrays require just one chunk each.
    mm.core.ensure_array(store,'pressure_hpa',(len(PRESSURES),),(len(PRESSURES),),'<i2',None,
                         {'_ARRAY_DIMENSIONS':['pressure_hpa'],'units':'hPa'},level)
    if not (store/'pressure_hpa'/'0').exists():
        mm.core.write_compressed_chunk(store/'pressure_hpa'/'0',mm.core.pack_int16_le(PRESSURES),level)
    for name,values,units in (
        ('forecast_hour',list(range(hours)),'hours'),
        ('valid_time',[int((mm.iso_init(date,cycle)+dt.timedelta(hours=h)).timestamp()) for h in range(hours)],'seconds since 1970-01-01 UTC')):
        dtype='<i2' if name=='forecast_hour' else '<i8'
        raw=mm.core.pack_int16_le(values) if name=='forecast_hour' else mm.core.pack_int64_le(values)
        mm.core.ensure_array(store,name,(hours,),(hours,),dtype,None,
                             {'_ARRAY_DIMENSIONS':['valid_time'],'units':units},level)
        if not (store/name/'0').exists():mm.core.write_compressed_chunk(store/name/'0',raw,level)
    return store,nx,ny


@perf_monitor.timed_task('sounding_profile_hour', lambda a, k: '%s %s%s F%03d' % (a[3], a[4], a[5], a[6]))
def profile_hour(client,wgrib2,store,model,date,cycle,h,step,level,work,keep_grib=False,surface_only=False):
    """Return count of native pressure-level records; publish only complete hours."""
    upper=('rap',) if model=='rap_analysis' else ('prslevnomads',) if model.startswith('refs_m') else ('prslev',) if model=='rrfs' else ('wrfprs',) if model=='hrrr' else ('mean',)
    surface=('rap',) if model=='rap_analysis' else ('2dfldnomads',) if model.startswith('refs_m') else ('2dfld',) if model=='rrfs' else ('wrfsfc',) if model=='hrrr' else ('mean',)
    files=tuple(dict.fromkeys(surface + (() if surface_only else upper)))
    inventory=[]
    for product in files:
        if model=='rap_analysis':
            if h!=0:raise ValueError('RAP analysis only has F000')
            url=f'{RAP_ROOT}/rap.{date}/rap.t{cycle}z.awp130pgrbf00.grib2'
            idx=url+'.idx'
        else:
            url,idx=mm.file_urls(model,date,cycle,h,product)
        try:rows=mm.core.parse_idx(client.text(idx),client.size(url))
        except urllib.error.HTTPError as exc:
            if exc.code==404:continue
            raise
        inventory.append((product,url,rows))
    if not inventory:return 0
    by_product={}
    requested_fields=MEAN_NATIVE if model=='refs_mean' else NATIVE
    humidity_sources={}
    if not surface_only:
      for p in PRESSURES:
        for var,short in requested_fields.items():
            for product,url,rows in inventory:
                found=native_record(rows,short,p)
                if found is not None:
                    by_product.setdefault(product,[]).append({'output':(var,p),'record':found,'url':url})
                    if var in ('q','td'):humidity_sources[p]='specific humidity' if var=='q' else 'native dewpoint'
                    break
        if model!='refs_mean' and p not in humidity_sources:
            # Prefer native DPT, otherwise derive from RH + native T. Do not
            # synthesize moisture where no humidity observation/model field exists.
            for var,short in HUMIDITY_FALLBACK:
                picked=False
                for product,url,rows in inventory:
                    found=native_record(rows,short,p)
                    if found is not None:
                        by_product.setdefault(product,[]).append({'output':(var,p),'record':found,'url':url})
                        humidity_sources[p]='native dewpoint' if var=='td_native' else 'relative humidity'
                        picked=True
                        break
                if picked:break
    for var,(short,layer_name) in SURFACE_NATIVE.items():
        for product,url,rows in inventory:
            rec=surface_record(rows,short,layer_name)
            if rec is not None:
                by_product.setdefault(product,[]).append({'output':(var,'surface'),'record':rec,'url':url})
                break
    if not by_product:return 0
    native_nx,native_ny=mm.core.NX,mm.core.NY
    source_grid=None
    # RAP runs on a different 13-km grid; identify it from the actual GRIB
    # before creating any persistent metadata. Never relabel RAP as RRFS.
    work.mkdir(parents=True,exist_ok=True)
    if model=='rap_analysis':
        example=next(iter(by_product.values()))[0]
        grib=work/'rap.gridprobe.grib2'
        mm.core.download_partial(client,example['url'],[example],grib)
        source_grid=vc.grid_details(wgrib2,grib,1)
        grib.unlink(missing_ok=True)
        native_nx,native_ny=source_grid['GRIB_Nx'],source_grid['GRIB_Ny']
        if source_grid['GRIB_gridType']!='lambert':
            raise RuntimeError('Unsupported RAP native grid: '+str(source_grid))
        create_store(store.parent,model,date,cycle,step,level,source_grid)
    ny,nx=coarse_dims(step,native_nx,native_ny);size=nx*ny
    fields={k:array('f',[math.nan])*(len(PRESSURES)*size) for k in set(NATIVE)|{'td','td_native','rh'}}
    surface_fields={k:array('f',[math.nan])*size for k in SURFACE_NATIVE}
    for product,items in by_product.items():
        items.sort(key=lambda x:x['record']['msg'])
        grib=work/(product+'.profile.grib2');binary=work/(product+'.profile.ieee')
        mm.core.download_partial(client,items[0]['url'],items,grib)
        if model=='rap_analysis':
            grid=vc.grid_details(wgrib2,grib,1)
            if grid!=source_grid:raise RuntimeError('RAP grid differs across selected messages')
        else:mm.check_grid(wgrib2,grib)
        mm.core.decode_to_ieee(wgrib2,grib,binary)
        field_bytes=native_nx*native_ny*4
        if binary.stat().st_size!=len(items)*field_bytes:
            raise RuntimeError('Decoded file grid/record count mismatch at '+str(binary))
        with binary.open('rb') as source:
            for item in items:
                raw=source.read(field_bytes)
                var,p=item['output']
                if p=='surface':surface_fields[var]=downsample(raw,step,var,native_nx,native_ny)
                else:
                    idx=PRESSURES.index(p)
                    fields[var][idx*size:(idx+1)*size]=downsample(raw,step,var,native_nx,native_ny)
        binary.unlink(missing_ok=True)
        if not keep_grib:grib.unlink(missing_ok=True)
    dew=array('f',[math.nan])*(len(PRESSURES)*size)
    if model=='refs_mean':
        # Native ensemble DPT already includes the time-lagged REFS membership.
        # Never replace it with a five-member derived Td.
        dew=fields['td']
    else:
        for idx,p in enumerate(PRESSURES):
            lo,hi=idx*size,(idx+1)*size
            for i in range(lo,hi):
                dew[i]=dewpoint_kelvin(fields['q'][i],p)
                if not math.isfinite(dew[i]):
                    candidate=fields['td_native'][i]
                    if math.isfinite(candidate):dew[i]=candidate
                    else:dew[i]=dewpoint_from_rh_kelvin(fields['t'][i],fields['rh'][i])
        fields['td']=dew
    # Surface-only repairs preserve existing expensive pressure-level chunks.
    if not surface_only:
        for name in ('t','q','td','u','v'):
            mm.core.write_compressed_chunk(store/name/f'{h}.0.0.0',to_bytes(fields[name]),level)
    for name in SURFACE_NATIVE:
        mm.core.write_compressed_chunk(store/name/f'{h}.0.0',to_bytes(surface_fields[name]),level)
    old_stamp=store/'.hours'/f'{h:03d}.json'
    previous=json.loads(old_stamp.read_text()) if surface_only and old_stamp.is_file() else {}
    stamp={'hour':h,'processing_revision':PROFILE_REVISION,
        'surface_fields':[k for k,v in surface_fields.items() if any(math.isfinite(x) for x in v)]}
    if not surface_only:stamp.update({
        'native_records':sum(map(len,by_product.values())),
        'native_levels':{name:[p for i,p in enumerate(PRESSURES) if any(math.isfinite(v) for v in fields[name][i*size:(i+1)*size])]
                         for name in requested_fields},
        'dewpoint_source':'native DPT' if model=='refs_mean' else 'SPFH, native DPT, or RH fallback (native per level)',
        'humidity_native_sources':{str(k):v for k,v in sorted(humidity_sources.items(),reverse=True)},
        'dewpoint_levels':[p for i,p in enumerate(PRESSURES) if any(math.isfinite(v) for v in dew[i*size:(i+1)*size])]})
    mm.core.json_write(old_stamp,{**previous,**stamp})
    return sum(map(len,by_product.values()))



def refresh_catalog(root, published_only=False, published_work=None, destination=None):
    datasets=[]
    for store in sorted(root.glob('sounding_*.zarr')):
        match=NAME_RE.fullmatch(store.name)
        if not match or not (store/'.zattrs').exists():continue
        if published_only and not (published_work/'.published'/(store.name+'.json')).is_file():continue
        attrs=json.loads((store/'.zattrs').read_text())
        hours=sorted(int(p.stem) for p in (store/'.hours').glob('*.json')) if (store/'.hours').is_dir() else []
        if not hours:continue
        datasets.append({'path':store.name,'model':attrs['model_id'],'init_time':attrs['init_time'],
            'grid_stride':attrs['grid_stride'],'grid':{k:attrs[k] for k in attrs['source_grid'] if k.startswith('GRIB_')},
            'pressure_hpa':attrs['pressure_levels_hpa'], 'available_hours':hours,
            'vertical_sampling':'native isobaric levels only; absent levels are NaN',
            'variables':[{'name':n,'units':UNITS[n]} for n in (('t','td','u','v') if attrs['model_id']=='refs_mean' else ('t','td','q','u','v'))
                          + tuple(x for x in SURFACE_NATIVE if (store/x/'.zarray').is_file())],
            **({'ensemble_method':attrs.get('ensemble_method'),'dewpoint_source':attrs.get('dewpoint_source')}
                 if attrs['model_id']=='refs_mean' else {})})
    catalog=destination if destination is not None else root/'catalog_profiles.json'
    mm.core.json_write(catalog,{'schema':'omd-evu-sounding-profile-1',
        'generated_utc':dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
        'datasets':datasets})
    return catalog


def report_inventory(args,client):
    """Read only upstream index files; show exact native pressure-level coverage.

    REFS member prslevnomads products are coarser than deterministic RRFS.
    REFS Mean uses native DPT instead of specific humidity and Td conversion.
    Output lists levels with complete TMP/SPFH/UGRD/VGRD; incomplete levels
    are printed separately. Never interpolate absent levels.
    """
    hours=mm.core.parse_hours(args.hours) if args.hours else [0]
    h=sorted(hours)[0]
    if h==0 and 'refs_mean' in args.models and len(args.models)==1:h=1
    for model in args.models:
        if model=='rap_analysis':
            grib=f'{RAP_ROOT}/rap.{args.date}/rap.t{args.cycle}z.awp130pgrbf00.grib2'
            urls=[grib+'.idx']
        else:
            products=('prslevnomads',) if model.startswith('refs_m') else ('prslev',) if model=='rrfs' else ('wrfprs',) if model=='hrrr' else ('mean',)
            urls=[mm.file_urls(model,args.date,args.cycle,h,product)[1] for product in products]
        rows=[]
        for url in urls:
            try:
                for raw in client.text(url).splitlines():
                    columns=raw.split(':')
                    if len(columns)<6:continue
                    rows.append({'short_name':columns[3].strip(),'text':raw,'msg':int(columns[0])})
            except urllib.error.HTTPError as exc:
                if exc.code==404:
                    print('%s F%03d missing index: %s' % (model,h,url));continue
                raise
        native=MEAN_NATIVE if model=='refs_mean' else NATIVE
        groups={name:[p for p in PRESSURES if native_record(rows,short,p)] for name,short in native.items()}
        full=sorted(set.intersection(*(set(v) for v in groups.values())),reverse=True) if groups else []
        print('%s F%03d: all four native fields at %d of %d target levels:' % (model,h,len(full),len(PRESSURES)))
        print('  %s' % (','.join(map(str,full)) or '(none)'))
        for name in native:
            print('  %-4s %2d levels%s' % (name,len(groups[name]),
                ('; without other fields: '+','.join(map(str,sorted(set(groups[name])-set(full),reverse=True)))) if set(groups[name])-set(full) else ''))


def parse_args(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--date',required=True,help='YYYYMMDD cycle date')
    ap.add_argument('--cycle',required=True,help='UTC cycle hour 00..23 (non-synoptic hours require rap_analysis alone)')
    ap.add_argument('--models',default='rrfs,hrrr',help='Comma-separated models including refs_mean and rap_analysis; all includes REFS')
    ap.add_argument('--hours',default='',help='Comma/range selection, e.g. 0-18; default=already-processed map hours')
    ap.add_argument('--start-hour',default=None,help='RAP-only inclusive UTC YYYYMMDDHH valid hour; used with --end-hour')
    ap.add_argument('--end-hour',default=None,help='RAP-only inclusive UTC YYYYMMDDHH valid hour')
    ap.add_argument('--max-hours-per-model',type=int,default=4,help='Limit new hours per run (default 4)')
    ap.add_argument('--hour-workers',type=int,default=2,help='Concurrent independent sounding-hour decodes, any positive integer (default 2)')
    ap.add_argument('--grid-stride',type=int,default=None,help='Native cells per sounding point (default 4 => 12 km for 3-km models; 1 => native 13 km for RAP)')
    ap.add_argument('--compression-level',type=int,default=5)
    ap.add_argument('--output-root',default='zarr')
    ap.add_argument('--work-root',default='work')
    ap.add_argument('--wgrib2',default=None)
    ap.add_argument('--timeout',type=int,default=90)
    ap.add_argument('--retries',type=int,default=3)
    ap.add_argument('--keep-grib',action='store_true')
    ap.add_argument('--missing-retry-hours',type=float,default=6.0,help='Retry a native-profile absence no more than every N hours (default 6)')
    ap.add_argument('--no-publish',action='store_true',help='Skip publishing to the configured remote server')
    ap.add_argument('--retention-days',type=int,default=7)
    ap.add_argument('--protected-cycles-file',default='protected_cycles.json')
    ap.add_argument('--cleanup-only',action='store_true',help='Prune expired profile stores safely, honoring protected cycles')
    ap.add_argument('--publish-only',action='store_true',help='Publish existing profile stores/catalog without decoding GRIB2')
    ap.add_argument('--reprocess-hours',action='store_true',help='Rebuild already processed profile hours from native GRIB; use with --hours and pause cron')
    ap.add_argument('--publish-workers',type=int,default=int(os.environ.get('PUBLISH_WORKERS','3')),help='Independent SSH/SCP store publishers (default 3; any positive integer)')
    ap.add_argument('--inventory-only',action='store_true',help='Read live GRIB index inventories (no data conversion, Zarr writes, or publishing)')
    ap.add_argument('--publish-host',default='thultquist@rzdm.ncep.noaa.gov')
    ap.add_argument('--publish-dir',default='/home/people/emc/www/htdocs/users/verification_restricted/test/trh/dynamic_viewer/data')
    ap.add_argument('--verbose',action='store_true')
    a=ap.parse_args(argv)
    if not re.fullmatch(r'\d{8}',a.date):ap.error('--date must be YYYYMMDD')
    if not re.fullmatch(r'(?:[01]\d|2[0-3])',a.cycle):ap.error('--cycle must be a valid UTC hour')
    dt.datetime.strptime(a.date+a.cycle,'%Y%m%d%H')
    if not 0<=a.compression_level<=9:ap.error('compression-level must be 0 to 9')
    if a.max_hours_per_model<1:ap.error('--max-hours-per-model must be positive')
    if a.hour_workers<1:ap.error('--hour-workers must be positive')
    if a.missing_retry_hours<0:ap.error('--missing-retry-hours must not be negative')
    if not 1<=a.retention_days<=90:ap.error('--retention-days must be 1..90')
    names=list(VALID_MODELS) if a.models=='all' else a.models.split(',')
    if any(n not in VALID_MODELS for n in names):ap.error('Unknown model in --models')
    a.models=tuple(dict.fromkeys(names))
    if a.grid_stride is None:
        a.grid_stride=1 if a.models==('rap_analysis',) else 4
    if not 1<=a.grid_stride<=64:ap.error('--grid-stride must be 1 to 64')
    if 'rap_analysis' in a.models and len(a.models)>1:
        ap.error('RAP analysis uses its own hourly cycles and must be selected alone')
    if a.cycle not in ('00','06','12','18') and a.models!=('rap_analysis',):
        ap.error('Non-synoptic cycle requires --models rap_analysis')
    if bool(a.start_hour) != bool(a.end_hour):
        ap.error('--start-hour and --end-hour must be given together')
    if a.start_hour:
        if a.models!=('rap_analysis',):
            ap.error('Hourly range is supported only for --models rap_analysis')
        if a.hours:
            ap.error('--hours cannot be combined with --start-hour/--end-hour')
        try:
            first=dt.datetime.strptime(a.start_hour,'%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
            last=dt.datetime.strptime(a.end_hour,'%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
        except (TypeError,ValueError):
            ap.error('--start-hour/--end-hour must be valid YYYYMMDDHH UTC')
        if last<first or (last-first)>dt.timedelta(hours=167):
            ap.error('RAP hourly range must be ascending and at most 168 hours')
        if a.date+a.cycle!=a.start_hour:
            ap.error('--date and --cycle must match --start-hour')
    return a


def prune_profiles(root,work,policy_file,days,now=None):
    """Local cleanup uses the operational retention whitelist; fail closed."""
    policy=load_policy(policy_file)
    now=now or dt.datetime.now(dt.timezone.utc)
    cutoff=now-dt.timedelta(days=days)
    removed=[]
    for store in root.glob('sounding_*.zarr'):
        match=NAME_RE.fullmatch(store.name)
        if not match or store.is_symlink() or not store.is_dir():continue
        model,date,cycle=match.groups()
        timestamp=mm.iso_init(date,cycle)
        if timestamp>=cutoff:continue
        if model!='rap_analysis' and (f'{model}_{date}_{cycle}00.zarr' in policy.model_stores):continue
        if model=='rap_analysis':
            stamp=date+cycle
            if any(a<=stamp<=b and 'rap_pressure' in names for a,b,names in policy.analysis_rules):continue
        shutil.rmtree(store)
        stamp=work/'.published'/(store.name+'.json')
        stamp.unlink(missing_ok=True)
        removed.append(store.name)
    return removed


def publish_profile_cleanup(root,work,host,remote_dir,removed):
    """Install a reconciled catalog before deleting only verified old profiles.

    A pending file makes interrupted remote cleanup retryable. No private
    analysis or model data are ever touched.
    """
    if not shutil.which('ssh') or not shutil.which('scp'):
        raise RuntimeError('Profile publication requires ssh and scp')
    if not re.fullmatch(r'[\w.-]+@[\w.-]+',host) or not re.fullmatch(r'/[A-Za-z0-9_./-]+',remote_dir):
        raise ValueError('Unsafe publication destination')
    if not remote_dir.rstrip('/').endswith('/data'):
        raise ValueError('Refusing remote profile cleanup outside viewer data directory')
    pending_file=work/'.profile_cleanup_pending.json'
    older=[]
    if pending_file.is_file():
        older=json.loads(pending_file.read_text())
    names=sorted(set(older)|set(removed))
    if any(not NAME_RE.fullmatch(name) for name in names):
        raise ValueError('Refusing invalid remote profile deletion name')
    if names:mm.core.json_write(pending_file,names)
    catalog=refresh_catalog(root,published_only=True,published_work=work,
                            destination=work/'catalog_profiles.json')
    import profile_catalog_publish
    profile_catalog_publish.publish(catalog,host,remote_dir,names)
    pending_file.unlink(missing_ok=True)


def web_permissions(store):
    """Make newly published profile assets world-readable; never touch secrets."""
    for folder,dirs,files in os.walk(store):
        os.chmod(folder,0o755)
        for f in files:
            os.chmod(os.path.join(folder,f),0o644)


@perf_monitor.monitored('sounding_profiles')
def main(argv=None):
    a=parse_args(argv)
    perf_monitor.record_event('run_config', workflow='sounding_profiles',
        models=list(a.models), hour_workers=a.hour_workers, grid_stride=a.grid_stride,
        max_hours_per_model=a.max_hours_per_model)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s')
    root=Path(a.output_root).resolve();root.mkdir(parents=True,exist_ok=True)
    work=Path(a.work_root).resolve();work.mkdir(parents=True,exist_ok=True)
    wgrib2=None if a.publish_only or a.cleanup_only or a.inventory_only else mm.core.find_wgrib2(a.wgrib2)
    client=None if a.publish_only or a.cleanup_only else mm.core.HTTPClient(timeout=a.timeout,retries=a.retries)
    if a.inventory_only:
        report_inventory(a,client)
        return 0
    done=0;errors=0;updated=[]
    if a.cleanup_only:
        removed=prune_profiles(root,work,Path(a.protected_cycles_file),a.retention_days)
        logging.info('Pruned %d unprotected expired sounding profile stores',len(removed))
        refresh_catalog(root)
        if not a.no_publish:
            with publish_transport.session():publish_profile_cleanup(root,work,a.publish_host,a.publish_dir,removed)
        return 0
    profile_targets = [(model,a.date,a.cycle) for model in a.models]
    if a.start_hour:
        first=dt.datetime.strptime(a.start_hour,'%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
        last=dt.datetime.strptime(a.end_hour,'%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
        profile_targets=[]
        current=first
        while current<=last:
            profile_targets.append(('rap_analysis',current.strftime('%Y%m%d'),current.strftime('%H')))
            current+=dt.timedelta(hours=1)
    for model,date,cycle in (() if a.publish_only else profile_targets):
        hours=model_hour_list(root,model,date,cycle,a.hours)
        if not hours:
            logging.warning('%s: no candidate hours (model not yet converted?)',model)
            continue
        store=root/f'sounding_{model}_{date}_{cycle}00.zarr'
        if model!='rap_analysis':
            store,nx,ny=create_store(root,model,date,cycle,a.grid_stride,a.compression_level)
        else:
            nx=ny=0  # Discover RAP's native projection from a GRIB message.
        planned=[]
        for h in hours:
            if len(planned)>=a.max_hours_per_model:
                break
            stamp=store/'.hours'/f'{h:03d}.json'
            if stamp.exists() and not a.reprocess_hours:
                complete_revision=read_processing_revision(stamp)
                if complete_revision>=PROFILE_REVISION and all((store/v/f'{h}.0.0').exists() for v in SURFACE_NATIVE):continue
            hour_work=work/'sounding_profiles'/model/f'{date}_{cycle}'/f'f{h:03d}'
            missing_marker=hour_work/'.missing_native.json'
            if not a.hours and missing_marker.exists() and a.missing_retry_hours:
                if dt.datetime.now(dt.timezone.utc).timestamp()-missing_marker.stat().st_mtime < a.missing_retry_hours*3600:
                    continue
            surface_only=(stamp.is_file() and not a.reprocess_hours and
                          (store/'t'/f'{h}.0.0.0').is_file())
            planned.append((h,hour_work,missing_marker,surface_only))

        def convert_one(task):
            # Different hours have distinct temporary paths, Zarr chunk paths,
            # completion stamps and source downloads. Store metadata and the
            # shared catalog are created only outside the worker pool.
            h,hour_work,missing_marker,surface_only=task
            started=time.monotonic()
            logging.info('%s %s F%03d: sounding START',model,date+cycle,h)
            try:
                n=profile_hour(client,wgrib2,store,model,date,cycle,h,a.grid_stride,
                               a.compression_level,hour_work,a.keep_grib,**({'surface_only': True} if surface_only else {}))
                if not n:
                    mm.core.json_write(missing_marker,{
                        'last_attempt_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
                        'reason':'no native pressure-level inputs found'})
                    logging.info('%s %s F%03d: no native profile records (%.1fs)',
                                 model,date+cycle,h,time.monotonic()-started)
                    return (h,0,None)
                missing_marker.unlink(missing_ok=True)
                logging.info('%s %s F%03d: %d native records (%.1fs)',
                             model,date+cycle,h,n,time.monotonic()-started)
                return (h,n,None)
            except Exception as exc:
                logging.exception('%s %s F%03d: sounding failed (%.1fs): %s',
                                  model,date+cycle,h,time.monotonic()-started,exc)
                return (h,0,exc)

        logging.info('%s %s: sounding hours queued=%d, concurrent hour workers=%d',
                     model,date+cycle,len(planned),a.hour_workers)
        # Do not create a pool for a single task; keep low-memory serial fallback.
        if a.hour_workers==1 or len(planned)<=1:
            results=map(convert_one,planned)
            for h,n,err in results:
                if err is not None:errors+=1
                elif n:
                    done+=1
                    updated.append(store)
        else:
            with ThreadPoolExecutor(max_workers=a.hour_workers) as pool:
                jobs={pool.submit(convert_one,task):task[0] for task in planned}
                for future in as_completed(jobs):
                    h,n,err=future.result()
                    if err is not None:errors+=1
                    elif n:
                        done+=1
                        updated.append(store)
    catalog=refresh_catalog(root)
    if not a.no_publish:
        if a.publish_only:
            updated=[root/f'sounding_{model}_{date}_{cycle}00.zarr' for model,date,cycle in profile_targets]
            updated=[store for store in updated if (store/'.hours').is_dir()]
        else:
            # Also retry stores whose prior publication failed, and bootstrap
            # locally complete profiles that have never been published. This
            # avoids declaring a recovered case complete while its catalog is stale.
            for model,date,cycle in profile_targets:
                store=root/f'sounding_{model}_{date}_{cycle}00.zarr'
                if not (store/'.hours').is_dir() or not any((store/'.hours').glob('*.json')):
                    continue
                state=work/'.published'/(store.name+'.json')
                if not state.is_file():
                    updated.append(store)
                else:
                    try:
                        old=json.loads(state.read_text())
                        if old.get('schema')!=1 or old.get('store')!=store.name or \
                           old.get('files')!=mm._published_files_snapshot(store):
                            updated.append(store)
                    except (OSError,ValueError,TypeError):
                        updated.append(store)
        transfers=list(dict.fromkeys(updated))
        transferred=[]
        def send_store(store):
            # Store-local publication state and staging files remain independent;
            # the shared remote profile catalog is published exactly once, last.
            web_permissions(store)
            mm.publish_one(store,None,work,a.publish_host,a.publish_dir,publish_mode='auto')
            return store
        with publish_transport.session():
            if len(transfers)>1 and a.publish_workers>1:
                with ThreadPoolExecutor(max_workers=a.publish_workers) as pool:
                    tasks={pool.submit(send_store,store):store for store in transfers}
                    for future in as_completed(tasks):
                        try:transferred.append(future.result())
                        except Exception as exc:
                            logging.error('Could not publish %s: %s',tasks[future],exc,exc_info=True);errors+=1
            else:
                for store in transfers:
                    try:transferred.append(send_store(store))
                    except Exception as exc:
                        logging.error('Could not publish %s: %s',store,exc,exc_info=True);errors+=1
            if transferred:
                try:
                    # Never advertise a local-only store or a failed transfer.
                    remote_catalog=refresh_catalog(root,published_only=True,published_work=work,
                                                   destination=work/'catalog_profiles.json')
                    mm.publish_one(transferred[-1],remote_catalog,work,a.publish_host,a.publish_dir,publish_mode='auto')
                except Exception as exc:
                    logging.error('Could not publish profile catalog: %s',exc,exc_info=True);errors+=1
    logging.info('Sounding profile summary: converted hours=%d failed=%d publish=%s',done,errors,not a.no_publish)
    return 1 if errors else 0

if __name__=='__main__':sys.exit(main())
