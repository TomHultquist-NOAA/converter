#!/usr/bin/env python3
"""Acquire and inventory hourly MRMS, URMA and RAP verification GRIB2 on WCOSS2.

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
import xml.etree.ElementTree as ET

UTC=dt.timezone.utc
BUCKETS={
    'rap':'https://noaa-rap-pds.s3.amazonaws.com',
    'urma':'https://noaa-urma-pds.s3.amazonaws.com',
    'rtma':'https://noaa-rtma-pds.s3.amazonaws.com',
    'mrms':'https://noaa-mrms-pds.s3.amazonaws.com',
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
    def get(self,url):
        with self.request(url) as f: return f.read()
    def download(self,url,dest):
        dest.parent.mkdir(parents=True,exist_ok=True)
        temp=dest.with_name(dest.name+'.part')
        try:
            with self.request(url) as src,temp.open('wb') as dst:
                shutil.copyfileobj(src,dst,1024*1024)
            if temp.stat().st_size<100: raise ValueError('Downloaded empty/tiny file: '+url)
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
        matches=[line for line in lines if f':{short}:' in line and f':{level}:' in line.lower()]
        # wgrib2's surface/level strings occasionally differ between files.
        # Leave missing rather than incorrectly map a nearby vertical level.
        if not matches:missing.append(field)
        else:selected[field]=matches[0]
    return selected,missing

def jobs_for(t,cfg):
    d=t.strftime('%Y%m%d');h=t.strftime('%H')
    return [
      ('rap_surface',f"{BUCKETS['rap']}/rap.{d}/rap.t{h}z.awp130bgrbf00.grib2",'rap'),
      ('rap_pressure',f"{BUCKETS['rap']}/rap.{d}/rap.t{h}z.awp130pgrbf00.grib2",'rap'),
      ('urma',f"{BUCKETS['urma']}/urma2p5.{d}/urma2p5.t{h}z.2dvaranl_ndfd.grb2_wexp",'urma'),
      ('rtma',f"{BUCKETS['rtma']}/rtma2p5.{d}/rtma2p5.t{h}z.2dvaranl_ndfd.grb2_wexp",'rtma'),
      ('mrms_refc',None,'mrms_refc'),('mrms_prate',None,'mrms_prate'),('mrms_apcp',None,'mrms_apcp')]

def valid_now(t,src,now,cfg):
    delay=dt.timedelta(minutes=cfg['source_delays_minutes'][src])
    return t+delay<=now

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
    previous_file=previous.get('file')
    if (previous.get('status')=='complete' and not force
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
        selected,missing=extract_selected(wgrib2,raw,acquired,fields)
        if not acquired.exists():raise ValueError('No required fields in '+data_url)
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

def cleanup(root,now,days):
    cutoff=now-dt.timedelta(days=days)
    removed=0
    for source in ('rap_surface','rap_pressure','urma','rtma','mrms_refc','mrms_prate','mrms_apcp'):
        area=root/source
        if not area.exists():continue
        for daydir in area.iterdir():
            if not daydir.is_dir() or not re.fullmatch(r'\d{8}',daydir.name):continue
            day=dt.datetime.strptime(daydir.name,'%Y%m%d').replace(tzinfo=UTC)
            # Retain day-folder containing a retained hour. Full-day pruning is
            # slightly conservative but never deletes data inside 7-day window.
            if day+dt.timedelta(days=1)>cutoff:continue
            if daydir.is_symlink(): continue
            shutil.rmtree(daydir);removed+=1
    return removed

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config',type=Path,default=Path(__file__).with_name('verification_sources.json'))
    ap.add_argument('--root',type=Path,required=True,help='Local staging root; outside your published Zarr tree')
    ap.add_argument('--wgrib2',default=os.getenv('WGRIB2') or shutil.which('wgrib2'))
    ap.add_argument('--lookback-hours',type=int,default=14,help='Revisit recent valid hours; catches delayed RAP/URMA')
    ap.add_argument('--backfill-hours',type=int,default=0,help='Override lookback to backfill within retention window')
    ap.add_argument('--source',choices=['all','rap','urma','rtma','mrms'],default='all')
    ap.add_argument('--now',help='Override current UTC hour for tests (YYYYMMDDHH)')
    ap.add_argument('--dry-run',action='store_true')
    ap.add_argument('--force',action='store_true')
    ap.add_argument('--keep-raw',action='store_true')
    ap.add_argument('--no-cleanup',action='store_true')
    ap.add_argument('--verbose',action='store_true')
    args=ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
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
    removed=0 if args.dry_run or args.no_cleanup else cleanup(root,now,conf['retention_days'])
    LOG.info('SUMMARY %s | removed_old_daydirs=%d',summ,removed)
    # Pending data is normal; the next hourly pass will retry.
    return 0

if __name__=='__main__':sys.exit(main())
