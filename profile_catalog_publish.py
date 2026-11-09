#!/usr/bin/env python3
"""Serialize shared GFS/regular sounding-catalog publication on the remote host.

Remote publication uses a unique staging filename and an exclusive remote flock.
Merges already published entries because two independent cron jobs may each have
an older snapshot. A cleanup transaction explicitly removes expired store names.
"""
import json
import os
from pathlib import Path
import re
import shlex
import uuid

import publish_transport

PROFILE_NAME = re.compile(r'sounding_(?:(?:rrfs|refs_mean|refs_m00[1-5]|hrrr|gfs_v16|gfs_v17)_\d{8}_(?:00|06|12|18)00|rap_analysis_\d{8}_(?:[01][0-9]|2[0-3])00)\.zarr')
REMOTE_SCRIPT = r'''
import json, os, pathlib, re, shutil, sys, tempfile
root=pathlib.Path(sys.argv[1])
staging=sys.argv[2]
removed=json.loads(sys.argv[3])
pattern=re.compile(r'sounding_(?:(?:rrfs|refs_mean|refs_m00[1-5]|hrrr|gfs_v16|gfs_v17)_\d{8}_(?:00|06|12|18)00|rap_analysis_\d{8}_(?:[01][0-9]|2[0-3])00)\.zarr')
if not root.is_absolute() or root.name!='data' or root.is_symlink() or not root.is_dir():
    raise SystemExit('Refusing unsafe publication directory')
if not re.fullmatch(r'\.catalog_profiles\.json\.[a-f0-9]{32}\.incoming',staging):
    raise SystemExit('Refusing unsafe staging filename')
if not isinstance(removed,list) or any(not isinstance(n,str) or not pattern.fullmatch(n) for n in removed):
    raise SystemExit('Refusing unsafe profile cleanup names')
source=root/staging
target=root/'catalog_profiles.json'
if source.is_symlink() or not source.is_file() or target.is_symlink():
    raise SystemExit('Missing staging catalog or invalid catalog path')
def read(path):
    obj=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(obj,dict) or obj.get('schema')!='omd-evu-sounding-profile-1' or not isinstance(obj.get('datasets'),list):
        raise ValueError('Invalid profile catalog schema')
    for entry in obj['datasets']:
        if not isinstance(entry,dict) or not isinstance(entry.get('path'),str) or not pattern.fullmatch(entry['path']):
            raise ValueError('Unsafe or malformed profile catalog dataset')
    return obj
incoming=read(source)
previous=read(target) if target.is_file() else {'datasets':[]}
# Existing entries not included in this invocation must survive independent
# GFS/regular publication. New entries replace entries for the same store.
merged={item['path']:item for item in previous['datasets']}
for item in incoming['datasets']:
    merged[item['path']]=item
for name in removed:
    merged.pop(name,None)
updated=dict(incoming)
updated['datasets']=[merged[name] for name in sorted(merged)]
fd,tmp=tempfile.mkstemp(prefix='.catalog_profiles.json.commit.',dir=str(root))
try:
    with os.fdopen(fd,'w',encoding='utf-8') as file:
        json.dump(updated,file,separators=(',',':'))
        file.write('\n')
        file.flush()
        os.fsync(file.fileno())
    os.chmod(tmp,0o644)
    os.replace(tmp,target)
finally:
    if os.path.exists(tmp): os.unlink(tmp)
source.unlink()
# Only after publishing the pruned catalog can old stores be deleted.
for name in removed:
    path=root/name
    if path.is_symlink():
        raise SystemExit('Refusing to remove profile symlink: '+name)
    if path.is_dir(): shutil.rmtree(path)
print('PROFILE CATALOG COMMIT entries=%d removed=%d'%(len(merged),len(removed)),flush=True)
'''


def publish(catalog, host, remote_dir, removed=()):
    catalog = Path(catalog)
    removed = sorted(set(removed))
    if any(not isinstance(name,str) or not PROFILE_NAME.fullmatch(name) for name in removed):
        raise ValueError('Invalid removed profile name')
    if not remote_dir.startswith('/') or not remote_dir.rstrip('/').endswith('/data'):
        raise ValueError('Unsafe remote publication directory')
    # UUID prevents accidental collisions among processes, hosts, and retries.
    staging = '.catalog_profiles.json.' + uuid.uuid4().hex + '.incoming'
    publish_transport.run(['scp',str(catalog),host+':'+remote_dir.rstrip('/')+'/'+staging], check=True)
    command = ('cd '+shlex.quote(remote_dir)+' && '
        'flock -w 120 .evu_profiles_catalog.lock python3 - '
        +shlex.quote(remote_dir)+' '+shlex.quote(staging)+' '
        +shlex.quote(json.dumps(removed,separators=(',',':'))))
    # The exclusive lock is held on the remote host, independent of which
    # login node launched the workflow. Never delete the stable lock inode.
    publish_transport.run(['ssh',host,command],input=REMOTE_SCRIPT,text=True,check=True)
