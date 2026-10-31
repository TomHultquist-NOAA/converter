#!/usr/bin/env python3
"""Safely revisit historical REFS Mean hours missing reflectivity/categories/derived type.

Only existing, completed stores are eligible. Dry-run unless --execute given.
No source-model data are overwritten; the operational --repair-refs-mean mode
selects missing fields and publishes only changed stores.
"""
import argparse
import fcntl
import datetime as dt
from pathlib import Path
import re
import subprocess
import sys
import zlib

RX=re.compile(r'^refs_mean_(\d{8})_(00|06|12|18)00\.zarr$')
TARGET=('refc','crain','csnow','cicep','cfrzr','weasd','asnow','ptype')

def stats(store):
    path=store/'processed'/'0'
    if not path.exists():return (0,{})
    try:flags=zlib.decompress(path.read_bytes())
    except (OSError,zlib.error):return (0,{})
    done=[i for i in range(1,min(len(flags),61)) if flags[i]==1]
    missing={field:sum(not (store/field/f'{h}.0.0').is_file() for h in done) for field in TARGET}
    return len(done),missing

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('./zarr'))
    p.add_argument('--work-root',type=Path,default=Path('./work'))
    p.add_argument('--protected-cycles-file',type=Path,default=Path('./protected_cycles.json'))
    p.add_argument('--max-cycles',type=int,default=2)
    p.add_argument('--days',type=int,default=7)
    p.add_argument('--workers',type=int,default=2)
    p.add_argument('--skip-first',type=int,default=0,help='Skip this many candidates (for manually continuing after partial/unavailable cycles)')
    p.add_argument('--execute',action='store_true')
    p.add_argument('--no-publish',action='store_true')
    p.add_argument('--wgrib2',default=None)
    a=p.parse_args()
    if a.skip_first<0:p.error('Invalid skip-first')
    if a.max_cycles<1 or a.max_cycles>100 or a.workers<1 or a.workers>12:p.error('Invalid worker/cycle limit')
    now=dt.datetime.now(dt.timezone.utc);limit=now-dt.timedelta(days=a.days)
    if a.execute and not a.protected_cycles_file.is_file():p.error('Protected-cycle configuration missing')
    candidates=[]
    for store in a.root.glob('refs_mean_*.zarr'):
        m=RX.fullmatch(store.name)
        if not m or not store.is_dir():continue
        init=dt.datetime.strptime(''.join(m.groups()),'%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
        if init<limit or init>now:continue
        done,missing=stats(store)
        if done and any(missing.values()):candidates.append((init,store,done,missing))
    # Start with newest incomplete stores (most immediately useful).
    candidates.sort(reverse=True)
    for init,store,done,missing in candidates:
        interesting={field:count for field,count in missing.items() if count}
        print(f'{init:%Y-%m-%d %HZ} processed {done} hours, missing: {interesting}',flush=True)
    if not a.execute:
        print(f'DRY RUN: {len(candidates)} existing stores have missing target fields. No changes.',flush=True);return
    lock=open(Path(__file__).with_name('.multisource_cron.lock'),'a+')
    try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:p.error('Cron/converter already holds .multisource_cron.lock; retry when idle')
    for init,store,done,missing in candidates[a.skip_first:a.skip_first+a.max_cycles]:
        cmd=[sys.executable,str(Path(__file__).with_name('multi_model_to_zarr_wcoss2.py')),
             '--date',init.strftime('%Y%m%d'),'--cycle',init.strftime('%H'),
             '--models','refs_mean','--repair-refs-mean',
             '--workers',str(a.workers),'--output-root',str(a.root),
             '--work-root',str(a.work_root),'--retention-days',str(a.days),
             '--protected-cycles-file',str(a.protected_cycles_file)]
        if a.wgrib2:cmd+=['--wgrib2',a.wgrib2]
        if a.no_publish:cmd+=['--no-publish']
        print('Repairing',store.name,flush=True)
        subprocess.run(cmd,check=True)
    lock.close()
    print(f'Executed {min(max(0,len(candidates)-a.skip_first),a.max_cycles)} repairs. To skip still-unavailable cycles, use --skip-first.',flush=True)

if __name__=='__main__':main()
