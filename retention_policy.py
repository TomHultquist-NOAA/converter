#!/usr/bin/env python3
"""Strict, shared model/verification permanent-retention configuration.

Operational JSON file is on WCOSS2, never shipped with release archives.
Each range is an inclusive UTC valid-hour interval. Source names expand to
individual analysis store products before the rules reach RZDM.
"""
from __future__ import annotations
import datetime as dt
import json
from pathlib import Path
import re
from typing import NamedTuple

MODELS = ('rrfs', 'refs_mean', 'refs_m001', 'refs_m002', 'refs_m003',
          'refs_m004', 'refs_m005', 'hrrr')
SOURCE_PRODUCTS = {
    'mrms': ('mrms_refc', 'mrms_prate', 'mrms_apcp'),
    'rap': ('rap_surface', 'rap_pressure'),
    'urma': ('urma',),
    'rtma': ('rtma',),
}
ALL_PRODUCTS = frozenset(p for products in SOURCE_PRODUCTS.values() for p in products)
STORE_RE = re.compile(r'^analysis_(mrms_refc|mrms_prate|mrms_apcp|rap_surface|rap_pressure|urma|rtma)_(\d{10})\.zarr$')
DATE_RE = re.compile(r'^\d{8}$')
HOUR_RE = re.compile(r'^\d{10}$')
CYCLE_RE = re.compile(r'^(\d{8})_(00|06|12|18)$')
UTC = dt.timezone.utc
# Protect the verifying analysis for every saved model cycle, F000 through F084.
CYCLE_ANALYSIS_FHR = 84
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
    seen_cycles = set()
    for idx, entry in enumerate(cfg['protected_cycles']):
        if isinstance(entry, str):
            cyc = entry
            selected = MODELS
        elif isinstance(entry, dict):
            if not {'date', 'cycle'} <= set(entry) or set(entry) - {'date', 'cycle', 'models', 'analysis_through_fhr', 'analysis_sources'}:
                raise ValueError(f'Invalid model-cycle keys at index {idx}')
            if not isinstance(entry['date'], str) or not isinstance(entry['cycle'], str):
                raise ValueError(f'Invalid model-cycle fields at index {idx}')
            cyc = entry['date'] + '_' + entry['cycle']
            selected = entry.get('models', list(MODELS))
            if (not isinstance(selected, list) or not selected or
                any(not isinstance(m, str) or m not in MODELS for m in selected)):
                raise ValueError(f'Invalid model list at index {idx}')
        else:
            raise ValueError(f'Invalid model-cycle entry at index {idx}')
        _cycle(cyc)
        date, hh = cyc.split('_')
        for model in selected:
            models.add(f'{model}_{date}_{hh}00.zarr')
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
        if cyc not in seen_cycles:
            seen_cycles.add(cyc)
            begin = _cycle(cyc)
            finish = begin + dt.timedelta(hours=CYCLE_ANALYSIS_FHR)
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
