#!/usr/bin/env python3
"""Safely preview or remove legacy EVU performance telemetry (not operational logs)."""
import argparse
from pathlib import Path
import re

REPORT_RE = re.compile(r'^evu_perf_[A-Za-z0-9_.-]+_(?:samples|events)\.jsonl$|'
                       r'^evu_perf_[A-Za-z0-9_.-]+_summary\.(?:json|txt)$')


def find_reports(directory):
    directory = Path(directory).expanduser()
    # Don't trust a symlinked telemetry directory, and never descend into other trees.
    if directory.is_symlink():
        raise ValueError('Refusing to inspect a symlinked telemetry directory')
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise ValueError('Performance log path is not a directory')
    if directory.name != 'performance':
        raise ValueError('Directory must have basename "performance"')
    return sorted(p for p in directory.iterdir()
                  if REPORT_RE.fullmatch(p.name) and p.is_file() and not p.is_symlink())


def run(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log-dir', type=Path,
                        default=Path(__file__).resolve().parent/'logs'/'performance')
    parser.add_argument('--apply', action='store_true', help='Delete matching reports; otherwise preview only')
    args = parser.parse_args(argv)
    try:
        reports = find_reports(args.log_dir)
    except ValueError as exc:
        parser.error(str(exc))
    for path in reports:
        print(('DELETE ' if args.apply else 'PREVIEW ') + str(path))
        if args.apply:
            path.unlink()
    print(('Removed ' if args.apply else 'Found ') + str(len(reports)) + ' legacy performance report(s)')
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
