#!/usr/bin/env python3
"""Check/repair published Analysis Zarr readability on the configured RZDM host.

Use --apply only when the site is intended to serve these verification data.
Touches ONLY analysis_*.zarr directories, their regular files, and the analysis
parent directory. Does not change any other data tree or model catalog.
"""
from __future__ import annotations
import argparse
import re
import shlex
import subprocess

REMOTE = r'''
import os, re, stat, sys
from pathlib import Path
root = Path(sys.argv[1])
apply = sys.argv[2] == 'apply'
if not root.is_absolute() or root.name != 'data' or root.is_symlink() or not root.is_dir():
    raise SystemExit('Unsafe remote data directory')
analysis = root / 'analysis'
if not analysis.is_dir() or analysis.is_symlink():
    raise SystemExit('Analysis directory absent or unsafe')
match = re.compile(r'^analysis_(?:mrms_refc|mrms_prate|mrms_apcp|urma|rap_surface|rap_pressure)_\d{10}\.zarr$')
counts = {'stores': 0, 'directories_needing_fix': 0, 'files_needing_fix': 0, 'errors': 0}
shown = 0

def inspect(path, expected, typ):
    global shown
    mode = stat.S_IMODE(path.lstat().st_mode)
    if mode == expected: return
    counts['directories_needing_fix' if typ == 'dir' else 'files_needing_fix'] += 1
    if shown < 12:
        print(f'{"FIX" if apply else "WOULD FIX"}: {mode:04o} -> {expected:04o} {path}', flush=True)
        shown += 1
    if apply:
        path.chmod(expected)

inspect(analysis, 0o755, 'dir')
for store in sorted(analysis.iterdir()):
    if not match.fullmatch(store.name):continue
    if store.is_symlink() or not store.is_dir():
        counts['errors'] += 1
        print('UNSAFE store skipped:', store, flush=True)
        continue
    counts['stores'] += 1
    inspect(store, 0o755, 'dir')
    for parent, dirs, files in os.walk(store, topdown=True, followlinks=False):
        p = Path(parent)
        for d in dirs[:]:
            child = p / d
            if child.is_symlink():
                counts['errors'] += 1
                print('UNSAFE directory symlink skipped:', child, flush=True)
                dirs.remove(d)
                continue
            inspect(child, 0o755, 'dir')
        for f in files:
            child = p / f
            if child.is_symlink() or not child.is_file():
                counts['errors'] += 1
                print('UNSAFE file skipped:', child, flush=True)
                continue
            inspect(child, 0o644, 'file')
print('ANALYSIS ACCESS', 'APPLIED' if apply else 'CHECK', counts, flush=True)
if counts['errors']:
    raise SystemExit(1)
'''


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--publish-host', default='thultquist@rzdm.ncep.noaa.gov')
    ap.add_argument('--publish-dir', default='/home/people/emc/www/htdocs/users/verification_restricted/test/trh/dynamic_viewer/data')
    ap.add_argument('--apply', action='store_true', help='Change modes to web-readable 755/644; default only reports')
    args = ap.parse_args()
    if not re.fullmatch(r'[\w.-]+@[\w.-]+', args.publish_host):
        ap.error('Unexpected host')
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', args.publish_dir) or not args.publish_dir.endswith('/data'):
        ap.error('Unexpected web data directory')
    cmd = 'python3 - ' + shlex.quote(args.publish_dir) + (' apply' if args.apply else ' check')
    subprocess.run(['ssh', args.publish_host, cmd], input=REMOTE, text=True, check=True)

if __name__ == '__main__':
    main()
