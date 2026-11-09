#!/usr/bin/env python3
"""Save a GFS v16/v17 historical cycle and shared verifying observations.

Uses the same operational protected_cycles.json as the existing case utility.
No separate observation copies or GFS observation ingest path are created.
Default is a dry-run plan; pass --execute to process and publish.
"""
from __future__ import annotations
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

from retention_policy import GFS_MODELS, load_policy
from create_case import save_protection

UTC = dt.timezone.utc

def window(start, end, max_hours):
    current = start
    while current <= end:
        finish = min(end, current + dt.timedelta(hours=max_hours-1))
        yield current, finish
        current = finish + dt.timedelta(hours=1)

def merge_policy(policy, day, cycle, models):
    target = day+'_'+cycle
    entries = policy['protected_cycles'][:]
    found = False
    for i, entry in enumerate(entries):
        key = entry if isinstance(entry,str) else entry['date']+'_'+entry['cycle']
        if key != target: continue
        found = True
        # A historical string has legacy F084 verification semantics. Promote
        # to an explicit model selection when saving a GFS case at that cycle.
        if isinstance(entry,str):
            from retention_policy import ALL_PROTECTED_MODELS
            day,hour=key.split('_')
            entries[i]={'date':day,'cycle':hour,'models':list(ALL_PROTECTED_MODELS)}
            break
        current = entry.get('models', list(__import__('retention_policy').ALL_PROTECTED_MODELS))
        entries[i] = {**entry, 'models': list(dict.fromkeys(current + list(models)))}
        break
    if not found:
        entries.append({'date':day, 'cycle':cycle, 'models':list(models)})
    return {**policy, 'protected_cycles':entries}

def run(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--date',required=True, help='Cycle initialization UTC YYYYMMDD')
    p.add_argument('--cycle',required=True,choices=('00','06','12','18'))
    p.add_argument('--models',default='all',help='all, gfs_v16, gfs_v17, or comma-separated pair')
    p.add_argument('--hours',default='0-240',help='3-hourly forecast leads')
    p.add_argument('--execute',action='store_true',help='Without this flag only display the plan')
    p.add_argument('--no-publish',action='store_true')
    p.add_argument('--skip-observations',action='store_true')
    p.add_argument('--skip-spc',action='store_true')
    p.add_argument('--skip-lightning',action='store_true')
    p.add_argument('--skip-profiles',action='store_true')
    p.add_argument('--skip-verification-f000',action='store_true',help='Skip fetching verifying v16 F000 cycles; they remain protected for later backfill')
    p.add_argument('--python',default=sys.executable)
    p.add_argument('--workers',type=int,default=4)
    p.add_argument('--max-hour-jobs',type=int,default=81)
    p.add_argument('--output-root',type=Path,default=Path('./zarr'))
    p.add_argument('--work-root',type=Path,default=Path('./work'))
    p.add_argument('--lock-dir',type=Path,default=None)
    p.add_argument('--protected-cycles-file',type=Path,default=Path(__file__).with_name('protected_cycles.json'))
    p.add_argument('--secrets-file',type=Path,default=Path(__file__).with_name('ptype_obs_secrets.sh'))
    p.add_argument('--publish-host',default='thultquist@rzdm.ncep.noaa.gov')
    p.add_argument('--publish-dir',default='/home/people/emc/www/htdocs/users/verification_restricted/test/trh/dynamic_viewer/data')
    p.add_argument('--wgrib2',default=None)
    p.add_argument('--v17-base-dir',default=None)
    a=p.parse_args(argv)
    try:
        start=dt.datetime.strptime(a.date+a.cycle,'%Y%m%d%H').replace(tzinfo=UTC)
        selected=GFS_MODELS if a.models=='all' else tuple(dict.fromkeys(a.models.split(',')))
        if not selected or any(m not in GFS_MODELS for m in selected):raise ValueError('Invalid GFS models')
        if a.workers<1 or a.max_hour_jobs<1:raise ValueError('workers and max-hour-jobs must be positive')
        from gfs_to_zarr import HOURS
        leads=set()
        for piece in a.hours.split(','):
            if '-' in piece:
                left,right=map(int,piece.split('-',1))
                if left>right or left%3 or right%3:raise ValueError('Invalid lead range')
                leads.update(range(left,right+1,3))
            else:leads.add(int(piece))
        if not leads or not leads.issubset(HOURS):raise ValueError('Unsupported forecast lead')
        last=start+dt.timedelta(hours=max(leads))
        if a.protected_cycles_file.is_symlink():raise ValueError('Protection file cannot be a symlink')
        policy_path=a.protected_cycles_file.resolve(strict=True)
        load_policy(policy_path)
        policy=json.loads(policy_path.read_text())
        new_policy=merge_policy(policy,a.date,a.cycle,selected)
        # Never start a long backfill if required credentials are missing.
        if a.execute and not a.skip_observations:
            secret=a.secrets_file
            if secret.is_symlink() or not secret.is_file() or stat.S_IMODE(secret.stat().st_mode)!=0o600:
                raise ValueError('Observations require a mode-0600 non-symlink ptype_obs_secrets.sh')
    except (OSError,ValueError) as exc:p.error(str(exc))
    now=dt.datetime.now(UTC)
    end=min(last,now-dt.timedelta(hours=1))
    script=Path(__file__).resolve().parent
    base=[a.python,str(script/'gfs_to_zarr.py'),'--date',a.date,'--cycle',a.cycle,
          '--models',','.join(selected),'--hours',a.hours,'--workers',str(a.workers),
          '--max-hour-jobs',str(a.max_hour_jobs),'--output-root',str(a.output_root),
          '--work-root',str(a.work_root),'--protected-cycles-file',str(policy_path),
          '--publish-host',a.publish_host,'--publish-dir',a.publish_dir]
    if a.no_publish:base.append('--no-publish')
    if a.skip_profiles:base.append('--skip-profiles')
    if a.wgrib2:base.extend(['--wgrib2',a.wgrib2])
    if a.v17_base_dir:base.extend(['--v17-base-dir',a.v17_base_dir])
    print('GFS CASE',start.isoformat(),'models',', '.join(selected),'leads',a.hours,flush=True)
    print('Protection:',policy_path,'observation window:',start.isoformat(),'through',last.isoformat(),flush=True)
    print('Plan:', ' '.join(base),flush=True)
    print('Observations:',not a.skip_observations,'SPC:',not a.skip_spc,'GLM:',not a.skip_lightning,flush=True)
    if not a.execute:
        print('DRY RUN — use --execute to modify the protection file, convert, and backfill.')
        return 0
    lock_dir=(a.lock_dir or policy_path.parent).resolve()
    if not lock_dir.is_dir():p.error('Lock directory missing')
    # Shared case admission protocol: pause crons, then take exclusive execution gate.
    # Locks remain open and inherited gates are explicitly closed in child processes.
    with (lock_dir/'.case_startup.lock').open('a+') as startup, (lock_dir/'.case_cron_gate.lock').open('a+') as gate:
        fcntl.flock(startup,fcntl.LOCK_EX)
        fcntl.flock(gate,fcntl.LOCK_EX)
        fcntl.flock(startup,fcntl.LOCK_UN)
        # All external work must finish successfully before protection is saved;
        # retain already protected data when a partial backfill fails.
        errors=[]
        def launch(cmd,env=None):
            print('RUN:', ' '.join(cmd),flush=True)
            result=subprocess.run(cmd,env=env,close_fds=True)
            if result.returncode:errors.append((cmd[1],result.returncode))
            return result.returncode==0
        # Protect BEFORE backfilling, so regular retention cannot delete newly
        # generated data on another host while the case work is in progress.
        if new_policy!=policy:
            save_protection(policy_path,new_policy)
            print('Updated GFS case protection (backup retained)',flush=True)
        launch(base)
        if end>=start and not a.skip_verification_f000:
            # One 0.25-degree v16 F000 per 6-hour valid time. These are
            # protected dependencies, NOT separate user-selected cases.
            for lead in range(0,241,6):
                valid=start+dt.timedelta(hours=lead)
                if valid>now-dt.timedelta(hours=1):break
                if lead==0 and 'gfs_v16' in selected:continue
                verify=[a.python,str(script/'gfs_to_zarr.py'),'--date',valid.strftime('%Y%m%d'),
                        '--cycle',valid.strftime('%H'),'--models','gfs_v16','--hours','0',
                        '--workers','1','--max-hour-jobs','1','--skip-profiles',
                        '--output-root',str(a.output_root),'--work-root',str(a.work_root),
                        '--protected-cycles-file',str(policy_path),
                        '--publish-host',a.publish_host,'--publish-dir',a.publish_dir]
                if a.no_publish:verify.append('--no-publish')
                if a.wgrib2:verify.extend(['--wgrib2',a.wgrib2])
                launch(verify)
        if end>=start:
            if not a.skip_observations:
                for left,right in window(start,end,168):
                    cmd=[a.python,str(script/'ptype_observations.py'),'--start',left.strftime('%Y%m%d%H'),
                         '--end',right.strftime('%Y%m%d%H'),'--output-root',str(a.output_root),
                         '--publish-host',a.publish_host,'--publish-dir',a.publish_dir]
                    if not a.no_publish:cmd+=['--publish','--rights-confirmed']
                    env=os.environ.copy()
                    # Source credentials without exposing them in process arguments or logs.
                    shell=['bash','-c','set -e; set -a; source "$1"; shift; exec "$@"','case-secrets',str(a.secrets_file),*cmd]
                    launch(shell)
            if not a.skip_spc:
                first=(start-dt.timedelta(hours=12)).date()
                final=(end-dt.timedelta(hours=12)).date()
                cmd=[a.python,str(script/'spc_lsr.py'),'--start-day',first.strftime('%y%m%d'),
                     '--end-day',final.strftime('%y%m%d'),'--output-root',str(a.output_root),
                     '--publish-host',a.publish_host,'--publish-dir',a.publish_dir]
                if not a.no_publish:cmd.append('--publish')
                launch(cmd)
            if not a.skip_lightning:
                for left,right in window(start,end,168):
                    cmd=[a.python,str(script/'glm_lightning.py'),'--start',left.strftime('%Y%m%d%H'),
                         '--end',right.strftime('%Y%m%d%H'),'--output-root',str(a.output_root),
                         '--publish-host',a.publish_host,'--publish-dir',a.publish_dir]
                    if not a.no_publish:cmd.append('--publish')
                    launch(cmd)
        if errors:
            print('Case remains protected, but these backfills failed:',errors,file=sys.stderr)
            return 1
    print('GFS case processing completed. Both forecast and shared observation files are protected.')
    return 0

if __name__=='__main__':sys.exit(run())
