#!/usr/bin/env python3
"""Operational, dependency-free WCOSS2 sounding-profile acquisition.

Selects native isobaric TMP/SPFH/UGRD/VGRD at 25 hPa intervals, deriving
Td from q and pressure except for REFS Mean, which supplies native TMP/DPT
and U/V winds at sparse levels from the entire time-lagged ensemble.
Samples every Nth point
of the original 3-km CONUS grid, and writes separate browser-readable Zarr v2.
Map Zarr stores are never modified. No artificial vertical interpolation.
"""
from __future__ import annotations
import argparse
from array import array
import datetime as dt
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
from retention_policy import load_policy
import verification_convert as vc

import multi_model_to_zarr_wcoss2 as mm

PRESSURES=tuple(range(1000, 99, -25))
NATIVE={'t': 'TMP', 'q': 'SPFH', 'u': 'UGRD', 'v': 'VGRD'}
UNITS={'t': 'K', 'q': 'kg kg-1', 'td': 'K', 'u': 'm s-1', 'v': 'm s-1'}
LONG={'t':'Native isobaric temperature', 'q':'Native isobaric specific humidity',
      'td':'Dewpoint derived from native specific humidity and pressure',
      'u':'Native isobaric U wind', 'v':'Native isobaric V wind'}
VALID_MODELS=mm.MODELS+('rap_analysis',)
# Operational REFS Mean has direct TMP/DPT/UGRD/VGRD fields at sparse levels.
# It does not publish SPFH in its mean inventory.
MEAN_NATIVE={'t':'TMP','td':'DPT','u':'UGRD','v':'VGRD'}
NAME_RE=re.compile(r'^sounding_(rrfs|refs_mean|refs_m00[1-5]|hrrr|rap_analysis)_(\d{8})_(\d{2})00\.zarr$')
RAP_ROOT='https://noaa-rap-pds.s3.amazonaws.com'



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
    if component in ('t','td'):return v if 150 <=v<=360 else math.nan
    if component=='q':return v if 0<=v<.1 else math.nan
    return v if -250<=v<=250 else math.nan


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
            dewpoint_formula='Magnus water-phase inversion of e=q*p/(0.622+0.378*q)',
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


def profile_hour(client,wgrib2,store,model,date,cycle,h,step,level,work,keep_grib=False):
    """Return count of native pressure-level records; publish only complete hours."""
    files= ('rap',) if model=='rap_analysis' else ('prslevnomads',) if model.startswith('refs_m') else ('prslev',) if model=='rrfs' else ('wrfprs',) if model=='hrrr' else ('mean',)
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
    for p in PRESSURES:
        for var,short in requested_fields.items():
            for product,url,rows in inventory:
                found=native_record(rows,short,p)
                if found is not None:
                    by_product.setdefault(product,[]).append({'output':(var,p),'record':found,'url':url})
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
    fields={k:array('f',[math.nan])*(len(PRESSURES)*size) for k in set(NATIVE)|{'td'}}
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
                var,p=item['output'];idx=PRESSURES.index(p)
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
            for i in range(lo,hi):dew[i]=dewpoint_kelvin(fields['q'][i],p)
        fields['td']=dew
    # Commit all 5 arrays. The hour completion stamp is written last.
    for name,values in fields.items():
        mm.core.write_compressed_chunk(store/name/f'{h}.0.0.0',to_bytes(values),level)
    mm.core.json_write(store/'.hours'/f'{h:03d}.json',{'hour':h,'native_records':sum(map(len,by_product.values())),
        'native_levels':{name:[p for i,p in enumerate(PRESSURES) if any(math.isfinite(v) for v in fields[name][i*size:(i+1)*size])]
                         for name in requested_fields},
        'dewpoint_source':'native DPT' if model=='refs_mean' else 'derived from SPFH',
        'dewpoint_levels':[p for i,p in enumerate(PRESSURES) if any(math.isfinite(v) for v in dew[i*size:(i+1)*size])]})
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
            'variables':[{'name':n,'units':UNITS[n]} for n in (('t','td','u','v') if attrs['model_id']=='refs_mean' else ('t','td','q','u','v'))],
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
    ap.add_argument('--max-hours-per-model',type=int,default=4,help='Limit new hours per run (default 4)')
    ap.add_argument('--grid-stride',type=int,default=None,help='Native cells per sounding point (default 4 => 12 km for 3-km models; 1 => native 13 km for RAP)')
    ap.add_argument('--compression-level',type=int,default=5)
    ap.add_argument('--output-root',default='zarr')
    ap.add_argument('--work-root',default='work')
    ap.add_argument('--wgrib2',default=None)
    ap.add_argument('--timeout',type=int,default=90)
    ap.add_argument('--retries',type=int,default=3)
    ap.add_argument('--keep-grib',action='store_true')
    ap.add_argument('--missing-retry-hours',type=float,default=6.0,help='Retry a native-profile absence no more than every N hours (default 6)')
    ap.add_argument('--no-publish',action='store_true',help='Skip publishing to RZDM')
    ap.add_argument('--retention-days',type=int,default=7)
    ap.add_argument('--protected-cycles-file',default='protected_cycles.json')
    ap.add_argument('--cleanup-only',action='store_true',help='Prune expired profile stores safely, honoring protected cycles')
    ap.add_argument('--publish-only',action='store_true',help='Publish existing profile stores/catalog without decoding GRIB2')
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
    incoming='.catalog_profiles.json.incoming'
    subprocess.run(['scp',str(catalog),host+':'+remote_dir+'/'+incoming],check=True)
    # Catalog must be installed first, to avoid exposing removed stores.
    script=('set -eu; cd '+shlex.quote(remote_dir)+'; '
           'test "$(pwd -P)" = '+shlex.quote(remote_dir.rstrip('/'))+'; '
           'mv -f -- '+shlex.quote(incoming)+' catalog_profiles.json; ')
    for name in names:
        script+='rm -rf -- '+shlex.quote(name)+'; '
    subprocess.run(['ssh',host,script],check=True)
    pending_file.unlink(missing_ok=True)


def web_permissions(store):
    """Make newly published profile assets world-readable; never touch secrets."""
    for folder,dirs,files in os.walk(store):
        os.chmod(folder,0o755)
        for f in files:
            os.chmod(os.path.join(folder,f),0o644)


def main(argv=None):
    a=parse_args(argv)
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
            publish_profile_cleanup(root,work,a.publish_host,a.publish_dir,removed)
        return 0
    for model in (() if a.publish_only else a.models):
        hours=model_hour_list(root,model,a.date,a.cycle,a.hours)
        if not hours:
            logging.warning('%s: no candidate hours (model not yet converted?)',model);continue
        store=root/f'sounding_{model}_{a.date}_{a.cycle}00.zarr'
        if model!='rap_analysis':
            store,nx,ny=create_store(root,model,a.date,a.cycle,a.grid_stride,a.compression_level)
        else:
            nx=ny=0  # Discover RAP's native projection from a GRIB message.
        count=0
        for h in hours:
            if count>=a.max_hours_per_model:break
            if (store/'.hours'/f'{h:03d}.json').exists():continue
            hour_work=work/'sounding_profiles'/model/f'{a.date}_{a.cycle}'/f'f{h:03d}'
            missing_marker=hour_work/'.missing_native.json'
            if not a.hours and missing_marker.exists() and a.missing_retry_hours:
                if dt.datetime.now(dt.timezone.utc).timestamp()-missing_marker.stat().st_mtime < a.missing_retry_hours*3600:
                    continue
            count+=1
            try:
                n=profile_hour(client,wgrib2,store,model,a.date,a.cycle,h,a.grid_stride,a.compression_level,
                               hour_work,a.keep_grib)
                if not n:
                    mm.core.json_write(missing_marker,{'last_attempt_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
                                                       'reason':'no native pressure-level inputs found'})
                    logging.info('%s F%03d: no native profile records yet',model,h);continue
                missing_marker.unlink(missing_ok=True)
                if model=='rap_analysis':ny,nx=coarse_dims(a.grid_stride,451,337)
                logging.info('%s F%03d: native records %d, %dx%d sampled grid',model,h,n,nx,ny)
                done+=1;updated.append(store)
            except Exception as exc:
                logging.exception('%s F%03d: sounding profile failed: %s',model,h,exc);errors+=1
    catalog=refresh_catalog(root)
    if not a.no_publish:
        if a.publish_only:
            updated=[root/f'sounding_{model}_{a.date}_{a.cycle}00.zarr' for model in a.models]
            updated=[store for store in updated if (store/'.hours').is_dir()]
        transfers=list(dict.fromkeys(updated))
        transferred=[]
        for store in transfers:
            try:
                # Make every store available before advertising it in the
                # shared catalog. A successful publication snapshot is saved.
                web_permissions(store)
                mm.publish_one(store,None,work,a.publish_host,a.publish_dir,publish_mode='auto')
                transferred.append(store)
            except Exception as exc:
                logging.exception('Could not publish %s: %s',store,exc);errors+=1
        if transferred:
            try:
                # Only advertise stores with confirmed publication snapshots;
                # a local-only case must not appear in the browser catalog.
                remote_catalog=refresh_catalog(root,published_only=True,published_work=work,
                                               destination=work/'catalog_profiles.json')
                # Publish the catalog last, after all changed stores are live.
                mm.publish_one(transferred[-1],remote_catalog,work,a.publish_host,a.publish_dir,publish_mode='auto')
            except Exception as exc:
                logging.exception('Could not publish profile catalog: %s',exc);errors+=1
    logging.info('Sounding profile summary: converted hours=%d failed=%d publish=%s',done,errors,not a.no_publish)
    return 1 if errors else 0

if __name__=='__main__':sys.exit(main())
