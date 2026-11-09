#!/usr/bin/env python3
"""Strict, shared model/verification permanent-retention configuration.

Operational JSON file remains local and is never shipped with release archives.
Each range is an inclusive UTC valid-hour interval. Source names expand to
individual analysis store products before the rules reach remote storage.
"""
from __future__ import annotations
import datetime as dt
import json
from pathlib import Path
import re
from typing import NamedTuple

MODELS = ('rrfs', 'refs_mean', 'refs_m001', 'refs_m002', 'refs_m003',
          'refs_m004', 'refs_m005', 'hrrr')
GFS_MODELS = ('gfs_v16', 'gfs_v17')
AI_MODELS = ('aigfs', 'aigefs_mean', 'hgefs_mean', 'gefs_mean', 'gefs_mean_p25')
GLOBAL_MODELS = GFS_MODELS + AI_MODELS
ALL_PROTECTED_MODELS = MODELS + GLOBAL_MODELS
SOURCE_PRODUCTS = {
    'mrms': ('mrms_refc', 'mrms_prate', 'mrms_apcp','mrms_apcp_pass2','mrms_apcp_6h','mrms_apcp_24h','mrms_rotation_ml60','gfs_mrms_apcp','gfs_mrms_apcp_pass2','gfs_mrms_apcp_6h','gfs_mrms_apcp_24h','gfs_mrms_prate'),
    'stageiv': ('stageiv_apcp','stageiv_6h','stageiv_24h','gfs_stageiv_apcp','gfs_stageiv_6h','gfs_stageiv_24h'),
    'rap': ('rap_surface', 'rap_pressure'),
    'urma': ('urma',),
    'rtma': ('rtma',),
    'nohrsc': ('nohrsc_6h','nohrsc_24h','nohrsc_48h','nohrsc_72h','gfs_nohrsc_6h','gfs_nohrsc_24h'),
    'ccpa': ('ccpa_1h','ccpa_3h','ccpa_6h','gfs_ccpa_1h','gfs_ccpa_3h','gfs_ccpa_6h'),
}
ALL_PRODUCTS = frozenset(p for products in SOURCE_PRODUCTS.values() for p in products)
STORE_RE = re.compile(r'^analysis_(mrms_refc|mrms_prate|mrms_apcp|mrms_rotation_ml60|stageiv_apcp|rap_surface|rap_pressure|urma|rtma|nohrsc_6h|nohrsc_24h|nohrsc_48h|nohrsc_72h|ccpa_1h|ccpa_3h|ccpa_6h|stageiv_6h|stageiv_24h|mrms_apcp_pass2|mrms_apcp_6h|mrms_apcp_24h|gfs_nohrsc_6h|gfs_nohrsc_24h|gfs_ccpa_1h|gfs_ccpa_3h|gfs_ccpa_6h|gfs_stageiv_6h|gfs_stageiv_24h|gfs_mrms_apcp_pass2|gfs_mrms_apcp_6h|gfs_mrms_apcp_24h|gfs_stageiv_apcp|gfs_mrms_apcp|gfs_mrms_prate)_(\d{10})\.zarr$')
DATE_RE = re.compile(r'^\d{8}$')
HOUR_RE = re.compile(r'^\d{10}$')
CYCLE_RE = re.compile(r'^(\d{8})_(00|06|12|18)$')
UTC = dt.timezone.utc
# Protect the verifying analysis for every saved model cycle, F000 through F084.
CYCLE_ANALYSIS_FHR = 84
GFS_CASE_VALID_FHR = 240
ALL_PRODUCT_TUPLE = tuple(sorted(ALL_PRODUCTS))


class ProtectionPolicy(NamedTuple):
    model_stores: frozenset[str]
    analysis_rules: tuple[tuple[str, str, tuple[str, ...]], ...]


def _hour(value: str) -> dt.datetime:
    if not isinstance(value, str) or not HOUR_RE.fullmatch(value):
        raise ValueError(f'Invalid UTC valid-hour YYYYMMDDHH: {value!r}')
    try:
        return dt.datetime.strptime(value, '%Y%m%d%H').replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(f'Invalid UTC valid-hour YYYYMMDDHH: {value!r}') from exc


def _cycle(value: str) -> dt.datetime:
    m = CYCLE_RE.fullmatch(value) if isinstance(value, str) else None
    if not m:
        raise ValueError(f'Invalid UTC cycle YYYYMMDD_CC: {value!r}')
    return _hour(''.join(m.groups()))


def _products(sources) -> tuple[str, ...]:
    if not isinstance(sources, list) or not sources or len(sources) != len(set(map(str, sources))):
        raise ValueError('Analysis sources must be a nonempty list without duplicates')
    if any(not isinstance(s, str) or (s != 'all' and s not in SOURCE_PRODUCTS and s not in ALL_PRODUCTS)
           for s in sources):
        raise ValueError(f'Invalid analysis sources: {sources!r}')
    if 'all' in sources and len(sources) != 1:
        raise ValueError('Use ["all"] alone, or list individual sources')
    result = set()
    for s in sources:
        if s == 'all': result.update(ALL_PRODUCTS)
        elif s in SOURCE_PRODUCTS: result.update(SOURCE_PRODUCTS[s])
        else: result.add(s)
    return tuple(sorted(result))


def load_policy(path: Path) -> ProtectionPolicy:
    """Fail closed for missing or invalid JSON; supports existing schema -1."""
    source = Path(path).expanduser()
    if source.is_symlink() or not source.is_file():
        raise ValueError(f'Protected-cycle configuration missing or unsafe: {source}')
    try:
        cfg = json.loads(source.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError(f'Cannot read protected-cycle configuration {source}: {exc}') from exc
    if (not isinstance(cfg, dict) or cfg.get('schema') != 'omd-evu-protected-cycles-1'
        or set(cfg) - {'schema', 'protected_cycles', 'protected_analysis'}
        or 'protected_cycles' not in cfg or not isinstance(cfg['protected_cycles'], list)
        or not isinstance(cfg.get('protected_analysis', []), list)):
        raise ValueError(f'Invalid retention configuration: {source}')
    models = set()
    rules = []
    for idx, entry in enumerate(cfg['protected_cycles']):
        if isinstance(entry, str):
            cyc = entry
            selected = ALL_PROTECTED_MODELS
        elif isinstance(entry, dict):
            if not {'date', 'cycle'} <= set(entry) or set(entry) - {'date', 'cycle', 'models', 'analysis_through_fhr', 'analysis_sources'}:
                raise ValueError(f'Invalid model-cycle keys at index {idx}')
            if not isinstance(entry['date'], str) or not isinstance(entry['cycle'], str):
                raise ValueError(f'Invalid model-cycle fields at index {idx}')
            cyc = entry['date'] + '_' + entry['cycle']
            selected = entry.get('models', list(ALL_PROTECTED_MODELS))
            if (not isinstance(selected, list) or not selected or
                any(not isinstance(m, str) or m not in ALL_PROTECTED_MODELS for m in selected)):
                raise ValueError(f'Invalid model list at index {idx}')
        else:
            raise ValueError(f'Invalid model-cycle entry at index {idx}')
        # A protected GEFS Mean case includes both native-resolution stores,
        # even when its existing whitelist only names 'gefs_mean'.
        if 'gefs_mean' in selected and 'gefs_mean_p25' not in selected:
            selected = tuple(selected) + ('gefs_mean_p25',)
        _cycle(cyc)
        date, hh = cyc.split('_')
        for model in selected:
            models.add(f'{model}_{date}_{hh}00.zarr')
        # A GFS comparison case also depends on v16 F000 at each verifying
        # synoptic valid time through F240. Protect these stores independently
        # without classifying them as separately saved forecast cases.
        if any(model in GLOBAL_MODELS for model in selected):
            origin = _cycle(cyc)
            for lead in range(0, GFS_CASE_VALID_FHR + 1, 6):
                instant = origin + dt.timedelta(hours=lead)
                models.add(f'gfs_v16_{instant:%Y%m%d_%H}00.zarr')
        # Older v6.6 whitelists may include explicit analysis_through_fhr and
        # analysis_sources options. Accept and validate them for compatibility,
        # but never let them restrict the now-mandatory F000–F084 / all-source
        # protection attached to every preserved forecast cycle.
        if isinstance(entry, dict):
            if 'analysis_sources' in entry and 'analysis_through_fhr' not in entry:
                raise ValueError(f'analysis_sources requires analysis_through_fhr at index {idx}')
            if 'analysis_through_fhr' in entry:
                lead = entry['analysis_through_fhr']
                if isinstance(lead, bool) or not isinstance(lead, int) or not 0 <= lead <= 744:
                    raise ValueError(f'Invalid legacy analysis_through_fhr at index {idx} (0..744)')
                if 'analysis_sources' in entry:
                    _products(entry['analysis_sources'])
        # Each saved GFS cycle covers 0–240 h; RRFS/REFS cycles cover 0–84 h.
        # Do not promote an RRFS-only case to 240 h or discard overlap rules.
        window = GFS_CASE_VALID_FHR if any(m in GLOBAL_MODELS for m in selected) else CYCLE_ANALYSIS_FHR
        begin = _cycle(cyc)
        finish = begin + dt.timedelta(hours=window)
        rules.append((begin.strftime('%Y%m%d%H'), finish.strftime('%Y%m%d%H'),
                      ALL_PRODUCT_TUPLE))

    for idx, entry in enumerate(cfg.get('protected_analysis', [])):
        if isinstance(entry, str):
            _hour(entry)
            start = end = entry
            products = tuple(sorted(ALL_PRODUCTS))
        elif isinstance(entry, dict):
            products = _products(entry.get('sources', ['all']))
            if 'cycle' in entry:
                if set(entry) - {'cycle', 'through_fhr', 'sources'} or 'through_fhr' not in entry:
                    raise ValueError(f'Invalid protected analysis cycle fields at index {idx}')
                begin = _cycle(entry['cycle'])
                lead = entry['through_fhr']
                if isinstance(lead, bool) or not isinstance(lead, int) or not 0 <= lead <= 744:
                    raise ValueError(f'Invalid through_fhr at index {idx} (0..744)')
                finish = begin + dt.timedelta(hours=lead)
                start, end = begin.strftime('%Y%m%d%H'), finish.strftime('%Y%m%d%H')
            else:
                if set(entry) - {'start', 'end', 'sources'} or 'start' not in entry or 'end' not in entry:
                    raise ValueError(f'Invalid protected analysis range fields at index {idx}')
                start, end = entry['start'], entry['end']
                begin, finish = _hour(start), _hour(end)
                if finish < begin or finish - begin > dt.timedelta(days=366):
                    raise ValueError(f'Protected analysis range out of bounds at index {idx}')
        else:
            raise ValueError(f'Invalid protected analysis entry at index {idx}')
        rules.append((start, end, products))
    return ProtectionPolicy(frozenset(models), tuple(rules))


def protects_analysis(store_name: str, analysis_rules) -> bool:
    """Check exact hourly Zarr store filename, without filesystem access."""
    match = STORE_RE.fullmatch(store_name)
    if not match:
        return False
    product, hour = match.groups()
    return any(start <= hour <= end and product in products
               for start, end, products in analysis_rules)


def rules_json(analysis_rules) -> str:
    return json.dumps([[start, end, list(products)] for start, end, products in analysis_rules], separators=(',', ':'))
