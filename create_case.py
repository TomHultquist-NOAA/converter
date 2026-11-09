#!/usr/bin/env python3
"""Create or extend preserved multi-model case studies.

Dry-run by default. Runs model conversion, native and aligned analyses,
full native sounding profiles, and optional observations under both cron locks.
Requires no new Python packages beyond the existing converter.
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
import time
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from typing import Callable
import re
import threading

from retention_policy import MODELS, load_policy
import publish_transport
import perf_monitor

UTC = dt.timezone.utc
FORMAT = '%Y%m%d%H'
PER_CYCLE_LIMIT = 84  # RRFS maximum; other model limits remain model-specific.
MAX_CYCLES = 124       # 31 days of 6-hour initialization cycles
MAX_WINDOW_HOURS = 168  # Native verification and observation backfill chunk size.

# For historical cases, explicitly supplied CLI flags override the local
# incremental configuration.  CASE_* variables can tune a case without
# increasing the load of the recurring cron workflow.  The other names are
# shared with the two cron wrappers.
CASE_WORKER_DEFAULTS = {
    'workers': (18, ('CASE_WORKERS', 'INCREMENTAL_WORKERS', 'WORKERS')),
    'aligned_workers': (3, ('CASE_ALIGNED_WORKERS', 'ALIGNED_WORKERS')),
    'sounding_workers': (3, ('CASE_SOUNDING_WORKERS', 'SOUNDING_PROFILE_HOUR_WORKERS')),
    'stage_workers': (3, ('CASE_STAGE_WORKERS',)),
    'publish_workers': (4, ('CASE_PUBLISH_WORKERS', 'PUBLISH_WORKERS')),
}


def default_converter_config(directory: Path) -> Path:
    """Resolve the shared config, preferring the converter-wide filename."""
    override = os.environ.get('CONVERTER_CONFIG_FILE')
    if override:
        return Path(override).expanduser()
    legacy_override = os.environ.get('INCREMENTAL_CONFIG_FILE')
    if legacy_override:
        return Path(legacy_override).expanduser()
    current = directory / 'converter_config.sh'
    legacy = directory / 'incremental_model_config.sh'
    return current if current.exists() or not legacy.exists() else legacy


def read_worker_config(config_path: Path) -> dict[str, str]:
    """Load shared operational settings in an isolated shell subprocess.

    The operators' configuration can contain shell constructs, so use Bash's
    source semantics rather than an incomplete shell-assignment parser.  Run
    in a child process, never import credentials or apply configuration
    side-effects to the caller, and never echo source-file contents.
    """
    names = tuple(dict.fromkeys([name for _, aliases in CASE_WORKER_DEFAULTS.values()
                                  for name in aliases] + [
        'RETENTION_DAYS', 'CASE_RETENTION_DAYS', 'NO_PUBLISH', 'MODELS',
        'PUBLISH_HOST', 'PUBLISH_DIR', 'PROTECTED_CYCLES_FILE',
        'OUTPUT_ROOT', 'WORK_ROOT', 'PTYPE_OBS_SECRETS_FILE',
        'GLM_ENABLED', 'GLM_CASE_ENABLED', 'GLM_LOOKBACK_HOURS',
        'GLM_WORKERS', 'GLM_PUBLISH', 'EVU_PERF_ENABLED',
        'EVU_PERF_SAMPLE_SECONDS', 'EVU_PERF_RETENTION_DAYS', 'EVU_PERF_LOG_DIR']))
    if not config_path.exists():
        return {name: os.environ[name] for name in names if os.environ.get(name)}
    if not config_path.is_file():
        raise ValueError(f'Worker configuration is not a regular file: {config_path}')
    shell = (
        'set -a; source "$1" >/dev/null || exit 1; shift; '
        'for key in "$@"; do printf "%s=%s\\0" "$key" "${!key-}"; done'
    )
    # Bash printf interprets \0 as a NUL separator.  stdout is deliberately
    # restricted to our allowlist; stderr is not copied into user-facing logs.
    result = subprocess.run(['bash', '-c', shell, 'case-worker-config',
                             str(config_path), *names], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise ValueError(f'Unable to source worker configuration: {config_path}')
    parsed = {}
    for item in result.stdout.split(b'\0'):
        if not item: continue
        key, sep, value = item.partition(b'=')
        if not sep: continue
        name = key.decode('ascii', errors='replace')
        if name in names and value:
            parsed[name] = value.decode('utf-8')
    return parsed


def resolve_worker_settings(args: argparse.Namespace, settings: dict[str, str]) -> dict[str, str]:
    """CLI > CASE_* setting > cron-shared setting > built-in default."""
    sources = {}
    for arg_name, (fallback, aliases) in CASE_WORKER_DEFAULTS.items():
        if getattr(args, arg_name, None) is not None:
            sources[arg_name] = 'CLI'
            continue
        for name in aliases:
            if name in settings:
                raw = settings[name].strip()
                try:
                    value = int(raw)
                except ValueError as exc:
                    raise ValueError(f'Invalid integer {name}={raw!r} in worker configuration') from exc
                setattr(args, arg_name, value)
                sources[arg_name] = name
                break
        else:
            setattr(args, arg_name, fallback)
            sources[arg_name] = 'built-in'
    return sources


def resolve_glm_settings(args: argparse.Namespace, settings: dict[str, str]) -> None:
    """Read optional GLM settings from the same shared converter/case config."""
    def flag(name: str, default: int) -> int:
        raw = str(settings.get(name, default)).strip()
        if raw not in ('0', '1'):
            raise ValueError(f'{name} must be 0 or 1, not {raw!r}')
        return int(raw)

    args.glm_enabled = bool(flag('GLM_ENABLED', 0))
    args.glm_case_enabled = bool(flag('GLM_CASE_ENABLED', 0))
    args.glm_publish = flag('GLM_PUBLISH', 1)
    try:
        args.glm_workers = int(str(settings.get('GLM_WORKERS', '4')).strip())
    except ValueError as exc:
        raise ValueError('GLM_WORKERS must be a positive integer') from exc
    if args.glm_workers < 1:
        raise ValueError('GLM_WORKERS must be a positive integer')



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
    log_path = getattr(_stage_log, 'path', None)
    if log_path is None:
        print('  $ ' + ' '.join(args), flush=True)
        if dry_run:
            return 0
        return subprocess.run(args, check=False, env=env,
                              **_child_case_gate_kwargs()).returncode
    # Keep stdout/stderr from parallel tools separate, retaining full logs
    # even if the subprocess fails. Stage starts/completions remain visible.
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(log_path, flags, 0o600), 'a', encoding='utf-8') as stream:
        stream.write('  $ ' + ' '.join(args) + '\n')
        stream.flush()
        if dry_run:
            return 0
        return subprocess.run(args, check=False, env=env,
                              stdout=stream, stderr=subprocess.STDOUT,
                              **_child_case_gate_kwargs()).returncode


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


def profile_common_args(args, profile_script):
    """Shared profile configuration; no external credentials are passed."""
    common = [args.python, str(profile_script), '--output-root', str(args.output_root),
              '--work-root', str(args.work_root), '--protected-cycles-file',
              str(args.protected_cycles_file), '--publish-host', args.publish_host,
              '--publish-dir', args.publish_dir,
              '--publish-workers', str(getattr(args, 'publish_workers', 3)),
              '--hour-workers', str(getattr(args, 'sounding_workers', 2)),
              '--missing-retry-hours', '0']
    if args.wgrib2:
        common += ['--wgrib2', args.wgrib2]
    if args.no_publish:
        common.append('--no-publish')
    if args.mode == 'all' or getattr(args, 'reprocess_soundings', False):
        common.append('--reprocess-hours')
    return common


def forecast_profile_job(args, profile_script, cyc, selected):
    """All available processed forecast hours, not the utility's four-hour default."""
    return [*profile_common_args(args, profile_script),
            '--date', cyc.strftime('%Y%m%d'), '--cycle', cyc.strftime('%H'),
            '--models', ','.join(selected), '--max-hours-per-model', '85']


def rap_profile_job(args, profile_script, begin, end):
    """One subprocess handles an inclusive batch of native hourly RAP profiles."""
    return [*profile_common_args(args, profile_script),
            '--date', begin.strftime('%Y%m%d'), '--cycle', begin.strftime('%H'),
            '--models', 'rap_analysis', '--start-hour', begin.strftime(FORMAT),
            '--end-hour', end.strftime(FORMAT),
            '--max-hours-per-model', '1']


@dataclass(frozen=True)
class Stage:
    """A subprocess stage with completion dependencies (not just launch order)."""
    name: str
    requires: tuple[str, ...]
    run: Callable[[], int]


# Pass the case execution gate to converter subprocesses. If the case
# coordinator is forcibly terminated while a subprocess is still writing,
# the inherited file description keeps scheduled crons out until it exits.
# Normal subprocess.run calls outside case execution inherit no extra FDs.
_case_gate_fd = None

def _child_case_gate_kwargs():
    return {'pass_fds': (_case_gate_fd,)} if _case_gate_fd is not None else {}

def _forget_case_gate():
    global _case_gate_fd
    _case_gate_fd = None

# Each subprocess receives a separate log when stages run concurrently.
# No SSH credentials, authorization flags, or environment secrets are logged.
_stage_log = threading.local()


def run_stages(stages: list[Stage], max_workers: int, logs_dir: Path | None = None) -> list[str]:
    """Bounded DAG execution; dependents never run after failed prerequisites.

    The scheduler is deliberately process-stage granularity, not per-GRIB task:
    each existing child owns its own conversion/publishing concurrency and its
    catalog's atomic update. Single-writer chains are explicit dependencies.
    """
    if max_workers < 1:
        raise ValueError('Stage workers must be positive')
    ordered = {stage.name: stage for stage in stages}
    if len(ordered) != len(stages):
        raise ValueError('Duplicate workflow stage name')
    seen = set()
    for stage in stages:
        if any(req not in seen for req in stage.requires):
            raise ValueError('Stages must appear after their dependencies: ' + stage.name)
        seen.add(stage.name)
    pending = dict(ordered)
    results = {}
    running = {}
    failures = []

    def invoke(stage, queued_at=None):
        stage_started = time.monotonic()
        if queued_at is not None:
            perf_monitor.record_event('stage_queue', stage=stage.name,
                queue_wait_seconds=round(max(0.0, stage_started - queued_at), 4))
        log_path = None
        if logs_dir is not None:
            logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            log_path = logs_dir/(re.sub(r'[^A-Za-z0-9_.-]', '_', stage.name)+'.log')
        _stage_log.path = log_path
        stage_status = 'failed'
        try:
            perf_monitor.record_event('stage_start', stage='case_stage:' + stage.name)
            status = stage.run()
            stage_status = 'complete' if int(status or 0) == 0 else 'failed'
            return int(status or 0)
        finally:
            elapsed = round(time.monotonic() - stage_started, 4)
            perf_monitor.record_event('stage_end', stage='case_stage:' + stage.name,
                elapsed_seconds=elapsed, status=stage_status)
            perf_monitor.record_event('case_stage_result', stage=stage.name,
                elapsed_seconds=elapsed, status=stage_status)
            _stage_log.path = None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        while pending or running:
            for name,stage in list(pending.items()):
                if any(results.get(dep) not in (None, 0) for dep in stage.requires):
                    failures.append(f'{name}: skipped because a prerequisite failed')
                    results[name] = -1
                    del pending[name]
                    print(f'  SKIP {name}: failed dependency', flush=True)
            for name,stage in list(pending.items()):
                if len(running) >= max_workers:
                    break
                if all(dep in results and results[dep] == 0 for dep in stage.requires):
                    del pending[name]
                    print(f'  START {name}',flush=True)
                    running[pool.submit(invoke, stage, time.monotonic())] = name
            if not running:
                if pending:
                    raise RuntimeError('Workflow has an unresolved dependency')
                break
            finished, _ = wait(tuple(running), return_when=FIRST_COMPLETED)
            for future in finished:
                name = running.pop(future)
                try:
                    status = future.result()
                except Exception as exc:
                    status = 1
                    print(f'  FAIL {name}: {exc}',flush=True)
                results[name] = status
                if status:
                    failures.append(f'{name}: exited with status {status}')
                    print(f'  FAIL {name}: status {status}',flush=True)
                else:
                    print(f'  DONE {name}',flush=True)
    return failures


def collect_job_result(*commands):
    """Run dependent commands in order, stopping before unsafe publication."""
    for job in commands:
        status = command(job, dry_run=False)
        if status:
            return status
    return 0


def run_optional_glm_job(job, warning_callback=None):
    """Run one optional GLM case batch without failing the primary case.

    The GLM collector writes each successful hour atomically and does not write
    a valid-looking file for incomplete hours. Returning success here allows
    later GLM batches to run; a subsequent create_case.py execution retries
    any hours that still lack a complete local GeoJSON file.
    """
    start = end = 'unknown'
    try:
        if '--start' in job:
            start = job[job.index('--start') + 1]
        if '--end' in job:
            end = job[job.index('--end') + 1]
        status = command(job, dry_run=False)
        if not status:
            return 0
        detail = f'exited with status {status}'
    except Exception as exc:
        detail = f'could not complete ({type(exc).__name__}: {exc})'

    warning = (f'GLM case batch {start}..{end} {detail}. This optional observation '
               'batch will not fail the case; missing/incomplete hours will be '
               'retried the next time this case is executed. Check the GLM stage '
               'log for details when parallel stage logging is enabled.')
    print('WARNING: ' + warning, flush=True)
    if warning_callback is not None:
        warning_callback(warning)
    return 0


def declared_cron_base(script_directory: Path) -> Path | None:
    """Read the installed wrappers' canonical lock root without executing them.

    Cron shells take locks under BASE_DIR.  Importantly, BASE_DIR may not be
    the directory containing this case script when a release is staged or
    launched through a symlink.  Never silently take an unrelated set of
    lock files in that situation.
    """
    declared = []
    for wrapper in ('run_model_incremental_cron.sh', 'run_multimodel_cron.sh'):
        source = script_directory / wrapper
        if not source.is_file():
            raise ValueError(f'Missing cron lock participant {wrapper}; install the complete converter package')
        match = re.search(r'^BASE_DIR=["\'](/[^"\']+)["\']\s*$',
                          source.read_text(encoding='utf-8'), flags=re.MULTILINE)
        if match is None:
            raise ValueError(f'Cannot confirm canonical BASE_DIR in {wrapper}; refusing uncoordinated case work')
        declared.append(Path(match.group(1)).resolve())
    if len(set(declared)) != 1:
        raise ValueError('Cron wrappers disagree about BASE_DIR; fix their installation before running a case')
    return declared[0]


def case_lock_directory(args, script_directory: Path) -> Path:
    """Select the same lock inode directory used by the active cron wrappers.

    The authoritative default is the directory containing the operational
    protected-cycle whitelist, *not* the case script directory.  Users with
    customized layouts can explicitly pass --lock-dir.  Fail closed if the
    installed cron wrappers point at an existing different base directory.
    """
    configured = getattr(args, 'lock_dir', None)
    lock_dir = (Path(configured) if configured is not None else
                Path(args.protected_cycles_file).parent).expanduser().resolve()
    expected = declared_cron_base(script_directory)
    if expected != lock_dir and expected.is_dir():
        raise ValueError(
            'Unsafe case/cron lock mismatch: case would lock '
            f'{lock_dir}, but cron wrappers configure BASE_DIR={expected}. '
            'Use the operational protected-cycles file and --lock-dir pointing '
            'to the cron BASE_DIR, or correct the cron wrapper installation. '
            'No processing has started.')
    if not lock_dir.is_dir():
        raise ValueError(f'Case/cron lock directory does not exist: {lock_dir}')
    return lock_dir


def execute(args, cycles, selected, now=None):
    now = now or utc_now()
    directory = Path(__file__).resolve().parent
    converter = directory/'multi_model_to_zarr_wcoss2.py'
    verifier = directory/'verification_pipeline.py'  # executed through converter's --verification-only
    aligner = directory/'aligned_analysis.py'
    collector = directory/'ptype_observations.py'
    spc_collector = directory/'spc_lsr.py'
    glm_collector = directory/'glm_lightning.py'
    profile_script = directory/'sounding_profiles.py'
    shear_only = getattr(args,'lowlevel_shear_only',False)
    if shear_only:
        args.skip_analysis=True
        args.skip_observations=True
        args.skip_glm=True
    skip_soundings = shear_only or getattr(args, 'skip_soundings', False)
    run_glm = (bool(getattr(args, 'glm_enabled', False)) and
               bool(getattr(args, 'glm_case_enabled', False)) and
               not getattr(args, 'skip_glm', False))
    needed = [converter,verifier,aligner] + ([] if args.skip_observations else [collector,spc_collector])
    if run_glm:
        needed.append(glm_collector)
    if not skip_soundings:
        needed.append(profile_script)
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
    print(f'Mode: {"lowlevel-shear-only" if shear_only else args.mode}; publish: {not args.no_publish}; '
          f'analyses: {not args.skip_analysis}; soundings: {not skip_soundings}; '
          f'observations: {not args.skip_observations}; GLM case backfill: {run_glm}; '
          f'publication workers: {getattr(args, "publish_workers", 3)}; '
          f'stage workers: {getattr(args, "stage_workers", 1)}; '
          f'sounding-hour workers: {getattr(args, "sounding_workers", 2)}')
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
    glm_warnings = []
    glm_warning_lock = threading.Lock()

    def record_glm_warning(message):
        with glm_warning_lock:
            glm_warnings.append(message)

    lock_dir = case_lock_directory(args, directory)
    print(f'Case/cron lock directory: {lock_dir} (cron participant BASE_DIR); '
          f'case script directory: {directory}', flush=True)
    # Acquire a priority/admission gate first, so crons launched while this
    # case is WAITING skip rather than repeatedly getting ahead of the case.
    # The two cron wrappers hold short-lived shared admission locks and a
    # long-lived shared execution lock. Our exclusive execution lock waits for
    # BOTH cron types to finish. Retain the legacy writer locks as a bridge
    # for an older already-running cron during rolling deployment.
    # Ordering is the same for every participant: admission -> execution ->
    # per-writer lock. Never remove a lock file to recover a stale lock:
    # kernel flock locks release automatically when owning processes exit.
    with ExitStack() as stack:
        for name, waiting_message in (
            ('.case_startup.lock', 'another case to finish'),
            ('.case_cron_gate.lock', 'active cron jobs to finish'),
            ('.model_incremental.lock', 'an earlier model cron to finish'),
            ('.multisource_cron.lock', 'an earlier analysis cron to finish'),
        ):
            lock = stack.enter_context(open(lock_dir/name, 'a+'))
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(f'Waiting for {waiting_message} ({name}); cron does not need to be disabled.',
                      flush=True)
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if name == '.case_cron_gate.lock':
                gate_fd = lock.fileno()
        # Keep the gate locked through any subprocess still processing even
        # if this coordinator is terminated with SIGKILL.
        global _case_gate_fd
        _case_gate_fd = None
        # Locate the live execution-gate descriptor within the acquired locks.
        # It belongs to the same ExitStack and closes automatically at exit.
        _case_gate_fd = gate_fd
        stack.callback(_forget_case_gate)
        print('Case processing locks acquired; scheduled cron runs will skip until the case finishes.',
              flush=True)
        # The protection file might have been updated by another case while
        # this one was waiting. Re-read and merge under the exclusive gate.
        load_policy(protected)
        live_policy = json.loads(protected.read_text(encoding='utf-8'))
        changed = updated_policy(live_policy, cycles, selected)
        # One private control socket for the entire manual case. Each child
        # converter/observation publisher inherits this path, so nearby
        # sequential SSH/SCP stages can reuse the connection as well as
        # concurrent stores within each stage. Cron workflows are unaffected.
        if not args.no_publish:
            stack.enter_context(publish_transport.session())
        if save_protection(protected, changed):
            print(f'Updated protected cycles: {protected} (timestamped backup saved alongside the file)')
        else:
            print('Case cycles already protected; existing whitelist unchanged')
        common = ['--publish-workers',str(getattr(args,'publish_workers',3)),
                  '--output-root',str(args.output_root), '--work-root',str(args.work_root),
                  '--protected-cycles-file',str(protected), '--publish-host',args.publish_host,
                  '--publish-dir',args.publish_dir, '--no-local-cleanup',
                  '--retention-days',str(getattr(args,'retention_days',7) or 7)]
        if args.wgrib2: common += ['--wgrib2',args.wgrib2]
        stages = []
        prev_forecast = None
        prev_profile = None
        forecast_selected=tuple(m for m in selected if m=='rrfs' or
                                 m in ('refs_m001','refs_m002','refs_m003','refs_m004','refs_m005')) if shear_only else selected
        if not forecast_selected:
            raise ValueError('Low-level shear repair requires RRFS and/or REFS member model selection')
        for cyc in cycles:
            date, hour = cyc.strftime('%Y%m%d'), cyc.strftime('%H')
            stage_name = f'forecast_{date}_{hour}'
            model_job = [args.python,str(converter),'--date',date,'--cycle',hour,
                         '--models',','.join(forecast_selected), '--workers',str(args.workers),
                         '--no-remote-cleanup',*common,'--no-publish']
            if shear_only:
                model_job += ['--repair-lowlevel-shear']
            elif args.mode == 'all':
                model_job += ['--overwrite-hours']
            else:
                model_job += ['--incremental','--lookahead-hours','84',
                              '--max-hour-jobs','1000','--force-missing-check']
            # A catalog's writer is serialized across cycles. Model publication
            # is a part of this stage, so no other model stage publishes first.
            publish_job = ([args.python,str(converter),'--date',date,'--cycle',hour,
                            '--models',','.join(forecast_selected),'--publish-only',
                            '--no-remote-cleanup',*common] if not args.no_publish else None)
            stages.append(Stage(stage_name,
                                (prev_forecast,) if prev_forecast else (),
                                (lambda c=model_job,p=publish_job: collect_job_result(
                                    c,*([p] if p else [])))))
            prev_forecast = stage_name
            if not skip_soundings:
                profile_name = f'forecast_profiles_{date}_{hour}'
                profile_job = forecast_profile_job(args,profile_script,cyc,selected)
                stages.append(Stage(profile_name,
                                    (prev_profile,) if prev_profile else (),
                                    lambda job=profile_job: command(job,dry_run=False)))
                # A new forecast cycle may have no processed model hours at
                # the time the independent profile stage starts. Reconcile
                # after BOTH stages finish so a single case pass always
                # discovers newly converted model hours. Never re-force
                # completed hours in this inexpensive second pass.
                reconcile_name = f'forecast_profiles_reconcile_{date}_{hour}'
                reconcile_job = list(profile_job)
                if '--reprocess-hours' in reconcile_job:
                    reconcile_job.remove('--reprocess-hours')
                retry_i = reconcile_job.index('--missing-retry-hours') + 1
                reconcile_job[retry_i] = '6'
                stages.append(Stage(reconcile_name,(stage_name,profile_name),
                                    lambda job=reconcile_job: command(job,dry_run=False)))
                prev_profile = reconcile_name
        prev_analysis = None
        prev_aligned = None
        if not args.skip_analysis and available_end >= start:
            for window_start,window_end in chunk_hours(start,available_end):
                tag = f'{window_start:%Y%m%d%H}_{window_end:%Y%m%d%H}'
                analysis_name = f'native_analysis_{tag}'
                analysis_job = [args.python,str(converter),'--verification-only',*common,
                                '--source','all','--start-hour',window_start.strftime(FORMAT),
                                '--end-hour',window_end.strftime(FORMAT)]
                if args.mode == 'all' or getattr(args,'reprocess_analysis',False):
                    analysis_job += ['--force']
                if args.no_publish:
                    analysis_job += ['--no-publish']
                stages.append(Stage(analysis_name,
                                    (prev_analysis,) if prev_analysis else (),
                                    lambda job=analysis_job: command(job,dry_run=False)))
                prev_analysis = analysis_name
                # Alignment may start as soon as THIS analysis batch succeeds;
                # it need not wait for subsequent analysis windows. Align jobs
                # are serialized with each other to protect their catalog.
                align_name = f'aligned_analysis_{tag}'
                align_job = [args.python,str(aligner),'--output-root',str(args.output_root),
                             '--work-root',str(args.work_root),
                             '--protected-cycles-file',str(protected),
                             '--publish-host',args.publish_host,'--publish-dir',args.publish_dir,
                             '--since',window_start.strftime(FORMAT),
                             '--until',window_end.strftime(FORMAT),
                             '--max-new-hours',str(args.max_aligned_stores),
                             '--workers',str(getattr(args,'aligned_workers',2)),
                             '--publish-workers',str(getattr(args,'publish_workers',3))]
                if args.no_publish:
                    align_job += ['--no-publish']
                stages.append(Stage(align_name,
                                    tuple(x for x in (analysis_name,prev_aligned) if x),
                                    lambda job=align_job: command(job,dry_run=False)))
                prev_aligned = align_name
            if not skip_soundings:
                for window_start,window_end in chunk_hours(start,available_end):
                    tag = f'{window_start:%Y%m%d%H}_{window_end:%Y%m%d%H}'
                    name = f'rap_profiles_{tag}'
                    rap_job = rap_profile_job(args,profile_script,window_start,window_end)
                    stages.append(Stage(name, (prev_profile,) if prev_profile else (),
                                        lambda job=rap_job: command(job,dry_run=False)))
                    prev_profile = name
        if not args.skip_observations and available_end >= start:
            prev_obs = None
            for window_start,window_end in chunk_hours(start,available_end):
                name = f'observations_{window_start:%Y%m%d%H}_{window_end:%Y%m%d%H}'
                stages.append(Stage(name,(prev_obs,) if prev_obs else (),
                                    lambda a=window_start,b=window_end:
                                    collect_observations(args,a,b,args.python,collector)))
                prev_obs = name
            first_day = (start-dt.timedelta(hours=12)).strftime('%y%m%d')
            last_day = (available_end-dt.timedelta(hours=12)).strftime('%y%m%d')
            first_spc_day=dt.datetime.strptime(first_day,'%y%m%d').replace(tzinfo=UTC)
            last_spc_day=dt.datetime.strptime(last_day,'%y%m%d').replace(tzinfo=UTC)
            prev_spc = None
            while first_spc_day <= last_spc_day:
                last_batch=min(last_spc_day,first_spc_day+dt.timedelta(days=30))
                name=f'spc_{first_spc_day:%y%m%d}_{last_batch:%y%m%d}'
                spc_job=[args.python,str(spc_collector),
                         '--start-day',first_spc_day.strftime('%y%m%d'),
                         '--end-day',last_batch.strftime('%y%m%d'),
                         '--output-root',str(args.output_root),'--publish-host',args.publish_host,
                         '--publish-dir',args.publish_dir]
                if not args.no_publish:
                    spc_job += ['--publish']
                stages.append(Stage(name,(prev_spc,) if prev_spc else (),
                                    lambda job=spc_job: command(job,dry_run=False)))
                prev_spc=name
                first_spc_day=last_batch+dt.timedelta(days=1)
        if run_glm and available_end >= start:
            prev_glm = None
            for window_start, window_end in chunk_hours(start, available_end):
                name = f'glm_lightning_{window_start:%Y%m%d%H}_{window_end:%Y%m%d%H}'
                glm_job = [args.python, str(glm_collector),
                           '--start', window_start.strftime(FORMAT),
                           '--end', window_end.strftime(FORMAT),
                           '--output-root', str(args.output_root),
                           '--workers', str(getattr(args, 'glm_workers', 4)),
                           '--publish-host', args.publish_host,
                           '--publish-dir', args.publish_dir]
                if not args.no_publish and getattr(args, 'glm_publish', 1) == 1:
                    glm_job += ['--publish']
                stages.append(Stage(name, (prev_glm,) if prev_glm else (),
                                    lambda job=glm_job: run_optional_glm_job(
                                        job, warning_callback=record_glm_warning)))
                prev_glm = name
        stage_workers = getattr(args,'stage_workers',1)
        # Per-run logs avoid output interleaving from independent subprocesses.
        # Capture is only enabled for concurrent runs; sequential fallback
        # retains the original streaming output for familiar debugging.
        run_logs = (args.work_root/'case_stage_logs'/
                    (cycles[0].strftime(FORMAT)+'_'+utc_now().strftime('%Y%m%dT%H%M%S%fZ'))
                    if stage_workers > 1 else None)
        print(f'Executing {len(stages)} stage(s), at most {stage_workers} concurrent '
              f'({"stage logs in " + str(run_logs) if run_logs else "streaming logs"})', flush=True)
        errors = run_stages(stages,stage_workers,logs_dir=run_logs)
    if errors:
        print('\nCASE FINISHED WITH ERRORS:',flush=True)
        for error in errors: print('  '+error,flush=True)
        return 1
    if glm_warnings:
        print('\nCASE PASS COMPLETE WITH OPTIONAL GLM WARNINGS: primary forecast, analysis, '
              'sounding and requested observation stages completed. '
              f'{len(glm_warnings)} GLM batch(es) need another attempt.', flush=True)
        print('GLM hours with no complete cached GeoJSON will be retried on the next '
              'execution of this case.', flush=True)
    else:
        print(('\nCASE PASS COMPLETE: targeted low-level shear repair and publication requested as configured.'
               if shear_only else
               '\nCASE PASS COMPLETE: forecasts, analyses, soundings, requested observations and enabled GLM hours '
               'retained and publication requested as configured.'),flush=True)
    return 0


def main(argv=None):
    directory=Path(__file__).resolve().parent
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    mutually=parser.add_mutually_exclusive_group(required=True)
    mutually.add_argument('--cycle',help='Single model initialization YYYYMMDDHH UTC')
    mutually.add_argument('--start-cycle',help='First initialization YYYYMMDDHH UTC (requires --end-cycle)')
    parser.add_argument('--end-cycle',help='Last initialization YYYYMMDDHH UTC (inclusive, every 6 hours)')
    parser.add_argument('--models',default=None,help='all or comma-separated RRFS, HRRR, REFS mean/member IDs; defaults to shared MODELS when configured')
    parser.add_argument('--mode',choices=['missing','all'],default='missing',
                        help='missing recovers absent fields; all overwrites existing model/analysis records')
    parser.add_argument('--execute',action='store_true',help='Wait for active crons, block new crons, protect, process and publish; otherwise preview only')
    parser.add_argument('--lowlevel-shear-only',action='store_true',
                        help='Only repair previously processed RRFS/REFS-member 0-1 km shear, under normal case/cron locks; skip soundings, analyses, observations')
    parser.add_argument('--no-publish',action='store_true',help='Store data locally only')
    parser.add_argument('--skip-analysis',action='store_true',help='Do not collect/regrid verification analyses or RAP soundings')
    parser.add_argument('--skip-soundings',action='store_true',help='Do not create or publish forecast/RAP sounding profiles')
    parser.add_argument('--reprocess-soundings',action='store_true',help='Refresh existing sounding hours from native fields even in --mode missing')
    parser.add_argument('--reprocess-analysis',action='store_true',help='Force native verification/analysis reprocessing even in --mode missing; aligned fields will refresh')
    parser.add_argument('--skip-observations',action='store_true',help='Do not retrieve Synoptic or mPING reports')
    parser.add_argument('--skip-glm',action='store_true',help='Do not collect GLM lightning even when GLM_ENABLED=1 and GLM_CASE_ENABLED=1 in the shared configuration')
    parser.add_argument('--rights-confirmed',action='store_true',help='Confirm authority to publish Synoptic and mPING reports to the restricted viewer')
    parser.add_argument('--retention-days',type=int,default=None,
                        help='Native analysis retention (1–366 days); defaults to CASE_RETENTION_DAYS / RETENTION_DAYS / 7')
    parser.add_argument('--config-file',type=Path,
                        default=default_converter_config(directory),
                        help='Trusted shared cron/case configuration file (auto-loaded when present)')
    parser.add_argument('--workers',type=int,default=None,
                        help='Model worker processes (any positive integer); overrides CASE_WORKERS / INCREMENTAL_WORKERS')
    parser.add_argument('--max-aligned-stores',type=int,default=2500,
                        help='Maximum updated alignment stores per 168-hour batch')
    parser.add_argument('--aligned-workers',type=int,default=None,
                        help='Analysis alignment workers (any positive integer); overrides CASE_ALIGNED_WORKERS / ALIGNED_WORKERS')
    parser.add_argument('--sounding-workers',type=int,default=None,
                        help='Sounding-hour decodes (any positive integer); overrides CASE_SOUNDING_WORKERS / SOUNDING_PROFILE_HOUR_WORKERS')
    parser.add_argument('--stage-workers',type=int,default=None,
                        help='Independent case stages (any positive integer); overrides CASE_STAGE_WORKERS')
    parser.add_argument('--publish-workers',type=int,default=None,
                        help='SSH/SCP publishers (any positive integer); overrides CASE_PUBLISH_WORKERS / PUBLISH_WORKERS')
    parser.add_argument('--lock-dir',type=Path,default=None,
                        help='Canonical cron BASE_DIR for all case/cron lock files (defaults to protected-cycles directory)')
    parser.add_argument('--output-root',type=Path,default=None)
    parser.add_argument('--work-root',type=Path,default=None)
    parser.add_argument('--protected-cycles-file',type=Path,default=None)
    parser.add_argument('--secrets-file',type=Path,default=None)
    parser.add_argument('--publish-host',default=None)
    parser.add_argument('--publish-dir',default=None)
    parser.add_argument('--wgrib2',default=None,help='Explicit installed wgrib2 executable (if needed)')
    parser.add_argument('--python',default=sys.executable,help='Python executable from the configured processing environment')
    args=parser.parse_args(argv)
    try:
        supplied_args = argv if argv is not None else sys.argv[1:]
        explicit_config = any(x == '--config-file' or x.startswith('--config-file=')
                              for x in supplied_args)
        if explicit_config and not args.config_file.is_file():
            raise ValueError(f'Explicit --config-file not found: {args.config_file}')
        config = read_worker_config(args.config_file.expanduser().absolute())
        worker_sources = resolve_worker_settings(args, config)
        resolve_glm_settings(args, config)
        if args.models is None:
            args.models = config.get('MODELS', 'all')
        if args.protected_cycles_file is None:
            args.protected_cycles_file = Path(config.get('PROTECTED_CYCLES_FILE',
                                                         str(directory/'protected_cycles.json'))).expanduser()
        if args.secrets_file is None:
            args.secrets_file = Path(config.get('PTYPE_OBS_SECRETS_FILE',
                                                str(directory/'ptype_obs_secrets.sh'))).expanduser()
        if args.output_root is None:
            args.output_root = Path(config.get('OUTPUT_ROOT', str(directory/'zarr'))).expanduser()
        if args.work_root is None:
            args.work_root = Path(config.get('WORK_ROOT', str(directory/'work'))).expanduser()
        if args.publish_host is None:
            args.publish_host = config.get('PUBLISH_HOST', 'thultquist@rzdm.ncep.noaa.gov')
        if args.publish_dir is None:
            args.publish_dir = config.get('PUBLISH_DIR',
                '/home/people/emc/www/htdocs/users/verification_restricted/test/trh/dynamic_viewer/data')
        no_publish_config = config.get('NO_PUBLISH', '0').strip()
        if no_publish_config not in ('0', '1'):
            raise ValueError(f'NO_PUBLISH must be 0 or 1, not {no_publish_config!r}')
        if no_publish_config == '1':
            args.no_publish = True
        if args.retention_days is None:
            raw_retention = config.get('CASE_RETENTION_DAYS', config.get('RETENTION_DAYS','7'))
            try:
                args.retention_days = int(raw_retention)
            except ValueError as exc:
                raise ValueError(f'Invalid configured retention days {raw_retention!r}') from exc
        if not 1 <= args.retention_days <= 366:
            raise ValueError('--retention-days must be in 1–366')
        # Match cron's one-thread-per-native-decoder policy.  Bounded process
        # pools cannot respect a CPU budget if each decoder spawns many BLAS
        # or OpenMP threads.  These assignments affect only this case run and
        # its subprocesses, not the parent shell or cron configuration.
        if args.execute:
            for name in ('OMP_NUM_THREADS', 'OMP_THREAD_LIMIT', 'MKL_NUM_THREADS',
                         'OPENBLAS_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
                os.environ[name] = '1'
        print('Case worker settings: ' + ', '.join(
            f'{key}={getattr(args, key)} ({worker_sources[key]})'
            for key in CASE_WORKER_DEFAULTS), flush=True)
        if args.cycle:
            if args.end_cycle: parser.error('--end-cycle is only valid with --start-cycle')
            first=last=parse_cycle(args.cycle)
        else:
            if not args.end_cycle: parser.error('--start-cycle requires --end-cycle')
            first,last=parse_cycle(args.start_cycle),parse_cycle(args.end_cycle)
        cycles=cycles_between(first,last)
        selected=model_ids(args.models)
        if args.workers < 1:parser.error('--workers must be positive')
        if not 1 <= args.max_aligned_stores <= 5000:parser.error('--max-aligned-stores must be 1–5000')
        if args.aligned_workers < 1:parser.error('--aligned-workers must be positive')
        if args.stage_workers < 1:parser.error('--stage-workers must be positive')
        if args.sounding_workers < 1:parser.error('--sounding-workers must be positive')
        if args.publish_workers < 1:parser.error('--publish-workers must be positive')
        args.output_root=args.output_root.expanduser().resolve()
        args.work_root=args.work_root.expanduser().resolve()
        args.protected_cycles_file=args.protected_cycles_file.expanduser().absolute()
        args.secrets_file=args.secrets_file.expanduser().absolute()
        perf_enabled = config.get('EVU_PERF_ENABLED', os.environ.get('EVU_PERF_ENABLED', '0'))
        if perf_enabled not in ('0', '1'):
            raise ValueError('EVU_PERF_ENABLED must be 0 or 1')
        os.environ['EVU_PERF_ENABLED'] = perf_enabled
        os.environ['EVU_PERF_SAMPLE_SECONDS'] = config.get('EVU_PERF_SAMPLE_SECONDS',
            os.environ.get('EVU_PERF_SAMPLE_SECONDS', '5'))
        os.environ['EVU_PERF_RETENTION_DAYS'] = config.get('EVU_PERF_RETENTION_DAYS',
            os.environ.get('EVU_PERF_RETENTION_DAYS', '14'))
        perf_dir = config.get('EVU_PERF_LOG_DIR', os.environ.get('EVU_PERF_LOG_DIR'))
        if not perf_dir:
            perf_base = args.lock_dir if args.lock_dir is not None else args.protected_cycles_file.parent
            perf_dir = str(perf_base / 'logs' / 'performance')
        os.environ['EVU_PERF_LOG_DIR'] = perf_dir
        with perf_monitor.PerformanceMonitor('historical_case', perf_dir,
                float(os.environ['EVU_PERF_SAMPLE_SECONDS']),
                int(os.environ['EVU_PERF_RETENTION_DAYS'])) as monitor:
            monitor.set_command('create_case.py ' + ' '.join(supplied_args))
            perf_monitor.record_event('run_config', workflow='historical_case',
                cycles=len(cycles), models=list(selected), model_workers=args.workers,
                stage_workers=args.stage_workers, sounding_workers=args.sounding_workers,
                aligned_workers=args.aligned_workers, publish_workers=args.publish_workers,
                glm_enabled=bool(args.glm_enabled and args.glm_case_enabled and not args.skip_glm))
            case_status = execute(args,cycles,selected)
            monitor.set_exit_status(case_status)
            return case_status
    except (ValueError,RuntimeError,OSError) as exc:
        parser.error(str(exc))


if __name__=='__main__':
    sys.exit(main())
