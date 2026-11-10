#!/usr/bin/env python3
"""Incrementally materialize native-grid GFS differences from existing GFS Zarrs.

Analysis means GFS v16 F000 initialized at the *valid time*, not F000 of
an earlier cycle. When a corresponding F000 is not available, omit the chunk.
"""
from array import array
import datetime as dt
import json
from pathlib import Path
import sys
import zlib

import perf_monitor
PRODUCTS = {
    'gfs_diff_v17_v16': ('gfs_v17', 'gfs_v16'),
    'gfs_diff_v16_analysis': ('gfs_v16', 'analysis'),
    'gfs_diff_v17_analysis': ('gfs_v17', 'analysis'),
}
SKIP = {'ptype', 'crain', 'csnow', 'cfrzr', 'cicep', 'snowpresence',
        # Angular differences require circular arithmetic and direction vectors
        # are only for display; never publish misleading scalar subtractions.
        'wave_direction_primary','wave_direction_wind','wave_direction_swell1',
        'wave_u_primary','wave_v_primary','wave_u_wind','wave_v_wind',
        'wave_u_swell1','wave_v_swell1'}
COORDS = {'processed', 'valid_time', 'forecast_hour', 'latitude', 'longitude'}


def subtract_float_chunks(a, b):
    """Subtract float32 chunks, retaining NaN (and rejecting infinities)."""
    import math
    aa, bb = array('f'), array('f')
    aa.frombytes(zlib.decompress(a)); bb.frombytes(zlib.decompress(b))
    if sys.byteorder != 'little':
        aa.byteswap(); bb.byteswap()
    if len(aa) != len(bb):
        raise ValueError('Difference arrays have incompatible lengths')
    out = array('f', (x-y if math.isfinite(x) and math.isfinite(y) else float('nan') for x,y in zip(aa,bb)))
    if sys.byteorder != 'little': out.byteswap()
    return out.tobytes()


def valid_analysis_path(root, date, cycle, hour):
    init = dt.datetime.strptime(date+cycle, '%Y%m%d%H').replace(tzinfo=dt.timezone.utc)
    valid = init + dt.timedelta(hours=hour)
    # GFS analysis cycles are every 6 hours; intermediate leads remain missing.
    if valid.hour not in (0,6,12,18): return None
    return root / ('gfs_v16_' + valid.strftime('%Y%m%d_%H') + '00.zarr')


def fields(store):
    if not store.is_dir(): return set()
    return {p.name for p in store.iterdir() if p.is_dir() and p.name not in COORDS|SKIP
            and (p/'.zarray').is_file() and json.loads((p/'.zarray').read_text()).get('dtype') == '<f4'}


def compatible(left, right):
    a = json.loads((left/'.zattrs').read_text()); b = json.loads((right/'.zattrs').read_text())
    keys = ['GRIB_Nx','GRIB_Ny','GRIB_DxInDegrees','GRIB_DyInDegrees',
            'GRIB_longitudeOfFirstGridPointInDegrees','GRIB_latitudeOfFirstGridPointInDegrees',
            'grid_order']
    return all(a.get(k)==b.get(k) for k in keys)


@perf_monitor.timed_task('gfs_difference_materialization', lambda a,k: '%s%s' % (a[1], a[2]))
def compute(root, date, cycle, hours, compression=3, *, force=False):
    """Return stores changed; safe to retry when one source arrives later."""
    import gfs_to_zarr as gfs
    root = Path(root)
    touched = []
    for product, (first, second) in PRODUCTS.items():
        source = root / f'{first}_{date}_{cycle}00.zarr'
        if not source.is_dir(): continue
        output = root / f'{product}_{date}_{cycle}00.zarr'
        changed = False
        for hour in hours:
            if hour < 0 or hour > 240: continue
            base = (valid_analysis_path(root,date,cycle,hour) if second=='analysis'
                    else root / f'{second}_{date}_{cycle}00.zarr')
            if base is None or not base.is_dir() or not compatible(source,base): continue
            if not (source/'processed'/'0').exists() or not (base/'processed'/'0').exists(): continue
            if not zlib.decompress((source/'processed'/'0').read_bytes())[hour]: continue
            other_hour = 0 if second=='analysis' else hour
            if not zlib.decompress((base/'processed'/'0').read_bytes())[other_hour]: continue
            names = fields(source)&fields(base)
            for name in sorted(names):
                left, right = source/name/f'{hour}.0.0', base/name/f'{other_hour}.0.0'
                if not (left.is_file() and right.is_file()): continue
                left_meta = json.loads((source/name/'.zarray').read_text())
                right_meta = json.loads((base/name/'.zarray').read_text())
                if (left_meta['shape'][1:] != right_meta['shape'][1:] or
                    left_meta['chunks'] != right_meta['chunks'] or
                    left_meta.get('compressor',{}).get('id')!='zlib' or
                    right_meta.get('compressor',{}).get('id')!='zlib'):
                    continue
                target = output/name/f'{hour}.0.0'
                if target.exists() and not force: continue
                if not output.exists():
                    gfs.create_store(output, 'gfs_v16', date, cycle, compression)
                    attrs=json.loads((output/'.zattrs').read_text())
                    attrs.update(model=product.replace('_',' '), model_id=product,
                                 source='Precomputed GFS native-grid difference',
                                 difference={'left':first,'right':second,'analysis_reference':'valid_time_F000' if second=='analysis' else None})
                    gfs.core.json_write(output/'.zattrs',attrs)
                attrs = json.loads((source/name/'.zattrs').read_text())
                attrs.update(long_name=attrs.get('long_name',name)+' difference', provenance=first+' minus '+second)
                gfs.core.ensure_array(output,name,(241,gfs.NY,gfs.NX),(1,gfs.NY,gfs.NX),'<f4','NaN',attrs,compression)
                raw = subtract_float_chunks(left.read_bytes(),right.read_bytes())
                gfs.core.write_compressed_chunk(target,raw,compression)
                changed=True
        if changed:
            # A lead is processed when at least one difference variable is present.
            status = bytearray(gfs.read_status(output))
            for fld in fields(output):
                for chunk in (output/fld).glob('*.0.0'):
                    h=int(chunk.name.split('.')[0]);status[h]=1
            gfs.shared.write_processed(output,status,compression)
            touched.append(output)
    return touched
