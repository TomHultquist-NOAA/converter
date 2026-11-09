#!/usr/bin/env python3
"""Prepare existing sounding stores for a change in horizontal sampling.

Dry run by default. With --apply, move only incompatible sounding stores into
an operational backup directory on the same filesystem. The next profile poll
can then create new stores at the requested resolution. No remote files, model
stores, or protected-cycle settings are deleted by this command.

Run only while incremental and verification cron processes are idle.
"""
from __future__ import annotations
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import sys

import sounding_profiles as sp


def inspect(root: Path, model_stride: int, rap_stride: int):
    """Only regular, recognized profile stores are eligible for migration."""
    result=[]
    for store in sorted(root.glob('sounding_*.zarr')):
        match=sp.NAME_RE.fullmatch(store.name)
        if match is None or store.is_symlink() or not store.is_dir():
            continue
        attrs=store/'.zattrs'
        if attrs.is_symlink() or not attrs.is_file():
            raise ValueError(f'Unrecognized or incomplete profile metadata: {store}')
        cfg=json.loads(attrs.read_text(encoding='utf-8'))
        stride=cfg.get('grid_stride')
        if type(stride) is not int or not 1 <= stride <= 64:
            raise ValueError(f'Unsafe profile grid stride in {attrs}')
        target=rap_stride if match.group(1)=='rap_analysis' else model_stride
        if stride!=target:
            result.append((store,stride,target))
    return result


def migrate(root: Path, work: Path, model_stride: int=4, rap_stride: int=1,
            apply: bool=False, now=None, lock_directory=None):
    root=root.resolve(strict=True)
    work=work.resolve(strict=True)
    if root==work or root in work.parents or work in root.parents:
        raise ValueError('Output and work roots must be separate directories')
    if not 1<=model_stride<=64 or not 1<=rap_stride<=64:
        raise ValueError('Grid strides must be 1..64')
    lock_file=(lock_directory or Path(__file__).resolve().parent)/'.model_incremental.lock'
    with open(lock_file,'a+',encoding='utf-8') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Incremental model cron is active; migration refused') from exc
        plans=inspect(root,model_stride,rap_stride)
        for store,old,new in plans:
            print(f'{store.name}: grid stride {old} -> {new}',flush=True)
        if not apply:
            print(f'DRY RUN: {len(plans)} stores would be archived. No changes made.',flush=True)
            return plans,None
        if not plans:
            print('No incompatible sounding stores found.',flush=True)
            return plans,None
        stamp=(now or dt.datetime.now(dt.timezone.utc)).strftime('%Y%m%dT%H%M%SZ')
        # Existing .published snapshots are intentionally left in place: the
        # delta publisher then removes obsolete remote chunks as it installs
        # the newly sampled profile under the original store name.
        backup=work/'profile_grid_backups'/stamp
        if backup.exists():
            raise FileExistsError(f'Backup directory already exists: {backup}')
        backup.mkdir(parents=True,exist_ok=False)
        # Check every source before any move; no partially migrated batch due
        # to a different mount point in work/.
        for store,old,new in plans:
            if store.stat().st_dev!=backup.stat().st_dev:
                raise RuntimeError(f'Output and backup must share filesystem: {store}')
        moved=[]
        try:
            for store,old,new in plans:
                dest=backup/store.name
                os.replace(store,dest)
                moved.append((store,dest))
        except OSError:
            for original,archived in reversed(moved):
                os.replace(archived,original)
            raise
        sp.refresh_catalog(root)
        (backup/'MIGRATION_INFO.json').write_text(json.dumps({
            'schema':1,'created_utc':stamp,'model_grid_stride':model_stride,
            'rap_grid_stride':rap_stride,'stores':[{'name':s.name,'previous_stride':o,
                'new_stride':n} for s,o,n in plans],
            'note':'Local backups; do not delete before validating new profile publication.'
        },indent=2)+'\n',encoding='utf-8')
        print(f'Archived {len(plans)} stores at {backup}',flush=True)
        print('New profiles will populate incrementally; saved cases require explicit backfill.',flush=True)
        return plans,backup


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-root',type=Path,default=Path('zarr'))
    p.add_argument('--work-root',type=Path,default=Path('work'))
    p.add_argument('--grid-stride',type=int,default=4)
    p.add_argument('--rap-stride',type=int,default=1)
    p.add_argument('--apply',action='store_true',help='Archive incompatible local stores so new-resolution profiles can be collected')
    args=p.parse_args(argv)
    try:
        migrate(args.output_root,args.work_root,args.grid_stride,args.rap_stride,args.apply)
    except (OSError,ValueError,RuntimeError,json.JSONDecodeError) as exc:
        p.exit(1,f'Profile-grid migration refused: {exc}\n')
    return 0

if __name__=='__main__':sys.exit(main())
