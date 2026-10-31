#!/usr/bin/env python3
"""Read-only report: model Zarr stores and local/published catalog coverage."""
import argparse
import datetime as dt
import json
import re
from pathlib import Path

MODELS=('rrfs','hrrr','refs_mean','refs_m001','refs_m002','refs_m003','refs_m004','refs_m005')
RX=re.compile(r'^(rrfs|hrrr|refs_mean|refs_m00[1-5])_(\d{8})_(00|06|12|18)00\.zarr$')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('./zarr'))
    p.add_argument('--days',type=int,default=7)
    a=p.parse_args()
    now=dt.datetime.now(dt.timezone.utc)
    cutoff=now-dt.timedelta(days=a.days)
    print('Model coverage as of',now.strftime('%Y-%m-%d %H:%M UTC'))
    print('Nominal cutoff',cutoff.strftime('%Y-%m-%d %H:%M UTC'))
    print('Model       Local stores / earliest init     Catalog entries / earliest init')
    for model in MODELS:
        local=[]
        for directory in a.root.iterdir() if a.root.is_dir() else []:
            m=RX.fullmatch(directory.name)
            if not m or m.group(1)!=model or not directory.is_dir():continue
            day,hh=m.group(2,3)
            moment=dt.datetime.strptime(day+hh,'%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
            if moment>=cutoff:local.append(moment)
        catalog=a.root/f'catalog_{model}.json'
        entries=[]
        if catalog.exists():
            try:
                payload=json.loads(catalog.read_text())
                for dataset in payload.get('datasets',[]):
                    init=dataset.get('init_time','').replace('Z','+00:00')
                    when=dt.datetime.fromisoformat(init)
                    if when>=cutoff:entries.append(when)
            except (OSError,TypeError,ValueError) as exc:
                print('  CATALOG ERROR',model,exc)
        local_txt=f'{len(local):>3} / {min(local).strftime("%m-%d %HZ") if local else "none"}'
        cat_txt=f'{len(entries):>3} / {min(entries).strftime("%m-%d %HZ") if entries else "none"}'
        print(f'{model:<11} {local_txt:<31} {cat_txt}')
    print('\nNote: a 14-day retention setting does not reconstruct cycles deleted under a prior 7-day policy.')
    print('If local stores exist but published catalog lacks them, republish those cycles; if local stores are absent, backfill GRIB->Zarr.')

if __name__=='__main__':main()
