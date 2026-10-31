#!/usr/bin/env python3
"""Hourly verification subcommand for multi_model_to_zarr_wcoss2.py.

Shares the operational model converter's cron lock, environment, output roots,
retention setting and TAR/SCP transport. This is a helper, NOT a second cron job.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

import verification_ingest as ingest
import verification_convert as convert
from retention_policy import load_policy, protects_analysis, rules_json

LOG = logging.getLogger('evu-unified-verify')
PRODUCTS = tuple(convert.STAGED_PRODUCTS)
UTC = dt.timezone.utc
STORE_RE = re.compile(r'^analysis_(mrms_refc|mrms_prate|mrms_apcp|urma|rtma|rap_surface|rap_pressure|nohrsc_6h|nohrsc_24h|nohrsc_48h|nohrsc_72h)_(\d{10})\.zarr$')


def completed_store(store: Path) -> bool:
    """Do not redownload unless a committed store AND each listed chunk exist."""
    try:
        if store.is_symlink() or not store.is_dir():
            return False
        meta = json.loads((store/'.conversion.json').read_text())
        fields = meta['fields']
        return bool(fields) and all((store/field/'0.0.0').is_file() and
                                    (store/field/'0.0.0').stat().st_size > 0 for field in fields)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def manifest_matches_current_selection(manifest: dict, expected: dict) -> bool:
    """An existing analysis is current only after its staged inventory was checked
    against the *present* field selectors, not merely its original field list.

    Explicitly missing GRIB messages count as checked: don't redownload known
    unavailable fields forever. Legacy manifests without inventory text remain
    usable if all requested keys were accounted for; when records include text,
    recheck the exact short name and level to catch changed CAPE definitions.
    """
    if not isinstance(manifest, dict) or manifest.get('status') not in ('complete', 'partial'):
        return False
    raw_selected = manifest.get('selected', [])
    raw_missing = manifest.get('missing', [])
    if not isinstance(raw_selected, list) or not isinstance(raw_missing, list):
        return False
    selected = {item.get('field'): item for item in raw_selected
                if isinstance(item, dict) and isinstance(item.get('field'), str)}
    missing = {item for item in raw_missing if isinstance(item, str)}
    if not set(expected).issubset(set(selected) | missing):
        return False
    for name in set(expected) & set(selected):
        original_line = selected[name].get('inventory')
        if original_line and name not in ingest.select_inventory_records(
                [original_line], {name: expected[name]})[0]:
            return False
    return True



def converted_store_matches_staging(store: Path, manifest: dict) -> bool:
    """A complete stored analysis must reflect *all* staged supported fields.

    A manifest may be refreshed after a Zarr conversion was committed. Its
    selectors can be current even though the original store omitted e.g. RAP
    MUCAPE. Compare the staged record keys to actual converted fields before
    accepting that store as already complete. Missing GRIB records are excluded
    (they were never staged), and known derived products use their output key.
    """
    if not isinstance(manifest, dict) or manifest.get('status') not in ('complete', 'partial'):
        return False
    try:
        info = json.loads((store / '.conversion.json').read_text())
        actual = set(info['fields'])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    selected = {entry['field'] for entry in manifest.get('selected', [])
                if isinstance(entry, dict) and isinstance(entry.get('field'), str)}
    expected = {('gust' if name == 'gust10' else name)
                for name in selected if ('gust' if name == 'gust10' else name) in convert.FIELD_UNITS}
    if manifest.get('product') == 'rap_pressure':
        # Components are stored as one layer-integrated bulk shear magnitude.
        expected.difference_update({'shru06', 'shrv06'})
        if {'shru06', 'shrv06'} <= selected:
            expected.add('shear06')
        if {'u300', 'v300'} <= selected:
            expected.add('wspd300')
    if manifest.get('product') == 'rap_surface' and 'snod' in selected:
        expected.add('snowpresence')
    if manifest.get('product') in ('urma', 'rtma') and {'t2m', 'dpt2m'} <= selected:
        expected.add('rh2m')
    return bool(expected) and expected <= actual


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_state(path, data):
    convert.atomic_json(path, data)


REMOTE_ANALYSIS_FINALIZE = r'''
from pathlib import Path
from datetime import datetime, timezone
import json, os, re, shutil, sys
root=Path(sys.argv[1]); cutoff_text=sys.argv[2]; dry=sys.argv[3]=='1'
# Validated UTC hours + product list, transferred as JSON; never accept paths.
products={'mrms_refc','mrms_prate','mrms_apcp','rap_surface','rap_pressure','urma','rtma','nohrsc_6h','nohrsc_24h','nohrsc_48h','nohrsc_72h'}
try:
    rules=json.loads(sys.argv[4])
    if not isinstance(rules,list):raise ValueError('Not an array')
    for item in rules:
        if not isinstance(item,list) or len(item)!=3:raise ValueError('Invalid interval')
        start,end,names=item
        if not isinstance(start,str) or not isinstance(end,str) or not re.fullmatch(r'\d{10}',start) or not re.fullmatch(r'\d{10}',end):raise ValueError('Invalid UTC hours')
        t0=datetime.strptime(start,'%Y%m%d%H');t1=datetime.strptime(end,'%Y%m%d%H')
        if t1<t0 or (t1-t0).days>366:raise ValueError('Invalid interval span')
        if not isinstance(names,list) or not names or any(not isinstance(x,str) or x not in products for x in names):raise ValueError('Invalid source names')
except (IndexError,ValueError,TypeError) as exc:
    raise SystemExit('Unsafe protected analysis configuration: '+str(exc))
def pinned(name):
    m=re.fullmatch(r'analysis_(mrms_refc|mrms_prate|mrms_apcp|urma|rtma|rap_surface|rap_pressure|nohrsc_6h|nohrsc_24h|nohrsc_48h|nohrsc_72h)_(\d{10})\.zarr',name)
    return bool(m and any(start<=m.group(2)<=end and m.group(1) in names for start,end,names in rules))
if not root.is_absolute() or root.name!='data' or root.is_symlink() or not root.is_dir():
    raise SystemExit('Unsafe remote data directory')
if not re.match(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$', cutoff_text):
    raise SystemExit('Invalid cutoff')
cutoff=datetime.strptime(cutoff_text,'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
pattern=re.compile(r'^analysis_(mrms_refc|mrms_prate|mrms_apcp|urma|rtma|rap_surface|rap_pressure|nohrsc_6h|nohrsc_24h|nohrsc_48h|nohrsc_72h)_(\d{10})\.zarr$')
folder=root/'analysis'
pending=root/'.catalog_analysis.json.incoming'
# Validate catalog and every referenced Zarr BEFORE making the catalog visible.
if not pending.is_file() or pending.is_symlink():
    raise SystemExit('Missing incoming analysis catalog')
obj=json.loads(pending.read_text(encoding='utf-8'))
if obj.get('schema')!='rrfs-zarr-viewer-1' or not isinstance(obj.get('datasets'),list):
    raise SystemExit('Invalid analysis catalog')
for entry in obj['datasets']:
    if not isinstance(entry,dict):raise SystemExit('Invalid entry')
    path=entry.get('path','')
    parts=Path(path).parts
    if len(parts)!=2 or parts[0]!='analysis' or not pattern.fullmatch(parts[1]):
        raise SystemExit('Unexpected analysis catalog path '+str(path))
    dest=root/path
    if dest.is_symlink() or not (dest/'.conversion.json').is_file():
        raise SystemExit('Referenced store not present: '+str(path))
# New catalog is complete. Refresh it atomically, then expire old stores.
if not dry:os.replace(str(pending),str(root/'catalog_analysis.json'))
removed=0
if folder.is_dir() and not folder.is_symlink():
    for child in folder.iterdir():
        m=pattern.fullmatch(child.name)
        if not m or child.is_symlink() or not child.is_dir():continue
        when=datetime.strptime(m.group(2),'%Y%m%d%H').replace(tzinfo=timezone.utc)
        if when>=cutoff or pinned(child.name):continue
        print(('WOULD REMOVE ' if dry else 'REMOVE ')+str(child),flush=True)
        if not dry:shutil.rmtree(str(child))
        removed+=1
print('Analysis retention removed='+str(removed),flush=True)
'''


def publish_catalog(catalog: Path, host: str, remote_dir: str, days: int, cutoff=None, analysis_rules=()):
    cutoff = cutoff or dt.datetime.now(UTC) - dt.timedelta(days=days)
    cutoff_text = cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')
    remote_tmp = remote_dir.rstrip('/')+'/.catalog_analysis.json.incoming'
    # subprocess argv: do not invoke an interpolated local shell.
    subprocess.run(['scp',str(catalog),f'{host}:{remote_tmp}'],check=True)
    args = 'python3 - '+shlex.quote(remote_dir)+' '+shlex.quote(cutoff_text)+' 0 '+shlex.quote(rules_json(analysis_rules))
    subprocess.run(['ssh',host,args],input=REMOTE_ANALYSIS_FINALIZE,text=True,check=True)



def cleanup_analysis_archives(work: Path, days: int, now: dt.datetime, rules=()):
    """Expire leftover analysis publication TARs, never removing pinned hours."""
    cutoff = now - dt.timedelta(days=days)
    removed = 0
    for entry in work.iterdir() if work.is_dir() and not work.is_symlink() else ():
        if not entry.is_file() or entry.is_symlink():
            continue
        m = re.fullmatch(r'analysis_(mrms_refc|mrms_prate|mrms_apcp|urma|rtma|rap_surface|rap_pressure|nohrsc_6h|nohrsc_24h|nohrsc_48h|nohrsc_72h)_(\d{10})\.tar(?:\.bz2)?', entry.name)
        if not m:
            continue
        try:
            when = dt.datetime.strptime(m.group(2), '%Y%m%d%H').replace(tzinfo=UTC)
        except ValueError:
            continue
        if when < cutoff and not protects_analysis(f'analysis_{m.group(1)}_{m.group(2)}.zarr', rules):
            entry.unlink()
            removed += 1
    return removed

def run(a, publish_store, validate_publisher, now=None):
    now = now or dt.datetime.now(UTC)
    policy = load_policy(a.protected_cycles_file)  # fail closed before conversion, deletion, or publication
    config = ingest.load_json(a.config)
    if config.get('schema')!='omd-evu-verification-source-map-1':
        raise ValueError('Verification source map has an unexpected schema')
    if not 1<=a.lookback_hours<=24*a.retention_days:
        raise ValueError('lookback-hours must be within the retention window')
    if not 1<=a.nohrsc_lookback_hours<=24*a.retention_days:
        raise ValueError('NOHRSC lookback hours must be within retention window')
    if not 1<=a.mrms_stride<=16:
        raise ValueError('MRMS stride must be 1..16')
    if not a.wgrib2 or not os.path.isfile(a.wgrib2) or not os.access(a.wgrib2,os.X_OK):
        raise ValueError('Provide an executable --wgrib2')
    out = Path(a.output_root).expanduser().resolve()
    work = Path(a.work_root).expanduser().resolve()
    stage = work/'verification_staging'
    out.mkdir(parents=True,exist_ok=True)
    stage.mkdir(parents=True,exist_ok=True)
    if not a.no_publish:
        validate_publisher(a.publish_host,a.publish_dir)
    client=ingest.Client(timeout=a.timeout,retries=a.retries)
    summary={'already':0,'converted':0,'pending':0,'partial':0,'not_due':0,'failed':0,
             'published':0,'publish_failed':0,'inspected':0}
    cutoff = now - dt.timedelta(days=a.retention_days)
    start_hour = ingest.hour(now)
    changed=[]
    if a.start_hour is not None:
        # Exact UTC backfill for protected case studies. Native NOHRSC products
        # are selected according to their own valid-hour availability rules.
        first = dt.datetime.strptime(a.start_hour, '%Y%m%d%H').replace(tzinfo=UTC)
        last = dt.datetime.strptime(a.end_hour, '%Y%m%d%H').replace(tzinfo=UTC)
        if last < first or (last-first) > dt.timedelta(hours=167):
            raise ValueError('Explicit verification backfill must cover 1–168 hours')
        requested_hours = [(first+dt.timedelta(hours=i), None) for i in range(int((last-first).total_seconds()/3600)+1)]
    else:
        requested_hours = [(start_hour-dt.timedelta(hours=offset), offset)
            for offset in range(max(a.lookback_hours,a.nohrsc_lookback_hours)-1,-1,-1)]
    for t,offset in requested_hours:
        if t<cutoff and not any(start <= ingest.stamp(t) <= end for start,end,_ in policy.analysis_rules):
            continue
        for job in ingest.jobs_for(t,config):
            product,_,delay_kind=job
            if offset is not None and offset>=a.lookback_hours and delay_kind!='nohrsc':continue
            if a.source!='all' and delay_kind.split('_')[0]!=a.source:
                continue
            store=out/'analysis'/f'analysis_{product}_{ingest.stamp(t)}.zarr'
            manifest_file=stage/product/t.strftime('%Y%m%d')/f'{product}_{ingest.stamp(t)}.json'
            previous=ingest.load_json(manifest_file)
            expected=config['fields'][product]
            inventory_current=manifest_matches_current_selection(previous, expected)
            conversion_current=converted_store_matches_staging(store, previous)
            if not a.force and completed_store(store) and inventory_current and conversion_current and not a.inspect_grid:
                summary['already']+=1
                continue
            if not ingest.valid_now(t,delay_kind,now,config):
                summary['not_due']+=1
                continue
            if completed_store(store) and not inventory_current:
                LOG.info('Refreshing obsolete staged inventory for %s %s (new verification fields/selectors)',
                         product,ingest.iso(t))
            elif completed_store(store) and not conversion_current:
                LOG.info('Refreshing converted %s %s: staged fields missing from committed store',
                         product,ingest.iso(t))
            try:
                staged_path=stage/previous.get('file','') if previous.get('file') else None
                if (a.force or not inventory_current
                    or staged_path is None or not staged_path.is_file()):
                    result=ingest.process_job(client,a.wgrib2,stage,t,job,config,now,
                                              plan=False,force=a.force,keep_raw=False)
                    if result in ('pending','not_due'):
                        summary[result]+=1
                        continue
                manifest=ingest.load_json(manifest_file)
                if a.inspect_grid:
                    result=convert.process_manifest(manifest,stage,out,a.wgrib2,
                                                    a.compression_level,a.mrms_stride,
                                                    force=a.force,inspect=True)
                    if result: summary['inspected']+=1
                    continue
                result=convert.process_manifest(manifest,stage,out,a.wgrib2,
                                                a.compression_level,a.mrms_stride,
                                                force=True,inspect=False)
                if result=='converted':
                    summary['converted']+=1
                    changed.append(store)
                    if manifest.get('status')=='partial':summary['partial']+=1
                    LOG.info('Ready %s valid %s',product,ingest.iso(t))
                elif result=='already':
                    summary['already']+=1
                else:
                    summary['pending']+=1
                if not a.keep_grib and completed_store(store) and staged_path is not None:
                    staged_path.unlink(missing_ok=True)
                if not a.keep_grib and completed_store(store):
                    acquired=stage/manifest.get('file','')
                    if acquired.is_file():acquired.unlink()
            except Exception as exc:
                summary['failed']+=1
                LOG.exception('Conversion failure %s valid %s: %s',product,ingest.iso(t),exc)
                # Keep the original manifest/staged GRIB retryable, and record
                # the conversion failure separately for case-level diagnostics.
                try:
                    if manifest_file.is_file():
                        diagnostic=manifest_file.with_suffix('.conversion_error.json')
                        diagnostic.write_text(json.dumps({'product':product,'valid_time':ingest.iso(t),
                            'exception_type':type(exc).__name__,'error':str(exc)},indent=2)+'\n')
                except OSError:pass
    if a.inspect_grid:
        LOG.info('Verification GRID INSPECTION %s',summary)
        return 1 if summary['failed'] else 0
    if not a.no_local_cleanup:
        summary['pruned_stores']=convert.prune(out,a.retention_days,now,policy.analysis_rules)
        summary['pruned_staging_days']=ingest.cleanup(stage,now,a.retention_days,policy.analysis_rules)
        summary['pruned_archives']=cleanup_analysis_archives(work,a.retention_days,now,policy.analysis_rules)
    count=convert.build_catalog(out)
    LOG.info('catalog_analysis.json: %d datasets',count)
    if not a.no_publish:
        catalog=out/'catalog_analysis.json'
        state_path=out/'.analysis_publish_state.json'
        try:
            state=json.loads(state_path.read_text()) if state_path.is_file() else {}
            if not isinstance(state,dict):state={}
        except Exception:state={}
        fresh={}
        dslist=json.loads(catalog.read_text())['datasets']
        errors=[]
        for entry in dslist:
            store=out/entry['path'];key=entry['id']
            if not completed_store(store):
                errors.append(key+': incomplete local store')
                continue
            digest=checksum(store/'.conversion.json')
            # Do not assume success means remote file persists forever; allow
            # --force-publish to reconstruct a missing remote installation.
            if a.force_publish or state.get(key)!=digest:
                try:
                    publish_store(store,None,work,a.publish_host,a.publish_dir,
                                  a.keep_publish_archive,store_arcname='analysis/'+store.name)
                    summary['published']+=1
                except Exception as exc:
                    summary['publish_failed']+=1
                    errors.append(key+': '+str(exc))
                    LOG.error('Failed publishing %s: %s',key,exc)
                    continue
            fresh[key]=digest
            if not a.no_local_cleanup:
                atomic_state(state_path,{**state,**fresh})
        if errors:
            LOG.error('Skipping analysis catalog publication because stores failed: %s',errors)
        else:
            try:
                publish_catalog(catalog,a.publish_host,a.publish_dir,a.retention_days,
                                cutoff=now-dt.timedelta(days=a.retention_days),
                                analysis_rules=policy.analysis_rules)
                atomic_state(state_path,fresh)
                LOG.info('Published catalog_analysis.json LAST')
            except Exception as exc:
                summary['publish_failed']+=1
                LOG.error('Failed publishing analysis catalog: %s',exc)
        # Expired publication TARs from an aborted run can be removed conservatively.
    LOG.info('Verification summary %s',summary)
    return 1 if summary['failed'] or summary['publish_failed'] else 0


def main(argv=None, *, publish_store, validate_publisher, default_publish_host=None, default_publish_dir=None):
    p=argparse.ArgumentParser(description='Hourly verification mode of operational multi-model converter')
    p.add_argument('--output-root',required=True)
    p.add_argument('--work-root',required=True)
    p.add_argument('--config',type=Path,default=Path(__file__).with_name('verification_sources.json'))
    p.add_argument('--source',choices=('all','rap','urma','rtma','mrms','nohrsc'),default='all')
    p.add_argument('--wgrib2',default=shutil.which('wgrib2'))
    p.add_argument('--lookback-hours',type=int,default=20)
    p.add_argument('--start-hour',help='UTC first valid hour YYYYMMDDHH (case study backfill)')
    p.add_argument('--end-hour',help='UTC final valid hour YYYYMMDDHH inclusive (max 168 hours)')
    p.add_argument('--nohrsc-lookback-hours',type=int,default=72,
                   help='Extra recovery window for native NOHRSC 6/24-h products (default 72 h)')
    p.add_argument('--mrms-stride',type=int,default=1)
    p.add_argument('--retention-days',type=int,default=7)
    p.add_argument('--protected-cycles-file',type=Path,
                   default=Path(__file__).with_name('protected_cycles.json'),
                   help='Shared model/analysis permanent-retention whitelist')
    p.add_argument('--compression-level',type=int,default=3)
    p.add_argument('--timeout',type=int,default=120)
    p.add_argument('--retries',type=int,default=3)
    p.add_argument('--publish-host',default=default_publish_host)
    p.add_argument('--publish-dir',default=default_publish_dir)
    p.add_argument('--no-publish',action='store_true')
    p.add_argument('--force',action='store_true',help='Redownload and reconvert hours')
    p.add_argument('--force-publish',action='store_true',help='Reupload stores even if tracked as published')
    p.add_argument('--keep-grib',action='store_true')
    p.add_argument('--keep-publish-archive',action='store_true')
    p.add_argument('--no-local-cleanup',action='store_true')
    p.add_argument('--inspect-grid',action='store_true',help='Inspect downloaded GRIB grids without conversion/publishing')
    p.add_argument('-v','--verbose',action='store_true')
    a=p.parse_args(argv)
    if (a.start_hour is None)!=(a.end_hour is None):
        p.error('--start-hour and --end-hour must be supplied together')
    if a.start_hour:
        try:
            a_start=dt.datetime.strptime(a.start_hour,'%Y%m%d%H').replace(tzinfo=UTC)
            a_end=dt.datetime.strptime(a.end_hour,'%Y%m%d%H').replace(tzinfo=UTC)
        except ValueError:
            p.error('--start-hour/--end-hour must be valid YYYYMMDDHH UTC times')
        if a_end<a_start or a_end-a_start>dt.timedelta(hours=167):
            p.error('Explicit backfill must span 1–168 consecutive UTC hours')
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format='%(asctime)s %(processName)s %(levelname)s %(message)s')
    if a.retention_days<1 or a.retention_days>366:p.error('retention-days must be 1..366')
    if a.compression_level not in range(10):p.error('compression-level must be 0..9')
    return_code=run(a,publish_store,validate_publisher)
    if return_code:raise SystemExit(return_code)
