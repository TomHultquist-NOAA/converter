#!/usr/bin/env python3
"""Independent locked, validated, optional weather-event feed publication (Py3.6)."""
from __future__ import print_function
import argparse
import datetime as dt
import fcntl
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

HERE=os.path.dirname(os.path.abspath(__file__))

def timestamp(txt):
    return dt.datetime.strptime(txt[:19],'%Y-%m-%dT%H:%M:%S')

def validate(data, now, max_age_hours, min_models, min_selected, max_cycle_age_hours=36):
    if data.get('schema')!='evu-event-discovery-preview-2':
        raise ValueError('Unexpected event JSON schema')
    count=data.get('counts',{})
    groups=data.get('event_groups',[])
    if (count.get('selected_event_groups',-1)<min_selected or
            len(groups)<min_selected or count.get('candidate_detections',0)<min_selected):
        raise ValueError('Insufficient candidates or groups; keep last published feed')
    status=data.get('coverage',{})
    scanned=[name for name,entry in status.items() if name!='gfs_difference' and entry.get('status')=='scanned']
    if len(scanned)<min_models:
        raise ValueError('Only %d models scanned; require %d' % (len(scanned),min_models))
    try:
        age=(now-timestamp(data['generated_utc'])).total_seconds()/3600
    except (TypeError,KeyError,ValueError):
        raise ValueError('Invalid generated_utc')
    if age < -0.5 or age > max_age_hours:
        raise ValueError('Invalid or stale scanner generation time: %.1f hours' % age)
    recent_groups=0
    for group in groups:
        if not isinstance(group.get('scenes'),list) or not group['scenes']:
            raise ValueError('Empty selected event scenes')
        recency=[]
        for scene in group['scenes']:
            try:recency.append((now-timestamp(scene['init_time'])).total_seconds()/3600)
            except (TypeError,KeyError,ValueError):pass
            if not isinstance(scene.get('source_records'),list) or not scene['source_records']:
                raise ValueError('Scene lacks source catalog records')
            if not scene.get('init_time') or not scene.get('valid_time'):
                raise ValueError('Scene lacks initialization / valid time')
        if any(-1 <= a <= max_cycle_age_hours for a in recency):recent_groups+=1
    if recent_groups < min_selected:
        raise ValueError('Too few selected situations reference recent initialization cycles (%s)' % recent_groups)
    return len(scanned)

def locked_file(path):
    directory=os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(directory):os.makedirs(directory)
    lock=open(path,'w')
    try:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except (OSError,IOError):
        lock.close();return None
    return lock

def transfer(local, target):
    if not target:
        print('Preview only: no publish destination configured; no published files changed.')
        return
    if target.startswith('local:'):
        dest=os.path.abspath(target[6:]);parent=os.path.dirname(dest)
        if not os.path.isdir(parent):raise ValueError('Local publish directory does not exist: '+parent)
        fd,tmp=tempfile.mkstemp(prefix='.weather_events_new_',suffix='.json',dir=parent)
        os.close(fd)
        try:
            shutil.copyfile(local,tmp);os.chmod(tmp,0o644);os.replace(tmp,dest)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
        print('Published atomically to local:',dest)
        return
    if target.startswith('ssh:'):
        spec=target[4:]
        if ':' not in spec:raise ValueError('ssh publish target must be user@host:/absolute/path.json')
        host,remote=spec.split(':',1)
        if (not re.match(r'^[a-zA-Z0-9_.@-]+$',host) or not remote.startswith('/') or
              not remote.endswith('.json') or '\n' in remote):
            raise ValueError('Unsafe remote publish destination')
        tmp=remote+'.incoming.%s' % os.getpid()
        subprocess.check_call(['scp','-q','-B','-o','BatchMode=yes',local,host+':'+tmp],timeout=65)
        try:
            cmd='chmod 644 '+shlex.quote(tmp)+' && mv -f '+shlex.quote(tmp)+' '+shlex.quote(remote)
            subprocess.check_call(['ssh','-o','BatchMode=yes',host,cmd],timeout=35)
        except Exception:
            subprocess.call(['ssh','-o','BatchMode=yes',host,'rm -f '+shlex.quote(tmp)],timeout=20)
            raise
        print('Published atomically via SSH:',host,remote)
        return
    raise ValueError('Publish destination must start local: or ssh:')

def main(argv=None):
    p=argparse.ArgumentParser(description='Nonoverlapping scanner+optional WWA+safe JSON publication')
    p.add_argument('--data-root',required=True)
    p.add_argument('--work-dir',required=True)
    p.add_argument('--publish-target',default='',help='empty = preview only; local:/dir/file.json; ssh:user@host:/dir/file.json')
    p.add_argument('--recent-cycles',type=int,default=1,
                   help='Number of recent initialization cycles screened per model')
    p.add_argument('--max-hour',type=int,default=72)
    p.add_argument('--hour-step',type=int,default=6)
    p.add_argument('--max-frames-per-model',type=int,default=12)
    p.add_argument('--max-events',type=int,default=15)
    p.add_argument('--min-models',type=int,default=5)
    p.add_argument('--min-selected',type=int,default=3)
    p.add_argument('--max-age-hours',type=float,default=2)
    p.add_argument('--max-cycle-age-hours',type=float,default=36)
    p.add_argument('--skip-alerts',action='store_true',help='Skip IEM enrichment; still report alert data not checked')
    p.add_argument('--strict-alerts',action='store_true',help='Do not publish if alert lookup incomplete')
    args=p.parse_args(argv)
    for key in ('recent_cycles','max_hour','hour_step','max_frames_per_model','max_events'):
        if getattr(args,key)<1:
            p.error('--%s must be positive' % key.replace('_','-'))
    work=os.path.abspath(args.work_dir)
    if not os.path.isdir(work):os.makedirs(work)
    handle=locked_file(os.path.join(work,'.weather_events_cron.lock'))
    if not handle:
        print('Weather event scan already running; skipped.');return 0
    try:
        stamp=dt.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
        preview=os.path.join(work,'weather_events_'+stamp+'.json')
        output=os.path.join(work,'weather_events_'+stamp+'_enriched.json')
        cmd=[sys.executable,os.path.join(HERE,'discover_weather_events.py'),
             '--data-root',args.data_root,'--output',preview,'--recent-cycles',str(args.recent_cycles),
             '--max-hour',str(args.max_hour),'--hour-step',str(args.hour_step),
             '--max-frames-per-model',str(args.max_frames_per_model),'--max-events',str(args.max_events)]
        print('Starting independent weather event scan at',stamp,flush=True)
        subprocess.check_call(cmd,timeout=7200)
        with open(preview,encoding='utf-8') as handle_json:data=json.load(handle_json)
        validate(data,dt.datetime.utcnow(),args.max_age_hours,args.min_models,args.min_selected,args.max_cycle_age_hours)
        if args.skip_alerts:
            for group in data.get('event_groups',[]):
                group['alert_context']={'status':'not_checked','matches':[], 'match_count':0,
                                        'notes':['Optional historical watch/warning/advisory lookup disabled.']}
            for item in data.get('candidate_group_summaries',[]):
                item['alert_context']={'status':'not_checked','matches':[], 'match_count':0}
            data['alert_enrichment']={'status':'disabled'}
            from enrich_weather_event_alerts import safe_write
            safe_write(output,data)
        else:
            from enrich_weather_event_alerts import enrich_document, safe_write
            data=enrich_document(data,max_samples=1,sleep=.15)
            if args.strict_alerts and data['alert_enrichment'].get('groups_unavailable'):
                raise ValueError('Alert lookups were incomplete; strict alert policy requires complete context')
            safe_write(output,data)
        with open(output,encoding='utf-8') as test:data=json.load(test)
        validate(data,dt.datetime.utcnow(),args.max_age_hours,args.min_models,args.min_selected,args.max_cycle_age_hours)
        transfer(output,args.publish_target)
        print('Completed event scan; %d selected groups' % data['counts']['selected_event_groups'])
        # Avoid unbounded workdir accumulation; do not touch any other files.
        cleanup=sorted(pathlib.Path(work).glob('weather_events_*.json'), key=lambda p:p.stat().st_mtime)
        for stale in cleanup[:-12]:
            try:stale.unlink()
            except OSError:pass
    finally:
        handle.close()
    return 0
if __name__=='__main__':
    try:sys.exit(main())
    except Exception as err:
        print('Weather event scan/publish failed; previous feed not overwritten:',str(err),file=sys.stderr)
        sys.exit(1)
