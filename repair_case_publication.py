#!/usr/bin/env python3
"""Audit/recover case NOHRSC publication on RZDM; never re-convert GRIB.

Dry-run by default. --publish sends only missing/incomplete case NOHRSC Zarr
stores then atomically installs a merged, remote-backed analysis catalog.
No unvalidated/local-only entries are promoted to the public catalog.
Requires normal WCOSS2 SSH/scp access, no extra Python dependencies.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

import multi_model_to_zarr_wcoss2 as converter
import verification_pipeline as verification
from retention_policy import load_policy

UTC=timezone.utc
FILE_RE=re.compile(r'^analysis_(nohrsc_6h|nohrsc_24h)_(\d{10})\.zarr$')

REMOTE_SNAPSHOT = r'''
import json, pathlib, sys
root=pathlib.Path(sys.argv[1]); cat=root/'catalog_analysis.json'
if root.name!='data' or not root.is_dir() or root.is_symlink():
 raise SystemExit('Unsafe/unavailable RZDM data directory')
existing=[]
for p in (root/'analysis').glob('analysis_*.zarr'):
 if not p.is_symlink() and p.is_dir() and (p/'.conversion.json').is_file():
  try:
   meta=json.loads((p/'.conversion.json').read_text())
   fields=meta.get('fields',[])
   if fields and all((p/f/'0.0.0').is_file() and (p/f/'0.0.0').stat().st_size>0 for f in fields):
    existing.append(p.name)
  except (OSError, ValueError, KeyError, TypeError):pass
try:
 catalog=json.loads(cat.read_text())
 if catalog.get('schema')!='rrfs-zarr-viewer-1':raise ValueError('Schema')
except (OSError, ValueError):catalog={'datasets':[]}
print(json.dumps({'existing':existing,'catalog':catalog}))
'''

def case_targets(catalog,cycle,lead):
    begin=datetime.strptime(cycle,'%Y%m%d%H').replace(tzinfo=UTC)
    end=begin+timedelta(hours=lead)
    targets=[]
    for ds in catalog.get('datasets',[]):
        if ds.get('product') not in ('nohrsc_6h','nohrsc_24h'):continue
        try:t=datetime.fromisoformat(ds['init_time'].replace('Z','+00:00'))
        except (KeyError,ValueError):continue
        if begin<=t<=end:
            path=ds.get('path',''); parts=Path(path).parts
            if len(parts)!=2 or parts[0]!='analysis' or not FILE_RE.fullmatch(parts[1]):
                raise ValueError('Invalid case NOHRSC catalog path')
            targets.append(ds)
    return targets

def merged_catalog(local,remote,existing):
    """Only announce datasets with remotely present stores; preserve remote-only history."""
    entries={}
    for source in (remote.get('datasets',[]),local.get('datasets',[])):
        for ds in source:
            if not isinstance(ds,dict):continue
            path=ds.get('path',''); parts=Path(path).parts
            if len(parts)!=2 or parts[0]!='analysis' or parts[1] not in existing:continue
            if not re.fullmatch(r'analysis_[a-z0-9_]+_\d{10}\.zarr',parts[1]):continue
            if not ds.get('variables'):continue
            entries[path]=ds
    return {'schema':'rrfs-zarr-viewer-1',
      'description':'OMD EVU verification data by UTC valid time',
      'datasets':sorted(entries.values(),key=lambda d:(d.get('init_time',''),d.get('product','')),reverse=True),
      'updated_at':datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}

def snapshot(host,remote_dir):
    args='python3 - '+shlex.quote(remote_dir)
    p=subprocess.run(['ssh',host,args],input=REMOTE_SNAPSHOT,text=True,
                     capture_output=True,check=True)
    return json.loads(p.stdout)

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cycle',required=True,help='Case initialization YYYYMMDDHH')
    p.add_argument('--max-lead',type=int,default=84)
    p.add_argument('--output-root',type=Path,required=True)
    p.add_argument('--work-root',type=Path,required=True)
    p.add_argument('--protected-cycles-file',type=Path,default=Path(__file__).with_name('protected_cycles.json'))
    p.add_argument('--publish-host',default=converter.core.DEFAULT_PUBLISH_HOST)
    p.add_argument('--publish-dir',default=converter.core.DEFAULT_PUBLISH_DIR)
    p.add_argument('--publish',action='store_true',help='Repair missing case stores and update remote catalog')
    p.add_argument('--force-case-stores',action='store_true',help='Reupload all case NOHRSC stores even if remotely present (with --publish)')
    p.add_argument('--no-remote-cleanup',action='store_true',help='Disable remote retention during catalog promotion')
    a=p.parse_args(argv)
    try:datetime.strptime(a.cycle,'%Y%m%d%H')
    except ValueError:p.error('Invalid UTC cycle; use YYYYMMDDHH')
    if not 0<=a.max_lead<=168:p.error('max-lead must be 0..168')
    converter.validate_publisher(a.publish_host,a.publish_dir)
    policy=load_policy(a.protected_cycles_file)
    output=a.output_root.expanduser().resolve();work=a.work_root.expanduser().resolve()
    path=output/'catalog_analysis.json'
    local=json.loads(path.read_text())
    if local.get('schema')!='rrfs-zarr-viewer-1':p.error('Invalid local analysis catalog')
    selected=case_targets(local,a.cycle,a.max_lead)
    if not selected:p.error('No case NOHRSC datasets found locally; backfill the case first')
    remote=snapshot(a.publish_host,a.publish_dir)
    catalog=remote['catalog'];existing=set(remote['existing'])
    needed=[];present=0;cataloged=0
    remote_records={d.get('path') for d in catalog.get('datasets',[]) if isinstance(d,dict)}
    for ds in selected:
        name=Path(ds['path']).name
        if name not in existing:needed.append(ds)
        else:present+=1
        if ds['path'] in remote_records:cataloged+=1
    print(f'Case {a.cycle}: local NOHRSC {len(selected)}, remote stores {present}, remote catalog entries {cataloged}')
    print(f'  Missing remote NOHRSC stores: {len(needed)}')
    for ds in needed:print('   ',ds['path'])
    if not a.publish:
        print('Dry run only. Add --publish to transfer missing case stores and publish a reconciled catalog.')
        return 0
    upload=selected if a.force_case_stores else needed
    for idx,ds in enumerate(upload,1):
        store=output/ds['path']
        if not verification.completed_store(store):
            raise RuntimeError('Local store incomplete: '+str(store))
        print(f'Publishing case store {idx}/{len(upload)}: {ds["path"]}',flush=True)
        converter.publish_one(store,None,work,a.publish_host,a.publish_dir,
                              store_arcname=ds['path'])
        existing.add(store.name)
    if upload:
        # Recheck remote state after transfer; never advertise unavailable data.
        remote=snapshot(a.publish_host,a.publish_dir)
        existing=set(remote['existing'])
        if any(Path(ds['path']).name not in existing for ds in selected):
            raise RuntimeError('A case store was not confirmed on RZDM; catalog unchanged')
    merged=merged_catalog(local,remote['catalog'],existing)
    selected_paths={ds['path'] for ds in selected}
    if not selected_paths <= {d['path'] for d in merged['datasets']}:
        raise RuntimeError('Case analyses missing from merged catalog; nothing published')
    with tempfile.TemporaryDirectory(dir=work) as tmp:
        staged=Path(tmp)/'catalog_analysis.json'
        staged.write_text(json.dumps(merged,separators=(',',':'))+'\n')
        # The usual publisher handles atomic installation. Do not delete old
        # remote analyses unless requested; normal retention can run later.
        verification.publish_catalog(staged,a.publish_host,a.publish_dir,366 if a.no_remote_cleanup else 14,
            cutoff=datetime(1970,1,1,tzinfo=UTC) if a.no_remote_cleanup else None,
            analysis_rules=policy.analysis_rules)
    print(f'Published reconciled analysis catalog ({len(merged["datasets"])} datasets, {len(selected)} case NOHRSC records).')
    print('Rerun the viewer after a hard refresh; the remote catalog can now find the case analyses.')
    return 0

if __name__=='__main__':sys.exit(main())
