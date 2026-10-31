#!/usr/bin/env python3
"""Batched historical model backfill using the operational converter safely.

Dry-run by default. Runs one cycle at a time, leaves existing completed hours
untouched, and uses the SAME protected whitelist and 14-day retention policy.
Pause hourly cron first; do not overlap a manual run with it.
"""
import argparse
import fcntl
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zlib

MODELS=('rrfs','hrrr','refs_mean','refs_m001','refs_m002','refs_m003','refs_m004','refs_m005')
LIMIT={'rrfs':84,'hrrr':48,'refs_mean':60,'refs_m001':60,'refs_m002':60,'refs_m003':60,'refs_m004':60,'refs_m005':60}

def already_complete(root,model,day,cyc):
    store=root/f'{model}_{day}_{cyc}00.zarr'
    file=store/'processed'/'0'
    if not file.is_file():return False
    try:
        flags=zlib.decompress(file.read_bytes())
        hours=range(1,61) if model=='refs_mean' else range(LIMIT[model]+1)
        return len(flags)>max(hours) and all(flags[h] for h in hours)
    except (OSError,zlib.error,ValueError):return False

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--start',required=True,help='First UTC initialization date, YYYYMMDD')
    p.add_argument('--end',required=True,help='Last UTC initialization date, YYYYMMDD (inclusive)')
    p.add_argument('--models',default='rrfs,hrrr,refs_mean',help='Comma-separated model IDs or all')
    p.add_argument('--root',type=Path,default=Path('./zarr'))
    p.add_argument('--work-root',type=Path,default=Path('./work'))
    p.add_argument('--protected-cycles-file',type=Path,default=Path('./protected_cycles.json'))
    p.add_argument('--workers',type=int,default=2)
    p.add_argument('--max-cycles',type=int,default=2,help='Maximum initialization cycles PER invocation')
    p.add_argument('--retention-days',type=int,default=7)
    p.add_argument('--skip-first',type=int,default=0,help='Skip this many candidates (for manually continuing after partial/unavailable cycles)')
    p.add_argument('--execute',action='store_true',help='Actually run the converter; omit to preview')
    p.add_argument('--no-publish',action='store_true')
    p.add_argument('--wgrib2',default=None)
    a=p.parse_args()
    start=dt.datetime.strptime(a.start,'%Y%m%d').replace(tzinfo=dt.timezone.utc)
    end=dt.datetime.strptime(a.end,'%Y%m%d').replace(tzinfo=dt.timezone.utc)
    if end<start or end-start>dt.timedelta(days=30):p.error('Invalid date range (max 31 days)')
    if a.skip_first<0:p.error('Invalid skip-first')
    if not 1<=a.retention_days<=30:p.error('Retention must be 1–30 days')
    if not 1<=a.max_cycles<=100:p.error('Invalid max cycles')
    if not 1<=a.workers<=12:p.error('Use 1–12 workers for a historical catchup')
    selected=MODELS if a.models=='all' else tuple(dict.fromkeys(s.strip() for s in a.models.split(',')))
    if not selected or any(model not in MODELS for model in selected):p.error('Unsupported model list')
    now=dt.datetime.now(dt.timezone.utc)
    cutoff=now-dt.timedelta(days=a.retention_days)
    if a.execute and not a.protected_cycles_file.is_file():p.error('Missing protected-cycle configuration')
    scheduled=0
    completed=0
    lock=None
    if a.execute:
        lock=open(Path(__file__).with_name('.multisource_cron.lock'),'a+')
        try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:p.error('Cron/converter already holds .multisource_cron.lock; retry when idle')
    d=start
    while d<=end:
        for hh in (0,6,12,18):
            cycle=d.replace(hour=hh)
            if cycle<cutoff or cycle>now-dt.timedelta(hours=4):continue
            missing=[model for model in selected if not already_complete(a.root,model,d.strftime('%Y%m%d'),f'{hh:02d}')]
            if not missing:continue
            print(f'{cycle:%Y-%m-%d %HZ}: missing/incomplete '+','.join(missing),flush=True)
            if a.skip_first<=scheduled<a.skip_first+a.max_cycles and a.execute:
                cmd=[sys.executable,str(Path(__file__).with_name('multi_model_to_zarr_wcoss2.py')),
                     '--date',d.strftime('%Y%m%d'),'--cycle',f'{hh:02d}',
                     '--models',','.join(missing),'--workers',str(a.workers),
                     '--output-root',str(a.root),'--work-root',str(a.work_root),
                     '--protected-cycles-file',str(a.protected_cycles_file),
                     '--retention-days',str(a.retention_days)]
                if a.wgrib2:cmd+=['--wgrib2',a.wgrib2]
                if a.no_publish:cmd+=['--no-publish']
                subprocess.run(cmd,check=True)
            scheduled+=1
        d+=dt.timedelta(days=1)
    if lock:lock.close()
    print(f'Found {scheduled} incomplete cycles. Max-cycles={a.max_cycles}, skipped={a.skip_first}. '+
          ('Executed selected batch.' if a.execute else 'DRY RUN; no changes made.'),flush=True)

if __name__=='__main__':main()
