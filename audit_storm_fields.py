#!/usr/bin/env python3
"""Read-only audit of native storm motion and 0–1/0–6 km shear for one case.

Compares model median/p90/p99 scalar shear (knots) and checks that derived
scalar values match the stored stored U/V components, accounting for their
possible s^-1 gradient or m/s-difference encoding. This does not establish
which forecast is most accurate; a reference wind profile or an independent
analysis is needed for that assessment. No network, writes or publication.
"""
import argparse
from array import array
import json
import math
from pathlib import Path
import statistics
import zlib

MODELS = ('rrfs', 'refs_mean', 'refs_m001', 'refs_m002', 'refs_m003',
          'refs_m004', 'refs_m005', 'hrrr')
MPS_TO_KT = 1.94384449244


def read_chunk(store, var, hour):
    chunk = store / var / f'{hour}.0.0'
    if not chunk.is_file():
        return None
    vals = array('f')
    try:
        vals.frombytes(zlib.decompress(chunk.read_bytes()))
    except (zlib.error, ValueError, OSError) as exc:
        print(f'      WARNING: unreadable {chunk}: {exc}')
        return None
    return vals


def percentile(vals, fraction):
    if not vals:
        return float('nan')
    return vals[min(len(vals) - 1, int((len(vals) - 1) * fraction))]


def finite_sample(vals, stride=1):
    if vals is None:
        return []
    return sorted(float(v) for v in vals[::max(1, stride)]
                  if math.isfinite(v) and -1e8 < v < 1e8)


def summarize_shear(store, hour, suffix):
    scalar = read_chunk(store, 'shear'+suffix, hour)
    uc = read_chunk(store, 'shru'+suffix, hour)
    vc = read_chunk(store, 'shrv'+suffix, hour)
    if scalar is None:
        return 'missing scalar shear'+suffix
    sample = finite_sample(scalar, max(1, len(scalar)//10000))
    if not sample:
        return 'scalar shear'+suffix+': no finite values'
    result = (f'shear{suffix}: median {percentile(sample,.50)*MPS_TO_KT:.1f} kt, '
              f'p90 {percentile(sample,.90)*MPS_TO_KT:.1f} kt, '
              f'p99 {percentile(sample,.99)*MPS_TO_KT:.1f} kt')
    if uc is None or vc is None:
        return result + ' (component U/V missing)'
    if len(scalar) != len(uc) or len(uc) != len(vc):
        return result + ' (WARNING: component/scalar array-size mismatch)'
    ratios = []
    rawmagnitudes = []
    step=max(1,len(scalar)//10000)
    for i in range(0,len(scalar),step):
        s,u,v=scalar[i],uc[i],vc[i]
        if all(math.isfinite(x) and abs(x)<1e8 for x in (s,u,v)):
            raw=math.hypot(u,v)
            if .00001 < raw and 0 < s < 250:
                rawmagnitudes.append(raw)
                ratios.append(s/raw)
    if ratios:
        ratios.sort()
        rawmagnitudes.sort()
        ratio=percentile(ratios,.5)
        units='m/s difference' if .8 < ratio < 1.2 else (
              's^-1 gradient × '+('1000 m' if suffix=='01' else '6000 m')
              if .8*int(suffix)*1000 < ratio < 1.2*int(suffix)*1000 else 'UNEXPECTED RATIO')
        result+=(f' | stored-component median {percentile(rawmagnitudes,.5):.5g}, '
                 f'scalar/component ratio {ratio:.3g} ({units})')
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',default='zarr',help='local Zarr output directory')
    p.add_argument('--cycle',default='2026102812',help='initialization YYYYMMDDHH')
    p.add_argument('--hour',type=int,default=12,help='forecast lead hour to audit')
    a=p.parse_args()
    if len(a.cycle)!=10 or not a.cycle.isdigit():p.error('--cycle must be YYYYMMDDHH')
    root=Path(a.root).expanduser()
    for model in MODELS:
        store=root/f'{model}_{a.cycle[:8]}_{a.cycle[8:]}00.zarr'
        print('\n'+model+' F'+str(a.hour).zfill(3)+':',end=' ')
        if not store.is_dir():
            print('store unavailable');continue
        u=read_chunk(store,'ustm',a.hour)
        v=read_chunk(store,'vstm',a.hour)
        print('right-mover '+('PRESENT' if u is not None and v is not None else
              'MISSING (USTM='+('yes' if u is not None else 'no')+
              ', VSTM='+('yes' if v is not None else 'no')+')'))
        if u is not None and v is not None:
            magnitudes=sorted(math.hypot(x,y)*MPS_TO_KT for x,y in
                zip(u[::max(1,len(u)//10000)], v[::max(1,len(v)//10000)])
                if math.isfinite(x) and math.isfinite(y) and abs(x)<1e8 and abs(y)<1e8)
            if magnitudes:
                print(f'    storm motion median {percentile(magnitudes,.5):.1f} kt, '
                      f'p90 {percentile(magnitudes,.9):.1f} kt')
        for suffix in ('01','06'):
            print('   ',summarize_shear(store,a.hour,suffix))
        manifest=root/f'{model}_{a.cycle[:8]}_{a.cycle[8:]}00.selection.json'
        if manifest.is_file():
            try:
                h=json.loads(manifest.read_text()).get('forecast_hours',{}).get(f'{a.hour:03d}',{})
                selections=h.get('selections',{})
                if h.get('shear01_orientation'):
                    print('    shear01 orientation:',h['shear01_orientation'])
                if h.get('shear01_revision') is not None:
                    print('    shear01 conversion revision:',h['shear01_revision'])
                for name in ('ustm','vstm','shru01','shrv01'):
                    if name in selections:
                        print(f'    native {name}: {selections[name].get("inventory", "unknown")}')
            except (ValueError,OSError,TypeError):
                pass
    print('\nINTERPRETATION: ratios near 1 mean stored U/V are wind differences in m/s;')
    print('ratios near 1000 or 6000 indicate native 1/s gradients scaled to m/s.')
    print('A mismatch, unusually large speed, or divergent HRRR/RRFS median is a')
    print('reason to inspect original GRIB2 units/values, NOT proof any model is wrong.')
    print('RAP analysis is intentionally not included in these model Zarr comparisons;')
    print('the currently ingested awp130pgrb RAP feed often has only 0–6 km shear.')

if __name__=='__main__':main()
