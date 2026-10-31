#!/usr/bin/env python3
"""Create or extend preserved OMD EVU model-evaluation case studies.

Dry-run by default. Runs the existing model, verification, alignment and
observation processing commands under both cron locks. Requires no new Python
packages beyond the deployed converter's standard-library requirements.
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
import tempfile
from contextlib import ExitStack

from retention_policy import MODELS, load_policy

UTC = dt.timezone.utc
FORMAT = '%Y%m%d%H'
PER_CYCLE_LIMIT = 84  # RRFS maximum; other model limits remain model-specific.
MAX_CYCLES = 124       # 31 days of 6-hour initialization cycles
MAX_WINDOW_HOURS = 168  # Native verification and observation backfill chunk size.


def parse_cycle(value: str) -> dt.datetime:
    if not isinstance(value, str) or len(value) != 10 or not value.isdigit():
        raise ValueError('Cycle must be YYYYMMDDHH in UTC, with HH=00,06,12,18')
    try:
        timestamp = dt.datetime.strptime(value, FORMAT).replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError('Invalid UTC date/cycle: ' + value) from exc
    if timestamp.hour not in (0, 6, 12, 18):
        raise ValueError('Cycle must initialize at 00, 06, 12, or 18 UTC')
    return timestamp


def cycles_between(begin: dt.datetime, end: dt.datetime) -> list[dt.datetime]:
    if begin > end or (end-begin) > dt.timedelta(hours=6*(MAX_CYCLES-1)):
        raise ValueError('Cycle range must be ascending and contain no more than 124 cycles')
    result = []
    current = begin
    while current <= end:
        result.append(current)
        current += dt.timedelta(hours=6)
    return result


def chunk_hours(begin: dt.datetime, end: dt.datetime):
    """Split inclusive hourly valid-time range into API-safe 168-hour batches."""
    current = begin
    while current <= end:
        last = min(end, current+dt.timedelta(hours=MAX_WINDOW_HOURS-1))
        yield current, last
        current = last+dt.timedelta(hours=1)


def model_ids(value: str) -> tuple[str, ...]:
    selected = MODELS if value == 'all' else tuple(dict.fromkeys(x.strip() for x in value.split(',')))
    if not selected or any(x not in MODELS for x in selected):
        raise ValueError('Invalid --models; use all or comma-separated model IDs')
    return tuple(selected)


def updated_policy(data: dict, cycles: list[dt.datetime], models: tuple[str, ...]) -> dict:
    """Preserve every old pin/rule and merge model coverage without duplicates."""
    if not isinstance(data, dict) or data.get('schema') != 'omd-evu-protected-cycles-1':
        raise ValueError('Unexpected protected-cycles schema')
    current = data.get('protected_cycles')
    if not isinstance(current, list):
        raise ValueError('Missing protected_cycles array')
    target = {c.strftime('%Y%m%d_%H'): c for c in cycles}
    merged = []
    found = set()
    for entry in current:
        if isinstance(entry, str):
            key = entry
            current_models = set(MODELS)
        else:
            key = entry['date'] + '_' + entry['cycle']
            current_models = set(entry.get('models', MODELS))
        if key in target:
            found.add(key)
            combined = current_models | set(models)
            if isinstance(entry, str) or combined == set(MODELS):
                merged.append(entry if isinstance(entry, str) or combined == current_models else key)
            elif combined == current_models:
                merged.append(entry)  # Preserve existing model ordering/idempotence.
            else:
                merged.append({**entry, 'models': [x for x in MODELS if x in combined]})
        else:
            merged.append(entry)
    for key in sorted(target):
        if key in found:
            continue
        if set(models) == set(MODELS):
            merged.append(key)
        else:
            date, hour = key.split('_')
            merged.append({'date': date, 'cycle': hour, 'models': list(models)})
    return {**data, 'protected_cycles': merged}


def save_protection(path: Path, modified: dict):
    """Back up and atomically replace only a validated operational JSON file."""
    if path.is_symlink() or not path.is_file():
        raise ValueError('Operational protected_cycles.json must already exist (not a symlink)')
    original = path.read_bytes()
    if json.loads(original) == modified:
        return False
    # Timestamped backups preserve the immediately preceding operational list
    # on every edit, not merely the first invocation of the case utility.
    backup = path.with_name(path.name + '.case-backup-' +
        dt.datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ'))
    fd, temporary = tempfile.mkstemp(prefix='.protected-backup-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, backup)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    fd, temporary = tempfile.mkstemp(prefix='.protected-case-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(modified, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    load_policy(path)  # Check updated file before any model/analysis operations.
    return True


def utc_now():
    return dt.datetime.now(UTC)


def command(args: list[str], *, dry_run: bool, env: dict | None = None) -> int:
    print('  $ ' + ' '.join(args), flush=True)
    if dry_run:
        return 0
    return subprocess.run(args, check=False, env=env).returncode


def collect_observations(args, start, end, python, script_path):
    secrets = args.secrets_file
    st = secrets.stat()
    if secrets.is_symlink() or not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o600:
        raise ValueError(f'Observation secret file must be a regular mode-0600 file: {secrets}')
    if not os.access(secrets, os.R_OK):
        raise ValueError('Cannot read observation secrets')
    obs = [python, str(script_path), '--start', start.strftime(FORMAT), '--end', end.strftime(FORMAT),
           '--output-root', str(args.output_root), '--publish-host', args.publish_host,
           '--publish-dir', args.publish_dir]
    if not args.no_publish:
        obs += ['--publish', '--rights-confirmed']
    # Trusted on-disk shell config is sourced inside a child; never print tokens.
    cmd = ['bash', '-c', 'set -e; set -a; source "$1"; shift; exec "$@"', 'case-secrets',
           str(secrets), *obs]
    print(f'  Observation valid hours {start:%Y%m%d%H}..{end:%Y%m%d%H} (private credentials loaded locally)', flush=True)
    return command(cmd, dry_run=False)


def execute(args, cycles, selected, now=None):
    now = now or utc_now()
    directory = Path(__file__).resolve().parent
    converter = directory/'multi_model_to_zarr_wcoss2.py'
    verifier = directory/'verification_pipeline.py'  # executed through converter's --verification-only
    aligner = directory/'aligned_analysis.py'
    collector = directory/'ptype_observations.py'
    spc_collector = directory/'spc_lsr.py'
    needed = [converter,verifier,aligner] + ([] if args.skip_observations else [collector,spc_collector])
    if any(not f.is_file() for f in needed):
        raise ValueError('Missing converter component; deploy the complete package')
    protected = args.protected_cycles_file
    if args.execute or protected.exists() or protected.is_symlink():
        if protected.is_symlink() or not protected.is_file():
            raise ValueError('Missing or unsafe operational protected_cycles.json')
        load_policy(protected)  # No changes or network work on an invalid whitelist.
        policy_data = json.loads(protected.read_text(encoding='utf-8'))
    else:
        # A dry-run plan can be shown before first-time site setup.
        policy_data = {'schema':'omd-evu-protected-cycles-1','protected_cycles':[], 'protected_analysis':[]}
    future = [c for c in cycles if c>now]
    if future:
        raise ValueError('Case cycle cannot be in the future: ' + future[0].strftime(FORMAT))
    changed = updated_policy(policy_data, cycles, selected)
    start = cycles[0]
    end = cycles[-1] + dt.timedelta(hours=PER_CYCLE_LIMIT)
    latest_finished = (now - dt.timedelta(minutes=31)).replace(minute=0,second=0,microsecond=0)
    available_end = min(end, latest_finished)
    print(f'Case: {cycles[0]:%Y%m%d%H}..{cycles[-1]:%Y%m%d%H} UTC; '
          f'{len(cycles)} cycle(s); models: {",".join(selected)}')
    print(f'Verification valid-time coverage: {start:%Y%m%d%H}..{end:%Y%m%d%H} UTC '
          f'(available through {available_end:%Y%m%d%H} at run time)')
    print(f'Mode: {args.mode}; publish: {not args.no_publish}; '
          f'analyses: {not args.skip_analysis}; observations: {not args.skip_observations}')
    print(f'Protected cycles: {len(changed["protected_cycles"])} total after merge '
          f'(previously {len(policy_data["protected_cycles"])})')
    if end > available_end:
        print('NOTE: Future/incomplete valid times will need a subsequent --execute run.')
    if not args.execute:
        print('DRY RUN: no files changed, downloads made, or remote transfers performed.')
        return 0
    if not args.skip_observations:
        if not args.secrets_file.is_file() or args.secrets_file.is_symlink():
            raise ValueError('Missing safe ptype_obs_secrets.sh; or use --skip-observations')
        if stat.S_IMODE(args.secrets_file.stat().st_mode) != 0o600:
            raise ValueError('ptype_obs_secrets.sh must have permissions 600')
        if not args.no_publish and not args.rights_confirmed:
            raise ValueError('Publishing Synoptic/mPING reports requires --rights-confirmed')
    args.output_root.mkdir(parents=True, exist_ok=True)
    args.work_root.mkdir(parents=True, exist_ok=True)
    errors = []
    # Lock BOTH operational workflows so case publication cannot race cron.
    # Acquiring model lock first cannot deadlock because neither cron takes both.
    with ExitStack() as stack:
        for name in ('.model_incremental.lock','.multisource_cron.lock'):
            lock = stack.enter_context(open(directory/name,'a+'))
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(f'Cron still running ({name}); retry when it finishes or pause cron') from exc
        if save_protection(protected, changed):
            print(f'Updated protected cycles: {protected} (timestamped backup saved alongside the file)')
        else:
            print('Case cycles already protected; existing whitelist unchanged')
        common = ['--output-root',str(args.output_root), '--work-root',str(args.work_root),
                  '--protected-cycles-file',str(protected), '--publish-host',args.publish_host,
                  '--publish-dir',args.publish_dir, '--no-local-cleanup']
        if args.wgrib2: common += ['--wgrib2',args.wgrib2]
        for cyc in cycles:
            date, hour = cyc.strftime('%Y%m%d'), cyc.strftime('%H')
            print(f'\n=== Forecast cycle {date} {hour} UTC ===',flush=True)
            task = [args.python,str(converter),'--date',date,'--cycle',hour,'--models',','.join(selected),
                    '--workers',str(args.workers),'--no-remote-cleanup',*common,'--no-publish']
            if args.mode == 'all':
                task += ['--overwrite-hours']
            else:
                task += ['--incremental', '--lookahead-hours','84','--max-hour-jobs','1000','--force-missing-check']
            status = command(task,dry_run=False)
            if status:
                errors.append(f'Model processing {date} {hour} returned {status}')
            if not args.no_publish:
                status = command([args.python,str(converter),'--date',date,'--cycle',hour,
                                 '--models',','.join(selected),'--publish-only','--no-remote-cleanup',*common],
                                 dry_run=False)
                if status: errors.append(f'Model publishing {date} {hour} returned {status}')
        if not args.skip_analysis and available_end >= start:
            print('\n=== Native verification / analysis ===',flush=True)
            for window_start,window_end in chunk_hours(start, available_end):
                job = [args.python,str(converter),'--verification-only',*common,
                       '--source','all','--start-hour',window_start.strftime(FORMAT),
                       '--end-hour',window_end.strftime(FORMAT)]
                if args.mode == 'all':job += ['--force']
                if args.no_publish:job+=['--no-publish']
                status=command(job,dry_run=False)
                if status:errors.append(f'Analysis {window_start:%Y%m%d%H}..{window_end:%Y%m%d%H} returned {status}')
            print('\n=== Align analysis to forecast grid ===',flush=True)
            for window_start,window_end in chunk_hours(start,available_end):
                # Aligner has a narrower CLI than the main converter.
                job=[args.python,str(aligner),'--output-root',str(args.output_root),
                     '--work-root',str(args.work_root),'--protected-cycles-file',str(protected),
                     '--publish-host',args.publish_host,'--publish-dir',args.publish_dir,
                     '--since',window_start.strftime(FORMAT),'--until',window_end.strftime(FORMAT),
                     '--max-new-hours',str(args.max_aligned_stores),
                     '--workers',str(getattr(args,'aligned_workers',2))]
                if args.no_publish:job+=['--no-publish']
                status=command(job,dry_run=False)
                if status:errors.append(f'Aligned analysis {window_start:%Y%m%d%H}..{window_end:%Y%m%d%H} returned {status}')
        if not args.skip_observations and available_end >= start:
            print('\n=== Synoptic METAR + mPING observations ===',flush=True)
            for window_start,window_end in chunk_hours(start,available_end):
                status=collect_observations(args,window_start,window_end,args.python,collector)
                if status:errors.append(f'Observations {window_start:%Y%m%d%H}..{window_end:%Y%m%d%H} returned {status}')
            print('\n=== SPC preliminary storm reports ===',flush=True)
            first_day=(start-dt.timedelta(hours=12)).strftime('%y%m%d')
            last_day=(available_end-dt.timedelta(hours=12)).strftime('%y%m%d')
            first_spc_day=dt.datetime.strptime(first_day,'%y%m%d').replace(tzinfo=dt.timezone.utc)
            last_spc_day=dt.datetime.strptime(last_day,'%y%m%d').replace(tzinfo=dt.timezone.utc)
            while first_spc_day<=last_spc_day:
                last_batch=min(last_spc_day,first_spc_day+dt.timedelta(days=30))
                spc_job=[args.python,str(spc_collector),
                         '--start-day',first_spc_day.strftime('%y%m%d'),
                         '--end-day',last_batch.strftime('%y%m%d'),
                         '--output-root',str(args.output_root),'--publish-host',args.publish_host,
                         '--publish-dir',args.publish_dir]
                if not args.no_publish:spc_job+=['--publish']
                status=command(spc_job,dry_run=False)
                if status:errors.append(f'SPC reports {first_spc_day:%y%m%d}..{last_batch:%y%m%d} returned {status}')
                first_spc_day=last_batch+dt.timedelta(days=1)
    if errors:
        print('\nCASE FINISHED WITH ERRORS:',flush=True)
        for error in errors: print('  '+error,flush=True)
        return 1
    print('\nCASE PASS COMPLETE: existing and newly retrieved data retained and publication requested as configured.',flush=True)
    return 0


def main(argv=None):
    directory=Path(__file__).resolve().parent
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    mutually=parser.add_mutually_exclusive_group(required=True)
    mutually.add_argument('--cycle',help='Single model initialization YYYYMMDDHH UTC')
    mutually.add_argument('--start-cycle',help='First initialization YYYYMMDDHH UTC (requires --end-cycle)')
    parser.add_argument('--end-cycle',help='Last initialization YYYYMMDDHH UTC (inclusive, every 6 hours)')
    parser.add_argument('--models',default='all',help='all or comma-separated RRFS, HRRR, REFS mean/member IDs')
    parser.add_argument('--mode',choices=['missing','all'],default='missing',
                        help='missing recovers absent fields; all overwrites existing model/analysis records')
    parser.add_argument('--execute',action='store_true',help='Acquire both locks, protect, process and publish; otherwise preview only')
    parser.add_argument('--no-publish',action='store_true',help='Store data locally only')
    parser.add_argument('--skip-analysis',action='store_true',help='Do not collect/regrid verification analyses')
    parser.add_argument('--skip-observations',action='store_true',help='Do not retrieve Synoptic or mPING reports')
    parser.add_argument('--rights-confirmed',action='store_true',help='Confirm authority to publish Synoptic and mPING reports to restricted NOAA viewer')
    parser.add_argument('--workers',type=int,default=12,help='Model worker processes, 1–20 (default 12)')
    parser.add_argument('--max-aligned-stores',type=int,default=2500,help='Maximum updated alignment stores per 168-hour batch')
    parser.add_argument('--aligned-workers',type=int,default=2,help='Simultaneous analysis-alignment processes, 1–4 (default 2)')
    parser.add_argument('--output-root',type=Path,default=directory/'zarr')
    parser.add_argument('--work-root',type=Path,default=directory/'work')
    parser.add_argument('--protected-cycles-file',type=Path,default=directory/'protected_cycles.json')
    parser.add_argument('--secrets-file',type=Path,default=directory/'ptype_obs_secrets.sh')
    parser.add_argument('--publish-host',default='thultquist@rzdm.ncep.noaa.gov')
    parser.add_argument('--publish-dir',default='/home/people/emc/www/htdocs/users/verification_restricted/test/trh/dynamic_viewer/data')
    parser.add_argument('--wgrib2',default=None,help='Explicit installed wgrib2 executable (if needed)')
    parser.add_argument('--python',default=sys.executable,help='Python executable with WCOSS2 runtime modules loaded')
    args=parser.parse_args(argv)
    try:
        if args.cycle:
            if args.end_cycle: parser.error('--end-cycle is only valid with --start-cycle')
            first=last=parse_cycle(args.cycle)
        else:
            if not args.end_cycle: parser.error('--start-cycle requires --end-cycle')
            first,last=parse_cycle(args.start_cycle),parse_cycle(args.end_cycle)
        cycles=cycles_between(first,last)
        selected=model_ids(args.models)
        if not 1 <= args.workers <= 20:parser.error('--workers must be in 1–20')
        if not 1 <= args.max_aligned_stores <= 5000:parser.error('--max-aligned-stores must be 1–5000')
        if not 1 <= args.aligned_workers <= 4:parser.error('--aligned-workers must be in 1–4')
        args.output_root=args.output_root.expanduser().resolve()
        args.work_root=args.work_root.expanduser().resolve()
        args.protected_cycles_file=args.protected_cycles_file.expanduser().absolute()
        args.secrets_file=args.secrets_file.expanduser().absolute()
        return execute(args,cycles,selected)
    except (ValueError,RuntimeError,OSError) as exc:
        parser.error(str(exc))


if __name__=='__main__':
    sys.exit(main())
