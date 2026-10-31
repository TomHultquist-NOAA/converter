#!/usr/bin/env python3
"""Read-only local RAP and REFS Mean Zarr inventory/catalog/manifest audit.

Use from the converter directory, where the Zarr output is under ./zarr and
verification staging is under ./work/verification_staging:

    python3 audit_rap_case.py --root "$PWD/zarr" \
      --start-hour 2026102812 --end-hour 2026102812 \
      --refs-mean-cycle 2026102812 --refs-hour 1

This tool never fetches, converts, publishes, or deletes data.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

PRODUCTS = ('rap_pressure', 'rap_surface')
PREFERRED = {
    'rap_pressure': ('h300', 'u300', 'v300', 'wspd300', 'h500', 'u500', 'v500',
                     'absv500', 'sbcape', 'mlcape', 'mucape', 'srh01', 'srh03'),
    'rap_surface': ('mslp', 'snod', 'weasd'),
}
MEAN_FIELDS = ('h300', 'u300', 'v300', 'wspd300', 'h500', 'u500', 'v500',
               'absv500', 'sbcape', 'mlcape', 'mucape')


def parse_utc(s: str) -> dt.datetime:
    try:
        return dt.datetime.strptime(s, '%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('Use valid UTC hour YYYYMMDDHH') from exc


def read_json(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding='utf-8'))
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def load_catalog(path: Path):
    obj = read_json(path)
    entries = obj.get('datasets', [])
    if not isinstance(entries, list):
        entries = []
    return entries, obj.get('updated_at', obj.get('generated_utc', 'unavailable'))


def entry_for(entries, product: str, stamp: dt.datetime):
    wanted = stamp.strftime('%Y-%m-%dT%H:00:00')
    return next((e for e in entries if isinstance(e, dict) and e.get('product') == product
                 and str(e.get('init_time', '')).startswith(wanted)), None)


def available_catalog_fields(entry, hour: int):
    if not entry:
        return set()
    return {v.get('name') for v in entry.get('variables', [])
            if isinstance(v, dict) and v.get('name')
            and hour in v.get('available_hours', [])}


def available_store_fields(store: Path, hour: int):
    """Return fields with real Zarr v2 chunks; never infer a field from a dir."""
    if not store.is_dir() or store.is_symlink():
        return set()
    result = set()
    for child in store.iterdir():
        if (child.is_dir() and not child.is_symlink() and
                (child / '.zarray').is_file() and
                (child / f'{hour}.0.0').is_file() and
                (child / f'{hour}.0.0').stat().st_size > 0):
            result.add(child.name)
    return result


def show_fields(label, fields, expected, indent='  '):
    print(indent + label + ': ' + (','.join(sorted(fields)) or 'none'))
    if expected:
        missing = sorted(set(expected) - fields)
        print(indent + 'absent requested: ' + (','.join(missing) or 'none'))


def find_stage_manifest(work_root: Path, product: str, stamp: dt.datetime):
    t = stamp.strftime('%Y%m%d%H')
    return work_root / 'verification_staging' / product / stamp.strftime('%Y%m%d') / f'{product}_{t}.json'


def assess_rap(root: Path, work: Path, native, aligned, product: str, stamp: dt.datetime):
    suffix = stamp.strftime('%Y%m%d%H')
    base = f'analysis_{product}_{suffix}.zarr'
    nstore = root / 'analysis' / base
    astore = root / 'aligned' / base
    nentry = entry_for(native, product, stamp)
    aentry = entry_for(aligned, product, stamp)
    ns = available_store_fields(nstore, 0)
    nc = available_catalog_fields(nentry, 0)
    ass = available_store_fields(astore, 0)
    ac = available_catalog_fields(aentry, 0)
    print(f'{suffix} {product}: native_store={nstore.is_dir()} native_catalog={bool(nentry)} '
          f'aligned_store={astore.is_dir()} aligned_catalog={bool(aentry)}')
    print('  native fields: ' + (','.join(sorted(ns)) or 'none'))
    print('  native catalog fields: ' + (','.join(sorted(nc)) or 'none'))
    print('  aligned fields: ' + (','.join(sorted(ass)) or 'none'))
    print('  aligned catalog fields: ' + (','.join(sorted(ac)) or 'none'))
    print('  missing native target chunks: ' + (','.join(sorted(set(PREFERRED[product]) - ns)) or 'none'))
    if ns - nc:
        print('  WARNING native chunks absent from catalog: ' + ','.join(sorted(ns-nc)))
    if ass - ac:
        print('  WARNING aligned chunks absent from catalog: ' + ','.join(sorted(ass-ac)))
    if ns - ass:
        print('  WARNING native fields absent from aligned store: ' + ','.join(sorted(ns-ass)))
    conv = read_json(nstore / '.conversion.json')
    if conv:
        print('  conversion fields: ' + (','.join(conv.get('fields', [])) or 'none'))
    manifest_file = find_stage_manifest(work, product, stamp)
    manifest = read_json(manifest_file)
    if manifest:
        selected = sorted({s.get('field') for s in manifest.get('selected', [])
                           if isinstance(s, dict) and s.get('field')})
        print(f'  staging manifest: {manifest.get("status", "unknown")} '
              f'selected={",".join(selected) or "none"} '
              f'missing={",".join(manifest.get("missing", [])) or "none"}')
        staged_but_unconverted = sorted(set(selected) & set(PREFERRED[product]) - set(conv.get('fields', [])))
        if staged_but_unconverted:
            print('  STALE CONVERSION: staged target fields missing from conversion metadata: '
                  + ','.join(staged_but_unconverted))
            print('  The converter rechecks these fields when this hour is revisited '
                  'with v1.3.5 or later.')
        selector_config = read_json(Path(__file__).with_name('verification_sources.json'))
        expected = set(selector_config.get('fields', {}).get(product, {}))
        accounted_for = set(selected) | set(manifest.get('missing', []))
        unexamined = sorted(expected - accounted_for)
        if unexamined:
            print('  STALE MANIFEST: current selectors not evaluated: ' + ','.join(unexamined))
            print('  The v1.3.5 verification workflow will re-inventory this hour '
                  'when explicitly backfilled or included in its lookback window.')
        else:
            print('  Selector coverage: all current requested field keys accounted for '
                  '(selected or explicitly missing)')
    else:
        print('  staging manifest: not found at ' + str(manifest_file))
    return nstore.is_dir(), bool(nentry), astore.is_dir(), bool(aentry)


def assess_mean(root: Path, stamp: dt.datetime, hour: int):
    name = f'refs_mean_{stamp:%Y%m%d}_{stamp:%H}00.zarr'
    store = root / name
    records, updated = load_catalog(root / 'catalog_refs_mean.json')
    # Catalog models are human-readable, e.g. 'REFS Mean'; use the exact
    # normalized Zarr store path instead of comparing a display label to
    # the internal 'refs_mean' ID.
    entry = next((e for e in records if isinstance(e, dict) and
                  e.get('path') == name and
                  str(e.get('init_time', '')).startswith(stamp.strftime('%Y-%m-%dT%H'))), None)
    actual = available_store_fields(store, hour)
    exposed = available_catalog_fields(entry, hour)
    print(f'REFS Mean {stamp:%Y%m%d%H} F{hour:03d}: '
          f'local_store={store.is_dir()} catalog={bool(entry)} updated={updated}')
    print('  native chunks: ' + (','.join(sorted(set(MEAN_FIELDS) & actual)) or 'none'))
    print('  absent native targets: ' + (','.join(sorted(set(MEAN_FIELDS) - actual)) or 'none'))
    print('  catalog fields at hour: ' + (','.join(sorted(exposed)) or 'none'))
    if actual - exposed:
        print('  WARNING local chunks absent from mean catalog: ' + ','.join(sorted(actual-exposed)))
    if entry:
        print('  catalog display model: ' + str(entry.get('model', 'unknown')))
    else:
        print('  WARNING no catalog entry matches this store path and cycle; this does '
              'not prove the source model fields are missing')
    print('  Native REFS Mean 300-hPa availability must be verified from its source GRIB inventory.')
    return bool(entry)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', required=True, type=Path, help='Converter Zarr output directory')
    ap.add_argument('--work-root', type=Path, help='Converter work directory; defaults to sibling work/')
    ap.add_argument('--start-hour', type=parse_utc, default=parse_utc('2026102812'))
    ap.add_argument('--end-hour', type=parse_utc, default=parse_utc('2026102812'))
    ap.add_argument('--refs-mean-cycle', type=parse_utc)
    ap.add_argument('--refs-hour', type=int, default=1)
    args = ap.parse_args(argv)
    if args.end_hour < args.start_hour or args.end_hour-args.start_hour > dt.timedelta(hours=167):
        ap.error('UTC range must contain 1–168 consecutive hours')
    if not 1 <= args.refs_hour <= 60:
        ap.error('--refs-hour must be between 1 and 60')
    root = args.root.expanduser().resolve()
    work = (args.work_root or root.parent / 'work').expanduser().resolve()
    native, nupdated = load_catalog(root / 'catalog_analysis.json')
    aligned, aupdated = load_catalog(root / 'catalog_aligned_analysis.json')
    print('Read-only inventory audit, root:', root)
    print('Native catalog:', nupdated, '; aligned catalog:', aupdated)
    totals = dict(native_stores=0, native_catalog=0, aligned_stores=0, aligned_catalog=0)
    t = args.start_hour
    while t <= args.end_hour:
        for product in PRODUCTS:
            flags = assess_rap(root, work, native, aligned, product, t)
            for key, yes in zip(totals, flags):
                totals[key] += bool(yes)
        t += dt.timedelta(hours=1)
    if args.refs_mean_cycle:
        assess_mean(root, args.refs_mean_cycle, args.refs_hour)
    print('Summary:', totals)
    print('Read-only local results; check RZDM separately with rap_diagnostics.html.')
    print('Missing chunks are not automatically evidence that the source GRIB omitted those records.')


if __name__ == '__main__':
    main()
