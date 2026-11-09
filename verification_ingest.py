#!/usr/bin/env python3
"""Acquire and inventory hourly MRMS, URMA and RAP verification GRIB2.

Uses Python 3 standard library and optional wgrib2 for selective GRIB extraction.
It does NOT convert files to Zarr or publish catalogs: that requires source-grid
handling / observed-valid-time mapping in the multi-model processor and viewer.
Only downloaded/extracted files are marked complete; unavailable data is retried.
"""
from __future__ import annotations
import argparse
import datetime as dt
import gzip
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import perf_monitor
import xml.etree.ElementTree as ET

UTC=dt.timezone.utc
BUCKETS={
    'rap':'https://noaa-rap-pds.s3.amazonaws.com',
    'urma':'https://noaa-urma-pds.s3.amazonaws.com',
    'rtma':'https://noaa-rtma-pds.s3.amazonaws.com',
    'mrms':'https://noaa-mrms-pds.s3.amazonaws.com',
    'nohrsc':'https://www.nohrsc.noaa.gov/snowfall_v2/data',
    'stageiv':'https://nomads.ncep.noaa.gov/pub/data/nccf/com/pcpanl/prod',
    'ccpa':'https://nomads.ncep.noaa.gov/pub/data/nccf/com/ccpa/prod',
}
LOG=logging.getLogger('evu-verify')

def iso(t): return t.strftime('%Y-%m-%dT%H:%M:%SZ')
def hour(t): return t.replace(minute=0,second=0,microsecond=0)
def stamp(t): return t.strftime('%Y%m%d%H')

def atomic_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(value,f,indent=2,sort_keys=True);f.write('\n')
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)

def load_json(path):
    return json.loads(path.read_text()) if path.exists() else {}

class Client:
    def __init__(self,timeout=80,retries=3): self.timeout,self.retries=timeout,retries
    def request(self,url,method='GET'):
        for attempt in range(self.retries+1):
            try:
                req=urllib.request.Request(url,method=method,headers={'User-Agent':'OMD-EVU-verification-ingest/1.0'})
                return urllib.request.urlopen(req,timeout=self.timeout)
            except (urllib.error.URLError,TimeoutError) as exc:
                status=getattr(exc,'code',None)
                if status in (400,401,403,404) or attempt==self.retries: raise
                time.sleep(min(3*2**attempt,25))
    def request_url(self,req):
        for attempt in range(self.retries+1):
            try:return urllib.request.urlopen(req,timeout=self.timeout)
            except (urllib.error.URLError,TimeoutError) as exc:
                if getattr(exc,'code',None) in (400,401,403,404) or attempt==self.retries:raise
                time.sleep(min(3*2**attempt,25))
    def get(self,url):
        with self.request(url) as f: return f.read()
    def download(self,url,dest):
        dest.parent.mkdir(parents=True,exist_ok=True)
        temp=dest.with_name(dest.name+'.part')
        try:
            started = time.monotonic()
            with self.request(url) as src,temp.open('wb') as dst:
                shutil.copyfileobj(src,dst,1024*1024)
            size = temp.stat().st_size
            perf_monitor.add_metric('analysis_download_bytes', size)
            perf_monitor.add_metric('analysis_download_seconds', time.monotonic() - started)
            if size<100: raise ValueError('Downloaded empty/tiny file: '+url)
            os.replace(temp,dest)
        finally: temp.unlink(missing_ok=True)

# Public S3 ListObjectsV2: no AWS credentials, boto3, jq, or aws-cli required.
def s3_keys(client,base,prefix,max_pages=20):
    token=None;result=[]
    for _ in range(max_pages):
        params={'list-type':'2','prefix':prefix,'max-keys':'1000'}
        if token: params['continuation-token']=token
        url=base+'/?'+urllib.parse.urlencode(params)
        root=ET.fromstring(client.get(url))
        for node in root.findall('.//{*}Contents'):
            key=node.findtext('{*}Key')
            if key: result.append(key)
        if root.findtext('.//{*}IsTruncated')!='true':break
        token=root.findtext('.//{*}NextContinuationToken')
        if not token:break
    return result

def mrms_file_time(name):
    m=re.search(r'(\d{8})-(\d{6})\.grib2(?:\.gz)?$',name)
    if not m: return None
    try:return dt.datetime.strptime(''.join(m.groups()),'%Y%m%d%H%M%S').replace(tzinfo=UTC)
    except ValueError:return None

def mrms_candidate_keys(client,t,product,config):
    day=t.strftime('%Y%m%d')
    if not hasattr(client,'mrms_listing_cache'):client.mrms_listing_cache={}
    cache_key=(day,product)
    if cache_key in client.mrms_listing_cache:return client.mrms_listing_cache[cache_key]
    # S3 directories differ from NCEP /2D/ inventory and may use the
    # product name with its level. Discover the actual object key via listing.
    prefixes=[f'CONUS/{product}/{day}/',f'CONUS/{product}/',
              f'CONUS/{product.rsplit("_",1)[0]}/{day}/']
    seen=set();keys=[]
    for prefix in prefixes:
        if prefix in seen:continue
        seen.add(prefix)
        found=s3_keys(client,BUCKETS['mrms'],prefix,max_pages=3 if not prefix.endswith(day+'/') else 20)
        keys.extend(k for k in found if day in k and k.endswith(('.grib2','.grib2.gz')))
        if keys: break
    result=sorted(set(keys))
    client.mrms_listing_cache[cache_key]=result
    return result

def mrms_select(keys,requested,product,tolerance=8):
    # Require a sample near the exact UTC verification hour: do not quietly
    # substitute a 40-minute-old radar snapshot or adjacent-hour QPE.
    eligible=[]
    needle=product.split('_00.')[0]
    for k in keys:
        if needle not in k: continue
        file_time=mrms_file_time(k)
        if file_time is None: continue
        err=abs((file_time-requested).total_seconds())
        if err<=tolerance*60: eligible.append((err, -file_time.timestamp(), k,file_time))
    if not eligible:return None
    _,_,key,observed=sorted(eligible)[0]
    return key,observed

def inventory(wgrib2,path):
    result=subprocess.run([wgrib2,str(path),'-s'],check=True,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    return result.stdout.splitlines()

def extract_selected(wgrib2,source,out,fields):
    """Create selected-message GRIB2 with one message/field, preserving native grid."""
    lines=inventory(wgrib2,source)
    # NOHRSC's single snowfall record must be genuine snow accumulation.
    # Reject a changed/ancillary first record instead of mislabeling it.
    if all(k.startswith('snow') and k.endswith('h') for k in fields):
        snowfall=[v for v in lines if re.search(r':(?:SNOW|ASNOW):',v,re.I)]
        if not snowfall:
            LOG.warning('NOHRSC snowfall field absent from GRIB inventory %s: %s',
                        source, ' | '.join(lines[:6])[:900])
        selected={next(iter(fields)): snowfall[0]} if snowfall else {}
        missing=[] if selected else list(fields)
    else:
        selected,missing=select_inventory_records(lines,fields)
    if not selected:return [],missing
    parts=[]
    try:
        for field,line in selected.items():
            record=line.split(':',1)[0]
            p=out.with_name(f'.{out.name}.{field}.part')
            subprocess.run([wgrib2,str(source),'-d',record,'-grib',str(p)],check=True,
                           stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            if not p.exists() or p.stat().st_size<16:raise ValueError('empty GRIB selection')
            parts.append(p)
        temp=out.with_name(out.name+'.part')
        with temp.open('wb') as dest:
            for p in parts:
                with p.open('rb') as f:shutil.copyfileobj(f,dest)
        os.replace(temp,out)
    finally:
        for p in parts:p.unlink(missing_ok=True)
    return [{'field':f,'inventory':line} for f,line in selected.items()],missing

def select_inventory_records(lines,fields):
    """Pure inventory selector, useful for testing field names and level matching."""
    selected={}; missing=[]
    for field,(short,level) in fields.items():
        if not short:
            if lines: selected[field]=lines[0]
            else: missing.append(field)
            continue
        # NOAA RAP's MSLMA has a local-table suffix ("MSLMA M") in some
        # inventories; require the exact sea-level record, never surface PRES.
        matches=[]
        for line in lines:
            parts=line.split(':')
            if len(parts)<5:continue
            if short=='MSLMA':
                matches_short=bool(re.fullmatch(r'MSLMA(?:\s+M)?',parts[3].strip(),re.I))
            else:
                matches_short=parts[3].strip().upper()==short.upper()
            matches_level=parts[4].strip().lower()==level.lower()
            if matches_short and matches_level:matches.append(line)
        # wgrib2's surface/level strings occasionally differ between files.
        # Leave missing rather than incorrectly map a nearby vertical level.
        if not matches:missing.append(field)
        else:selected[field]=matches[0]
    return selected,missing

def nohrsc_jobs(t):
    # Accumulation interval ends exactly at valid UTC time.
    # 6-hour analyses are issued at 00/06/12/18; 24-hour at 00/12.
    # A 48-hour analysis is derived in the viewer by summing two 24-h analyses.
    if t.hour%6:return []
    stamp_ = stamp(t)
    periods=(6,24) if t.hour in (0,12) else (6,) # No new 48/72-h downloads
    return [(f'nohrsc_{p}h',
             f"{BUCKETS['nohrsc']}/{t:%Y%m}/sfav2_CONUS_{p}h_{stamp_}_grid184.grb2",
             'nohrsc') for p in periods]

def ccpa_jobs(t):
    """CCPA 1/3/6-h CONUS analyses, ending at the requested valid UTC hour.

    0p125 lat/lon preserves data geometry for explicit downstream regridding.
    Do not manufacture hours unavailable from the native source.
    """
    # Hourly CCPA exists at every UTC valid hour, not just the 3-hour periods.
    # CCPA files are grouped in 6-hour *publication* directories. The
    # 03/09/15/21 UTC three-hour products reside in the next 06/12/18/00
    # directory, including next-day rollover for 21 UTC.
    directory_time=t+dt.timedelta(hours=(-t.hour)%6)
    d=directory_time.strftime('%Y%m%d');group=directory_time.strftime('%H')
    h=t.strftime('%H')
    periods=(1,3,6) if t.hour%6==0 else ((1,3) if t.hour%3==0 else (1,))
    return [(f'ccpa_{period}h',
             f"{BUCKETS['ccpa']}/ccpa.{d}/{group}/ccpa.t{h}z.{period:02d}h.0p125.conus.gb2",
             'ccpa') for period in periods]

def stageiv_jobs(t, cfg):
    d=t.strftime('%Y%m%d');h=t.strftime('%H')
    products=[('stageiv_apcp',1)]
    # Native longer Stage IV analyses have additional quality control and
    # are not necessarily equal to summed one-hour QPE.
    if cfg.get('stageiv_native_accumulations', True):
        if t.hour%6==0: products.append(('stageiv_6h',6))
        if t.hour==12: products.append(('stageiv_24h',24))
    return [(name,f"{BUCKETS['stageiv']}/pcpanl.{d}/st4_conus.{d}{h}.{period:02d}h.grb2",'stageiv')
            for name,period in products]


def mrms_accumulation_jobs(t,cfg):
    result=[]
    periods=cfg.get('mrms_native_accumulations',[])
    if (not isinstance(periods,list) or any(type(p) is not int or p not in (6,24) for p in periods)
        or len(set(periods))!=len(periods)):
        raise ValueError('mrms_native_accumulations must be a list containing only 6 and/or 24')
    for period in periods:
        # These high-resolution products are large; default is off. Publishing
        # at synoptic valid times avoids seven redundant copies per day.
        if period==6 and t.hour%6==0 or period==24 and t.hour==12:
            result.append((f'mrms_apcp_{period}h',None,'mrms_apcp_pass2'))
    if cfg.get('mrms_pass2_hourly',False):
        result.append(('mrms_apcp_pass2',None,'mrms_apcp_pass2'))
    return result


def jobs_for(t,cfg):
    d=t.strftime('%Y%m%d');h=t.strftime('%H')
    return [
      ('rap_surface',f"{BUCKETS['rap']}/rap.{d}/rap.t{h}z.awp130bgrbf00.grib2",'rap'),
      ('rap_pressure',f"{BUCKETS['rap']}/rap.{d}/rap.t{h}z.awp130pgrbf00.grib2",'rap'),
      ('urma',f"{BUCKETS['urma']}/urma2p5.{d}/urma2p5.t{h}z.2dvaranl_ndfd.grb2_wexp",'urma'),
      ('rtma',f"{BUCKETS['rtma']}/rtma2p5.{d}/rtma2p5.t{h}z.2dvaranl_ndfd.grb2_wexp",'rtma'),
      ('mrms_refc',None,'mrms_refc'),('mrms_prate',None,'mrms_prate'),('mrms_apcp',None,'mrms_apcp'),('mrms_rotation_ml60',None,'mrms_rotation_ml60'),
      ] + stageiv_jobs(t,cfg) + mrms_accumulation_jobs(t,cfg) + nohrsc_jobs(t) + ccpa_jobs(t)

def valid_now(t,src,now,cfg):
    delay=dt.timedelta(minutes=cfg['source_delays_minutes'][src])
    return t+delay<=now

@perf_monitor.timed_task('analysis_ingest', lambda a, k: '%s %s' % (a[4][0], a[3].strftime('%Y%m%d%H')))
def process_job(client,wgrib2,root,t,job,config,now,plan=False,force=False,keep_raw=False):
    ident,url,src=job
    if not valid_now(t,src,now,config):return 'not_due'
    directory=root/ident/t.strftime('%Y%m%d')
    directory.mkdir(parents=True,exist_ok=True)
    name=f'{ident}_{stamp(t)}';manifest=directory/(name+'.json')
    previous=load_json(manifest)
    fields=config['fields'][ident]
    # v1 could mark a job COMPLETE despite missing requested fields. A changed
    # selection (e.g. URMA GUST at 10 m) should trigger automatic re-extraction.
    selected_before={x.get('field') for x in previous.get('selected',[]) if isinstance(x,dict)}
    # The same field key may have acquired a corrected native layer selector
    # (e.g. MUCAPE). Do not reuse a staged GRIB that has the old record.
    selectors_current=all(
        not entry.get('inventory') or entry.get('field') not in fields or
        entry['field'] in select_inventory_records([entry['inventory']],
                                                   {entry['field']:fields[entry['field']]})[0]
        for entry in previous.get('selected',[]) if isinstance(entry,dict))
    previous_file=previous.get('file')
    if (previous.get('status')=='complete' and not force and selectors_current
        and set(fields).issubset(selected_before) and previous_file
        and (root/previous_file).is_file()):
        # Preserve staged GRIB, but enrich v1 manifests with provenance for
        # derived fields that will be constructed by the future Zarr stage.
        derived={key:meta for key,meta in config.get('derived_at_zarr_stage',{}).items()
                 if meta.get('source')==ident and set(meta.get('inputs',[])).issubset(selected_before)}
        if previous.get('derived_at_zarr_stage')!=derived:
            previous['derived_at_zarr_stage']=derived
            atomic_json(manifest,previous)
        return 'already'
    if plan:
        LOG.info('PLAN %s %s %s',iso(t),ident,url or 'S3 MRMS product discovery')
        return 'planned'
    acquired=directory/(name+'.grib2')
    raw=directory/(name+'.source.grib2')
    tmpgz=directory/(name+'.source.grib2.gz')
    actual_time=t;data_url=url
    try:
        if url is None:
            product=config['mrms_products'][ident]
            keys=mrms_candidate_keys(client,t,product,config)
            chosen=mrms_select(keys,t,product,config['mrms_hourly_sample_tolerance_minutes'])
            if chosen is None:raise FileNotFoundError(f'No MRMS {product} sample within time tolerance for {iso(t)}')
            key,actual_time=chosen
            data_url=BUCKETS['mrms']+'/'+urllib.parse.quote(key,safe='/')
        LOG.info('Downloading %s %s',ident,iso(t))
        if data_url.endswith('.gz'):
            client.download(data_url,tmpgz)
            with gzip.open(tmpgz,'rb') as inp,raw.open('wb') as out:shutil.copyfileobj(inp,out,1024*1024)
            tmpgz.unlink(missing_ok=True)
        else:client.download(data_url,raw)
        with raw.open('rb') as f:
            if f.read(4)!=b'GRIB':raise ValueError('Downloaded content is not GRIB2: '+data_url)
        if ident.startswith('ccpa_'):
            lines=inventory(wgrib2,raw)
            period=int(ident.split('_')[1][:-1])
            if not any(re.search(r':APCP:',line,re.I) and re.search(r'(?<!\d)'+str(period)+r'\s*hour\s*acc',line,re.I) for line in lines):
                raise ValueError(f'CCPA source lacks a genuine {period}-hour APCP record')
        if ident.startswith('stageiv_'):
            # Accept only the explicitly requested native accumulation ending at this UTC valid time.
            # Some wgrib2 releases vary the surface-level label; never accept
            # an arbitrary first record without verifying its APCP interval.
            lines=inventory(wgrib2,raw)
            period=1 if ident=='stageiv_apcp' else int(ident.split('_')[1][:-1])
            if len(lines)!=1 or not re.search(r':APCP:',lines[0],re.I) or not re.search(r'(?<!\d)'+str(period)+r'\s*hour\s*acc',lines[0],re.I):
                raise ValueError('Stage IV input is not a single '+str(period)+'-hour APCP GRIB record: '+str(lines[:2]))
        selected,missing=extract_selected(wgrib2,raw,acquired,fields)
        if not acquired.exists():raise ValueError('No required fields in '+data_url)
        # RAP wrfprs contains native 0-1 km shear and USTM/VSTM absent in
        # awp130. Fetch only indexed records, then explicitly regrid onto the
        # awp130 analysis domain (never concatenate differing native grids).
        if ident=='rap_pressure' and any(k in missing for k in ('shru01','shrv01','ustm','vstm')):
            with tempfile.TemporaryDirectory(prefix='evu-rap-native-') as td:
                supplemental,found=append_native_rap_wrfprs(client,wgrib2,acquired,t,missing,Path(td))
            if supplemental:
                selected.extend(supplemental)
                missing=[k for k in missing if k not in found]
        derivations={key:meta for key,meta in config.get('derived_at_zarr_stage',{}).items()
                     if meta.get('source')==ident and set(meta.get('inputs',[])).issubset({x['field'] for x in selected})}
        status='partial' if missing else 'complete'
        payload={'status':status,'valid_time':iso(t),'source_time':iso(actual_time),
                 'source':src,'product':ident,'url':data_url,'selected':selected,
                 'missing':missing,'derived_at_zarr_stage':derivations,'file':str(acquired.relative_to(root)),'bytes':acquired.stat().st_size}
        atomic_json(manifest,payload)
        LOG.log(logging.WARNING if missing else logging.INFO,'%s %s %s fields=%d missing=%s derived_later=%s',
                status.upper(),ident,iso(t),len(selected),','.join(missing) or '-',
                ','.join(derivations) or '-')
        return status
    except Exception as err:
        acquired.unlink(missing_ok=True)
        LOG.warning('PENDING %s %s: %s',ident,iso(t),err)
        # A negative result is NOT treated as success; hourly retry can fill it.
        atomic_json(manifest,{'status':'pending','valid_time':iso(t),'source':src,'error':str(err),'url':data_url})
        return 'pending'
    finally:
        if not keep_raw:
            raw.unlink(missing_ok=True);tmpgz.unlink(missing_ok=True)

def indexed_grib_records(client, url, wanted, out):
    """Download only required GRIB2 messages from an indexed alternate product.

    Reject ignored/short HTTP ranges; never pull the full ~200 MB RAP wrfprs.
    Returns native field names in message order, or an empty list. The last
    message is deliberately not downloadable without verified object size.
    """
    idx=client.get(url+'.idx').decode('utf-8','replace')
    entries=[]
    for line in idx.splitlines():
        parts=line.split(':')
        if len(parts)<5:continue
        try:entries.append((int(parts[1]),line))
        except ValueError:continue
    matches=[]
    for i,(start,line) in enumerate(entries[:-1]):
        entry=select_inventory_records([line],wanted)[0]
        if not entry:continue
        matches.append((start,entries[i+1][0]-1,entry))
    # Every requested field must be present, as a partial pair is unsafe.
    chosen={key:(start,end) for start,end,rec in matches for key in rec}
    if not set(wanted).issubset(chosen):return []
    out.parent.mkdir(parents=True,exist_ok=True)
    tmp=out.with_suffix(out.suffix+'.incoming')
    ordered=sorted(chosen.items(),key=lambda x:x[1][0])
    try:
        with tmp.open('wb') as dst:
            for field,(start,end) in ordered:
                req=urllib.request.Request(url,headers={'Range':f'bytes={start}-{end}',
                                      'User-Agent':'OMD-EVU-native-rap-1.0'})
                with client.request_url(req) as resp:
                    if getattr(resp,'status',None)!=206:
                        raise ValueError('Native RAP source did not honor HTTP Range')
                    payload=resp.read()
                if len(payload)!=end-start+1 or not payload.startswith(b'GRIB'):
                    raise ValueError('Invalid native RAP GRIB2 range '+field)
                dst.write(payload)
        os.replace(tmp,out)
    finally:tmp.unlink(missing_ok=True)
    return [field for field,_ in ordered]


def _lambert_new_grid_args(grid):
    """wgrib2's Lambert specification, from the actual awp130 grid."""
    if grid['GRIB_gridType']!='lambert':
        raise ValueError('Native RAP supplemental field target must be Lambert')
    def f(key):return f'{float(grid[key]):.9f}'
    return ['-new_grid','lambert:'+':'.join(f(k) for k in
            ('GRIB_LoVInDegrees','GRIB_Latin1InDegrees','GRIB_Latin2InDegrees','GRIB_LaDInDegrees')),
            ':'.join((f('GRIB_longitudeOfFirstGridPointInDegrees'),str(int(grid['GRIB_Nx'])),f('GRIB_DxInMetres'))),
            ':'.join((f('GRIB_latitudeOfFirstGridPointInDegrees'),str(int(grid['GRIB_Ny'])),f('GRIB_DyInMetres')))]


def append_native_rap_wrfprs(client,wgrib2,base_file,valid,missing,working):
    """Prefer native RAP 0-1km shear and right-mover, regridded to awp130.

    Do not mix the rotated native RAP grid with awp130. If the targeted
    acquisition or regrid fails, leave existing staged fields intact; RAP's
    independently labeled vertical-profile estimates remain the fallback.
    """
    wanted_all={'shru01':('VUCSH','0-1000 m above ground'),
                'shrv01':('VVCSH','0-1000 m above ground'),
                'ustm':('USTM','0-6000 m above ground'),
                'vstm':('VSTM','0-6000 m above ground')}
    missing=set(missing)
    wanted={k:v for k,v in wanted_all.items() if k in missing}
    if not wanted:return [],[]
    # Transfer dependent vector components together, never half a pair.
    if set(wanted)&{'shru01','shrv01'}:wanted.update({k:wanted_all[k] for k in ('shru01','shrv01')})
    if set(wanted)&{'ustm','vstm'}:wanted.update({k:wanted_all[k] for k in ('ustm','vstm')})
    day=valid.strftime('%Y%m%d');hour=valid.strftime('%H')
    url=f"{BUCKETS['rap']}/rap.{day}/rap.t{hour}z.wrfprsf00.grib2"
    selected_raw=working/'rap_native.selected.grib2'
    target=working/'rap_native.regridded.grib2'
    try:
        fields=indexed_grib_records(client,url,wanted,selected_raw)
        if not fields:
            LOG.info('RAP wrfprs lacks selected native records; will use existing/derived fields %s',sorted(wanted))
            return [],[]
        from verification_convert import grid_details
        original_grid=grid_details(wgrib2,base_file)
        subprocess.run([wgrib2,str(selected_raw),'-new_grid_winds','earth',
                        '-new_grid_interpolation','bilinear',
                        *_lambert_new_grid_args(original_grid),str(target)],
                       check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        supplemental_grid=grid_details(wgrib2,target)
        if (original_grid['GRIB_Nx']!=supplemental_grid['GRIB_Nx'] or
            original_grid['GRIB_Ny']!=supplemental_grid['GRIB_Ny'] or
            original_grid['GRIB_gridType']!=supplemental_grid['GRIB_gridType'] or
            any(abs(float(original_grid[k])-float(supplemental_grid[k]))>0.005 for k in
                ('GRIB_longitudeOfFirstGridPointInDegrees','GRIB_latitudeOfFirstGridPointInDegrees',
                 'GRIB_DxInMetres','GRIB_DyInMetres','GRIB_LoVInDegrees','GRIB_LaDInDegrees'))):
            raise ValueError('Native wrfprs regrid differs from primary RAP source grid')
        supplement_lines=inventory(wgrib2,target)
        matched,absent=select_inventory_records(supplement_lines,wanted)
        if absent:raise ValueError('Regridded RAP native fields incomplete: '+','.join(absent))
        # Replace any stale record in main source only when all components are
        # absent; the standard ingestion source omits both fields today.
        with base_file.open('ab') as dst, target.open('rb') as source:
            shutil.copyfileobj(source,dst,1024*1024)
        entries=[{'field':key,'inventory':line,'native_source':'wrfprs',
                  'native_url':url,'regridded_to':'awp130'} for key,line in
                 sorted(matched.items(),key=lambda item:int(item[1].split(':',1)[0]))]
        LOG.info('Supplemented RAP awp130 with native wrfprs fields: %s',','.join(matched))
        return entries,sorted(matched)
    except (OSError,ValueError,RuntimeError,subprocess.CalledProcessError,urllib.error.HTTPError,urllib.error.URLError) as exc:
        LOG.warning('RAP wrfprs native supplement unavailable/unsafe: %s; retaining original source',exc)
        return [],[]
    finally:
        selected_raw.unlink(missing_ok=True);target.unlink(missing_ok=True)


def cleanup(root, now, days, analysis_rules=()):
    """Expire old staging day folders, preserving protected analysis valid hours.

    Unknown filenames are never removed as a side effect of a partial-day prune.
    """
    from retention_policy import protects_analysis
    cutoff = now - dt.timedelta(days=days)
    removed = 0
    for source in ('rap_surface', 'rap_pressure', 'urma', 'rtma',
                   'mrms_refc', 'mrms_prate', 'mrms_apcp', 'mrms_rotation_ml60', 'stageiv_apcp',
                   'nohrsc_6h','nohrsc_24h','nohrsc_48h','nohrsc_72h','ccpa_1h','ccpa_3h','ccpa_6h', 'stageiv_6h','stageiv_24h','mrms_apcp_6h','mrms_apcp_24h','mrms_apcp_pass2'):
        area = root/source
        if not area.is_dir() or area.is_symlink(): continue
        for daydir in area.iterdir():
            if not daydir.is_dir() or daydir.is_symlink() or not re.fullmatch(r'\d{8}', daydir.name): continue
            day = dt.datetime.strptime(daydir.name, '%Y%m%d').replace(tzinfo=UTC)
            if day + dt.timedelta(days=1) > cutoff: continue
            # Never delete a protected hour's original manifest or staged GRIB.
            # A cycle-protection interval may cover only part of a UTC day.
            keep_any = any(protects_analysis(f'analysis_{source}_{daydir.name}{hour:02d}.zarr', analysis_rules)
                           for hour in range(24))
            if not keep_any:
                shutil.rmtree(daydir); removed += 1
                continue
            for child in daydir.iterdir():
                if child.is_symlink() or not child.is_file(): continue
                m = re.fullmatch(re.escape(source) + r'_(\d{10})\.(?:json|grib2|source\.grib2|source\.grib2\.gz)', child.name)
                if m and not protects_analysis(f'analysis_{source}_{m.group(1)}.zarr', analysis_rules):
                    child.unlink(); removed += 1
            if not any(daydir.iterdir()):
                daydir.rmdir()
    return removed


@perf_monitor.monitored('verification_ingest')
def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config',type=Path,default=Path(__file__).with_name('verification_sources.json'))
    ap.add_argument('--protected-cycles-file',type=Path,default=Path(__file__).with_name('protected_cycles.json'))
    ap.add_argument('--root',type=Path,required=True,help='Local staging root; outside your published Zarr tree')
    ap.add_argument('--wgrib2',default=os.getenv('WGRIB2') or shutil.which('wgrib2'))
    ap.add_argument('--lookback-hours',type=int,default=14,help='Revisit recent valid hours; catches delayed RAP/URMA')
    ap.add_argument('--backfill-hours',type=int,default=0,help='Override lookback to backfill within retention window')
    ap.add_argument('--source',choices=['all','rap','urma','rtma','mrms','stageiv','ccpa'],default='all')
    ap.add_argument('--now',help='Override current UTC hour for tests (YYYYMMDDHH)')
    ap.add_argument('--dry-run',action='store_true')
    ap.add_argument('--force',action='store_true')
    ap.add_argument('--keep-raw',action='store_true')
    ap.add_argument('--no-cleanup',action='store_true')
    ap.add_argument('--verbose',action='store_true')
    args=ap.parse_args(argv)
    perf_monitor.record_event('run_config', workflow='verification_ingest',
        source=args.source, lookback_hours=args.lookback_hours,
        backfill_hours=args.backfill_hours, dry_run=bool(args.dry_run))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    from retention_policy import load_policy
    policy=load_policy(args.protected_cycles_file)
    conf=load_json(args.config)
    if conf.get('schema')!='omd-evu-verification-source-map-1':ap.error('Configuration schema mismatch')
    if not args.dry_run and not args.wgrib2:ap.error('wgrib2 not found; pass --wgrib2 /absolute/path/to/wgrib2')
    now=dt.datetime.strptime(args.now,'%Y%m%d%H').replace(tzinfo=UTC) if args.now else dt.datetime.now(UTC)
    h=hour(now);count=args.backfill_hours or args.lookback_hours
    if count<1 or count>24*conf['retention_days']:ap.error('Lookback must be between 1 hour and retention window')
    root=args.root.expanduser().resolve();root.mkdir(parents=True,exist_ok=True)
    client=Client();summ={k:0 for k in ('complete','partial','pending','already','not_due','planned')}
    for idx in range(count-1,-1,-1):
        t=h-dt.timedelta(hours=idx)
        for job in jobs_for(t,conf):
            if args.source!='all' and (job[2].split('_',1)[0]!=args.source):continue
            result=process_job(client,args.wgrib2,root,t,job,conf,now,args.dry_run,args.force,args.keep_raw)
            summ[result]+=1
    removed=0 if args.dry_run or args.no_cleanup else cleanup(root,now,conf['retention_days'],policy.analysis_rules)
    LOG.info('SUMMARY %s | removed_old_daydirs=%d',summ,removed)
    # Pending data is normal; the next hourly pass will retry.
    return 0

if __name__=='__main__':sys.exit(main())
