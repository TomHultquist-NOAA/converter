#!/usr/bin/env python3
"""Inspect recent converter performance reports and identify workflow coverage.

Read-only; never starts jobs, modifies Zarr, or deletes logs. Optional context
for support and performance optimization, not a job-health monitor. A missing
label means no matching *completed monitored run* in this time window.
"""
from __future__ import print_function
import argparse
from collections import defaultdict
import datetime
import json
from pathlib import Path
import time

LABELS = (
    ('regional', ('model_incremental_cron','model_conversion','historical_case')),
    ('verification', ('multisource_cron','verification_pipeline','verification_ingest',
                      'verification_convert','analysis_alignment_run')),
    ('gfs', ('gfs_incremental_cron','gfs_conversion','gfs_historical_case')),
    ('ai', ('ai_incremental_cron','ai_conversion')),
    ('observations', ('sounding_profiles','glm_lightning','ptype_observations','spc_lsr')),
)

def summarize(log_dir, hours):
    if log_dir.is_symlink():
        raise ValueError('Refusing symlinked performance directory')
    cutoff = time.time() - hours * 3600
    summary_by_label = defaultdict(list)
    for path in sorted(log_dir.glob('evu_perf_*_summary.json')) if log_dir.is_dir() else []:
        if path.is_symlink() or not path.is_file() or path.stat().st_mtime < cutoff:
            continue
        try:
            report=json.loads(path.read_text(encoding='utf-8'))
            label=report.get('label')
            if report.get('schema') != 'omd-evu-performance-1' or not label:
                continue
            summary_by_label[label].append((path, report))
        except (OSError, ValueError, TypeError):
            continue
    return summary_by_label

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log-dir',type=Path,
        default=Path(__file__).resolve().parent/'logs'/'performance')
    parser.add_argument('--hours',type=float,default=24.0,
        help='Count completed reports modified in last N hours (default 24)')
    args=parser.parse_args(argv)
    if not 0 < args.hours <= 24 * 366:
        parser.error('--hours must be between 0 and 8784')
    try: reports=summarize(args.log_dir,args.hours)
    except (OSError,ValueError) as exc: parser.error(str(exc))
    print('Performance coverage, last %.1f h, directory: %s' % (args.hours,args.log_dir))
    total=sum(map(len,reports.values()))
    print('Completed monitored runs: %d' % total)
    for group, labels in LABELS:
        print('\n%s:' % group.upper())
        for label in labels:
            rows=reports.get(label,[])
            tasks=defaultdict(int)
            for _,row in rows:
                for key,value in (row.get('stage_task_summary') or {}).items():
                    tasks[key]+=int(value.get('count') or 0)
            if rows:
                newest=max(rows,key=lambda r:r[0].stat().st_mtime)[1]
                print('  %-25s %3d runs | latest %.1fs, peak %.1f cores, %.2f GiB' % (
                    label,len(rows),newest.get('duration_seconds',0),
                    newest.get('peak_cpu_cores',0),newest.get('peak_process_tree_rss_gib',0)))
                for key,count in sorted(tasks.items()):
                    print('     %-30s %d tasks' % (key,count))
            else:
                print('  %-25s no completed report in this window' % label)
    covered=set(x for _,labels in LABELS for x in labels)
    for label,rows in sorted(reports.items()):
        if label not in covered:
            print('\nOTHER: %s (%d runs)' % (label,len(rows)))
    if not reports:
        print('\nNo reports: check EVU_PERF_ENABLED=1, work actually ran, and log path.')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
