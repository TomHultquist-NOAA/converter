#!/usr/bin/env python3
"""Safely recompute existing bulk-shear Zarr chunks from stored U/V components.

The original code scaled all GRIB VUCSH/VVCSH values by layer depth. Some
operational RRFS/REFS products already contain wind differences in m/s, so
that produced implausibly large displayed speeds. The native components are
preserved in their Zarr arrays; this utility updates only 'shear01/shear06'.

First run without --execute to inspect. Pause model cron during --execute.
After completion, use the main converter --publish-only for the same cycle.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import json
import re
import zlib
import logging
import multi_model_to_zarr_wcoss2 as conv


def repair(store: Path, model: str, level: int = 3, execute: bool = False):
    results=[]
    for suffix,depth in (('01',1000),('06',6000)):
        udir,vdir=store/('shru'+suffix),store/('shrv'+suffix)
        if not udir.is_dir() or not vdir.is_dir():continue
        for source in sorted(udir.glob('*.0.0')):
            m=re.fullmatch(r'(\d+)\.0\.0',source.name)
            if not m or not (vdir/source.name).is_file():continue
            h=int(m.group(1));dest=store/('shear'+suffix)/source.name
            try:
                uraw=zlib.decompress(source.read_bytes())
                vraw=zlib.decompress((vdir/source.name).read_bytes())
                if len(uraw)!=len(vraw) or len(uraw)!=conv.core.FIELD_BYTES:
                    raise ValueError('unexpected component chunk length')
                corrected=conv.derive_bulk_shear_bytes(uraw,vraw,depth,model)
                unchanged=dest.is_file() and zlib.decompress(dest.read_bytes())==corrected
                if execute and not unchanged:
                    conv.core.write_compressed_chunk(dest,corrected,level)
                results.append((suffix,h,'unchanged' if unchanged else 'updated' if execute else 'would update'))
            except (OSError,ValueError,zlib.error) as exc:
                logging.error('%s %s F%03d: %s',store.name,suffix,h,exc)
                results.append((suffix,h,'ERROR'))
        if execute and any(kind==suffix and status=='updated' for kind,_,status in results):
            attrs_path=store/('shear'+suffix)/'.zattrs'
            if attrs_path.is_file():
                meta=json.loads(attrs_path.read_text())
                meta.update({'units':'m s-1','derivation':'native_grib_vector_shear_components_unit_aware_v2',
                             'note':'GRIB VUCSH/VVCSH may be wind difference (m/s) or vertical gradient (s-1); scale determined from source convention and sampled magnitudes'})
                conv.core.json_write(attrs_path,meta)
    return results


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--date',required=True,help='UTC YYYYMMDD')
    p.add_argument('--cycle',required=True,choices=['00','06','12','18'])
    p.add_argument('--models',default='all',help='all, or comma-separated model IDs')
    p.add_argument('--output-root',type=Path,default=Path('zarr'))
    p.add_argument('--compression-level',type=int,choices=range(10),default=3)
    p.add_argument('--execute',action='store_true',help='Apply correction; otherwise preview only')
    a=p.parse_args(argv)
    try:conv.iso_init(a.date,a.cycle)
    except ValueError:p.error('Invalid date')
    selected=list(conv.MODELS) if a.models=='all' else [x.strip() for x in a.models.split(',')]
    if not selected or len(selected)!=len(set(selected)) or any(x not in conv.MODELS for x in selected):p.error('Invalid models')
    failures=0;total=0
    for model in selected:
        store=a.output_root/f'{model}_{a.date}_{a.cycle}00.zarr'
        if not store.is_dir() or store.is_symlink():continue
        rows=repair(store,model,a.compression_level,a.execute)
        total+=sum(status=='updated' for _,_,status in rows)
        failures+=sum(status=='ERROR' for _,_,status in rows)
        print(f'{model}: {len(rows)} checked, {sum(status=="updated" for _,_,status in rows)} updated, {sum(status=="would update" for _,_,status in rows)} would update, {sum(status=="ERROR" for _,_,status in rows)} errors')
    print(('Applied' if a.execute else 'Previewed')+f' unit-aware bulk-shear repair; updated={total}, failures={failures}')
    return 1 if failures else 0

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(levelname)s: %(message)s')
    raise SystemExit(main())
