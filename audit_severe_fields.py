#!/usr/bin/env python3
"""Inspect GRIB2 remote .idx files for native SCP/STP records, without downloading GRIB2.

The standardized code table defines SCCP, STPC, and SIGT but not every
operational model or product necessarily emits them. Do not fabricate fields.
"""
from __future__ import annotations
import argparse
import sys
import urllib.error
import multi_model_to_zarr_wcoss2 as conv

TARGETS={'scp','stp','stp_fixed'}

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--date',required=True)
    p.add_argument('--cycle',choices=['00','06','12','18'],required=True)
    p.add_argument('--hour',type=int,default=12)
    p.add_argument('--models',default='all')
    a=p.parse_args(argv)
    try:conv.iso_init(a.date,a.cycle)
    except ValueError:p.error('Invalid date')
    models=conv.MODELS if a.models=='all' else tuple(x.strip() for x in a.models.split(','))
    if not models or any(model not in conv.MODELS for model in models):p.error('Invalid models')
    client=conv.core.HTTPClient(timeout=20,retries=1,insecure=False)
    for model in models:
        if a.hour<hour_lower(model) or a.hour>conv.hour_bounds(model)[1]:continue
        print(f'\n{model} {a.date} {a.cycle} F{a.hour:03d}')
        found=set()
        for product in conv.PRODUCTS[conv.family(model)]:
            url,idx=conv.file_urls(model,a.date,a.cycle,a.hour,product)
            try:
                rows=conv.core.parse_idx(client.text(idx),client.size(url))
            except (urllib.error.HTTPError,OSError,ValueError) as exc:
                print(f' {product}: unavailable ({type(exc).__name__})');continue
            for name in TARGETS:
                r=conv.choose_field(rows,name,a.hour)
                if r:
                    print(f' {product} {name}: {r["text"]}')
                    found.add(name)
        for name in sorted(TARGETS-found):print(f' {name}: not present in configured products')
    return 0

def hour_lower(model):return conv.hour_bounds(model)[0]

if __name__=='__main__':sys.exit(main())
