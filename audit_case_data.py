#!/usr/bin/env python3
"""Read-only audit of NOHRSC and METAR/mPING coverage for a forecast case.

Reports local staged GRIB manifests, converted analysis Zarr stores, local
analysis catalogs, and optional RZDM-published directory contents. No GRIB
processing, credentials, downloads, or changes. Stdlib-only on WCOSS2.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
from pathlib import Path

UTC=dt.timezone.utc

def read_json(p):
    try:return json.loads(p.read_text()) if p.is_file() else None
    except (ValueError,OSError):return None

def product_times(start,end):
    t=start
    while t<=end:
        if t.hour%6==0:
            yield t,'nohrsc_6h','snow6h'
            if t.hour in (0,12):yield t,'nohrsc_24h','snow24h'
        t+=dt.timedelta(hours=1)

def catalog_records(path):
    obj=read_json(path)
    if not obj or not isinstance(obj.get('datasets'),list):return set()
    return {(d.get('product'),d.get('init_time','')[:13],v.get('name'))
            for d in obj['datasets'] for v in d.get('variables',[])
            if 0 in v.get('available_hours',[])}

def inspect_case(init,output,work,remote=None,lead=84):
    end=init+dt.timedelta(hours=lead)
    local=catalog_records(output/'catalog_analysis.json')
    remote_catalog=catalog_records(remote/'catalog_analysis.json') if remote else None
    print(f'CASE {init:%Y%m%d%H} through {end:%Y%m%d%H} UTC (F000–F{lead:03})')
    print(f'Local output root: {output}')
    if remote:print(f'Published data dir: {remote}')
    print('\nNOHRSC UTC-valid fields:')
    print('TIME UTC   PRODUCT      STAGING     LOCAL STORE  LOCAL CAT  '+('RZDM CAT  ' if remote else '')+'ERROR')
    failures=0
    for t,product,field in product_times(init,end):
        key=t.strftime('%Y%m%d%H')
        path=work/'verification_staging'/product/t.strftime('%Y%m%d')/(f'{product}_{key}.json')
        manifest=read_json(path)
        err=read_json(path.with_suffix('.conversion_error.json'))
        store=output/'analysis'/f'analysis_{product}_{key}.zarr'
        complete=(store/'.conversion.json').is_file() and (store/field/'0.0.0').is_file()
        record=(product,t.strftime('%Y-%m-%dT%H'),field)
        listed=record in local
        remote_listed=record in remote_catalog if remote_catalog is not None else None
        staged=manifest.get('status','absent') if manifest else 'absent'
        problem=(err or {}).get('error') or (manifest or {}).get('error') or ''
        if not complete or not listed or (remote and not remote_listed):failures+=1
        print(f'{key} {product:12} {staged:11} {"yes" if complete else "NO":11} '
              f'{"yes" if listed else "NO":9} '+(f'{"yes" if remote_listed else "NO":9} ' if remote else '')+str(problem)[:150])
        if manifest and manifest.get('selected') and not complete:
            print('    staged fields:',','.join(x.get('field','?') for x in manifest['selected']))
    print('\nObservation files (hourly):')
    missing=[];zero=[];partial=[];published_missing=[]
    t=init
    while t<=end:
        key=t.strftime('%Y%m%d%H');localfile=output/'ptype_observations'/(key+'.json')
        item=read_json(localfile)
        if not item or item.get('schema')!='omd-evu-ptype-observations-1':missing.append(key)
        else:
            if not item.get('features'):zero.append(key)
            if any(v!='ok' for v in item.get('provider_status',{}).values()):partial.append(key)
        if remote and not (remote/'ptype_observations'/(key+'.json')).is_file():published_missing.append(key)
        t+=dt.timedelta(hours=1)
    def show(label,collection):
        print(f'  {label}: {len(collection)}'+(f' (first: {", ".join(collection[:6])})' if collection else ''))
    show('Missing local observation hours',missing)
    show('Published zero-classified-report hours',zero)
    show('Provider partial/stale hours',partial)
    if remote:show('Missing published observation hours',published_missing)
    print('\nModel 24-hour snowfall inputs (ASNOW is cumulative snowfall):')
    cycle_key=init.isoformat().replace('+00:00','Z')
    for model,limit in [('rrfs',84),('hrrr',48),('refs_m001',60),('refs_m002',60),('refs_m003',60),('refs_m004',60),('refs_m005',60),('refs_mean',60)]:
        cat=read_json(output/f'catalog_{model}.json') or {}
        ds=next((d for d in cat.get('datasets',[]) if d.get('init_time','').startswith(cycle_key[:16])),None)
        if not ds:
            print(f'  {model:12} cycle not present in local model catalog')
            continue
        v=next((x for x in ds.get('variables',[]) if x.get('name')=='asnow'),None)
        hours=set(v.get('available_hours',[])) if v else set()
        def status(h):
            if h>limit:return '-'
            if h in hours and (h==24 or h-24 in hours):return 'ready'
            return 'MISSING'
        print(f'  {model:12} F024={status(24):7} F048={status(48):7} F072={status(72):7}')
    print('  A 24-hour model window needs cumulative ASNOW at Fh and F(h-24),')
    print('  except F024, which needs only F024. A missing day-boundary')
    print('  chunk cannot be recovered from the browser: use --repair-snowfall.')
    print(f'\nNOHRSC fields with missing local/published coverage: {failures}')
    if failures:
        print('  If staging=pending, inspect manifest.error (HTTP/field-selection problem).')
        print('  If staging=complete but store=NO, inspect *.conversion_error.json (regrid/units).')
        print('  If store=yes but LOCAL CAT=NO, regenerate the analysis catalog.')
        print('  If LOCAL CAT=yes but RZDM CAT=NO, republish analyses and catalog.')
    if missing or published_missing:
        print('  Missing observations require historical retrieval and authorized publishing;')
        print('  zero classified reports do not necessarily indicate a failure.')
    return failures

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cycle',required=True,help='Forecast initialization YYYYMMDDHH')
    p.add_argument('--output-root',type=Path,required=True,help='WCOSS2 zarr root')
    p.add_argument('--work-root',type=Path,required=True,help='WCOSS2 converter work root')
    p.add_argument('--published-data-dir',type=Path,help='Optional mounted/SSH shell directory on RZDM containing catalog_analysis.json')
    p.add_argument('--max-lead',type=int,default=84,help='Case window in hours (default 84)')
    a=p.parse_args()
    if a.max_lead<0 or a.max_lead>168:p.error('--max-lead must be 0–168')
    try:stamp=dt.datetime.strptime(a.cycle,'%Y%m%d%H').replace(tzinfo=UTC)
    except ValueError:p.error('--cycle must be YYYYMMDDHH UTC')
    inspect_case(stamp,a.output_root,a.work_root,a.published_data_dir,a.max_lead)
if __name__=='__main__':main()
