#!/usr/bin/env python3
"""Read-only inventory of native and aligned verification products by valid hour.

For WCOSS2 use: python3 audit_analysis_alignment.py --root "$PWD/zarr" --hours 24
No data processing, SSH, deletion, or publication is performed.
"""
import argparse
import datetime as dt
import json
from pathlib import Path

PRODUCTS=['mrms_refc','mrms_prate','mrms_apcp','rap_surface','rap_pressure','rtma','urma']
FIELDS={'mrms_refc':'refc','mrms_prate':'prate','mrms_apcp':'apcp',
        'rap_surface':'snod','rap_pressure':'t850','rtma':'t2m','urma':'t2m'}

def inventory(root,subdir,product,when,field):
    base=root/subdir/('analysis_%s_%s.zarr'%(product,when))
    if not base.is_dir():return '—'
    chunk=base/field/'0.0.0'
    return 'OK' if chunk.is_file() else 'incomplete'

def catalog_inventory(root,product,when,field):
    p=root/'catalog_aligned_analysis.json'
    if not p.is_file():return 'no catalog'
    cat=json.loads(p.read_text())
    matching=[e for e in cat.get('datasets',[]) if e.get('product')==product and
             str(e.get('init_time','')).replace('-','').replace(':','').replace('T','')[:10]==when]
    if not matching:return 'not indexed'
    if not any(v.get('name')==field for e in matching for v in e.get('variables',[])):
        return 'field missing'
    return 'OK'

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('zarr'))
    p.add_argument('--hours',type=int,default=24)
    p.add_argument('--products',default='rtma,urma,mrms_refc,rap_pressure')
    a=p.parse_args()
    products=a.products.split(',')
    if not (1<=a.hours<=336) or any(q not in PRODUCTS for q in products):p.error('Invalid hours or products')
    now=dt.datetime.now(dt.timezone.utc).replace(minute=0,second=0,microsecond=0)
    print('Source inventory; UTC valid hour; model and RZDM status NOT tested')
    print('UTC hour     product       field     native     aligned    catalog')
    for i in range(a.hours):
        stamp=(now-dt.timedelta(hours=i)).strftime('%Y%m%d%H')
        for product in products:
            field=FIELDS[product]
            print('%-12s %-13s %-9s %-10s %-10s %s'%(stamp,product,field,
                inventory(a.root,'analysis',product,stamp,field),
                inventory(a.root,'aligned',product,stamp,field),
                catalog_inventory(a.root,product,stamp,field)))
if __name__=='__main__':main()
