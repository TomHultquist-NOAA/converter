#!/usr/bin/env python3
"""Prune recognized dated cron logs, without touching active or unknown files.

Intended for independent cron pollers; the shared daily model cleanup also
handles these names. Both timestamp and mtime must exceed the retention age.
"""
from __future__ import annotations
import argparse
import datetime as dt
from pathlib import Path
import re

LOG_RE = re.compile(r'^(?:gfs_incremental|ai_incremental|model_incremental|multisource|cron_errors|model_cron_errors)_(\d{8})_(\d{6})(?:_\d+)?\.log$')


def prune(log_dir: Path, days: int, now: dt.datetime | None = None, dry_run: bool = False, prefix: str = 'gfs_incremental') -> list[Path]:
    if not 1 <= days <= 366:
        raise ValueError('retention days must be 1..366')
    if prefix not in ('gfs_incremental', 'ai_incremental', 'model_incremental', 'multisource', 'cron_errors', 'model_cron_errors'):
        raise ValueError('unrecognized log prefix')
    if log_dir.is_symlink() or not log_dir.is_dir():
        return []
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError('now must be timezone-aware')
    cutoff = now.astimezone(dt.timezone.utc) - dt.timedelta(days=days)
    removed = []
    for child in sorted(log_dir.iterdir()):
        match = LOG_RE.fullmatch(child.name)
        if not match or not child.name.startswith(prefix + '_') or child.is_symlink() or not child.is_file():
            continue
        try:
            stamp = dt.datetime.strptime(''.join(match.groups()), '%Y%m%d%H%M%S').replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        if stamp >= cutoff or dt.datetime.fromtimestamp(child.stat().st_mtime, dt.timezone.utc) >= cutoff:
            continue
        removed.append(child)
        if not dry_run:
            child.unlink()
    return removed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--logs-dir', required=True, type=Path)
    ap.add_argument('--retention-days', type=int, required=True)
    ap.add_argument('--prefix', default='gfs_incremental', choices=('gfs_incremental', 'ai_incremental', 'model_incremental', 'multisource', 'cron_errors', 'model_cron_errors'))
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    for path in prune(a.logs_dir, a.retention_days, dry_run=a.dry_run, prefix=a.prefix):
        print(('WOULD REMOVE ' if a.dry_run else 'REMOVE ') + str(path), flush=True)


if __name__ == '__main__':
    main()
