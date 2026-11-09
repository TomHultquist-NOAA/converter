#!/usr/bin/env python3
"""Convert staged verification GRIB2 messages into browser-compatible Zarr v2.

Python standard library + installed wgrib2 only. Outputs independent one-hour
stores retaining distinct georeferencing for RAP, URMA and MRMS. No GRIB
projection is silently assumed to be RRFS's Lambert grid.
"""
from __future__ import annotations
import argparse
from array import array
from datetime import datetime, timezone, timedelta
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import perf_monitor
import tempfile
import time
import zlib
import rap_vertical_diagnostics as rapvd

LOG=logging.getLogger('verify-convert')
UTC=timezone.utc
STAGED_PRODUCTS=['mrms_refc','mrms_prate','mrms_apcp', 'mrms_rotation_ml60','stageiv_apcp','urma','rtma','rap_surface','rap_pressure',
                 'nohrsc_6h','nohrsc_24h','nohrsc_48h','nohrsc_72h','ccpa_1h','ccpa_3h','ccpa_6h','stageiv_6h','stageiv_24h','mrms_apcp_6h','mrms_apcp_24h','mrms_apcp_pass2']
FIELD_UNITS={'refc':'dBZ','prate':'kg m-2 s-1','apcp':'kg m-2','t2m':'K','dpt2m':'K',
             'rottrack_ml60':'0.001 s-1','rh2m':'%','wspd10':'m s-1','gust':'m s-1','u10':'m s-1','v10':'m s-1',
             'weasd':'kg m-2','snod':'m','snowpresence':'1','hsfc':'gpm',
             'snow6h':'m','snow24h':'m','snow48h':'m','snow72h':'m','mslp':'Pa',
             't925':'K','t850':'K','t700':'K','h925':'gpm','h850':'gpm','h700':'gpm','h500':'gpm','h300':'gpm','u300':'m s-1','v300':'m s-1','wspd300':'m s-1',
             'u925':'m s-1','u850':'m s-1','u700':'m s-1','u500':'m s-1','u600':'m s-1','u400':'m s-1','h600':'gpm','h400':'gpm',
             'v925':'m s-1','v850':'m s-1','v700':'m s-1','v500':'m s-1','v600':'m s-1','v400':'m s-1',
             'absv500':'s-1','srh01':'m2 s-2','srh03':'m2 s-2',
             'shru01':'m s-1','shrv01':'m s-1','shear01':'m s-1',
             'shru06':'m s-1','shrv06':'m s-1','shear06':'m s-1',
             'ustm':'m s-1','vstm':'m s-1',
             'sbcape':'J kg-1','mlcape':'J kg-1','mucape':'J kg-1'}

def atomic_json(p,v):
 p.parent.mkdir(parents=True,exist_ok=True)
 t=p.with_name('.'+p.name+'.tmp')
 with t.open('w') as f:json.dump(v,f,indent=2,sort_keys=True,allow_nan=False);f.write('\n')
 os.replace(t,p)

def run(wgrib2,*args):
 p=subprocess.run([wgrib2,*map(str,args)],capture_output=True,text=True)
 if p.returncode:raise RuntimeError('wgrib2 failed: '+p.stderr[-1200:])
 return p.stdout

def grid_details(wgrib2,filename,number=1):
 out=run(wgrib2,filename,'-d',str(number),'-grid')
 dims=re.search(r'\((\d+)\s*x\s*(\d+)\)',out)
 if not dims:raise ValueError('Unable to parse wgrib2 grid dimensions: '+out)
 nx,ny=map(int,dims.groups())
 if nx<2 or ny<2 or nx*ny>100_000_000:raise ValueError('Invalid/unreasonable grid size')
 if 'Lambert Conformal' in out:
  def numeric(label):
   m=re.search(r'\b'+label+r'\s+(-?\d+(?:\.\d+)?)',out,re.I)
   if not m:raise ValueError('Missing '+label+' in Lambert grid: '+out)
   return float(m.group(1))
  # On common wgrib2 releases -grid reports dx/dy in km, often without units;
  # older formats may report metres. Cross-check in the processing environment via --inspect-grid.
  dx=numeric('Dx');dy=numeric('Dy')
  if dx<=0 or dy<=0:raise ValueError('Nonpositive grid spacing')
  dx=dx*1000 if dx<100 else dx
  dy=dy*1000 if dy<100 else dy
  return {'GRIB_gridType':'lambert','GRIB_Nx':nx,'GRIB_Ny':ny,
   'GRIB_DxInMetres':dx,'GRIB_DyInMetres':dy,
   'GRIB_LoVInDegrees':norm_lon(numeric('LoV')),
   'GRIB_LaDInDegrees':numeric('LatD'),'GRIB_Latin1InDegrees':numeric('Latin1'),
   'GRIB_Latin2InDegrees':numeric('Latin2'),
   'GRIB_longitudeOfFirstGridPointInDegrees':norm_lon(numeric('Lon1')),
   'GRIB_latitudeOfFirstGridPointInDegrees':numeric('Lat1')}
 if 'lat-lon grid' in out.lower() or 'lat/lon grid' in out.lower():
  lat=re.search(r'\blat\s+(-?\d+(?:\.\d+)?)\s+to\s+(-?\d+(?:\.\d+)?)',out,re.I)
  lon=re.search(r'\blon\s+(-?\d+(?:\.\d+)?)\s+to\s+(-?\d+(?:\.\d+)?)',out,re.I)
  if not lat or not lon:raise ValueError('Could not parse lat/lon grid bounds: '+out)
  lat0,lat1=map(float,lat.groups());lon0,lon1=map(float,lon.groups())
  # Wrapping is retained as a continuous interval, essential near 180 deg.
  if lon1<lon0:lon1+=360
  return {'GRIB_gridType':'regular_ll','GRIB_Nx':nx,'GRIB_Ny':ny,
          'longitude_min':norm_lon(lon0),'longitude_max':norm_lon(lon0)+(lon1-lon0),
          'latitude_min':min(lat0,lat1),'latitude_max':max(lat0,lat1)}
 raise ValueError('Unsupported grid type; explicit reprojection required: '+out)

def norm_lon(lon):return ((lon+180)%360)-180

def decode(wgrib2,filename,number,nx,ny,tmp):
 run(wgrib2,filename,'-d',str(number),'-no_header','-order','we:sn',
     '-little_endian','-ieee',tmp)
 raw=Path(tmp).read_bytes()
 if len(raw)!=nx*ny*4:raise ValueError(f'GRIB decode length mismatch: {len(raw)} != {nx*ny*4}')
 arr=array('f');arr.frombytes(raw)
 if sys.byteorder!='little':arr.byteswap()
 return arr

def valid_float(v,field):
 if not math.isfinite(v) or abs(v)>1e15:return float('nan')
 # MRMS missing/not-available status codes (not meteorological zeros).
 if field=='refc' and v<=-90:return float('nan')
 if field=='rottrack_ml60' and v<0:return float('nan')
 if (field in ('prate','apcp') or field.startswith('snow')) and v<0:return float('nan')
 return v

def convert_units(field,source_name,arr):
 for i,v in enumerate(arr):
  v=valid_float(v,field)
  if field=='prate' and math.isfinite(v):
   # MRMS PrecipRate is mm/hour, browser convention is kg m-2 s-1.
   v=v/3600
  arr[i]=v
 return arr

def derive_rh(t,td):
 if len(t)!=len(td):raise ValueError('RH grids differ')
 result=array('f')
 for a,b in zip(t,td):
  if not (math.isfinite(a) and math.isfinite(b) and 120<a<350 and 120<b<350):
   result.append(float('nan'));continue
  c=a-273.15;d=b-273.15
  # Bolton saturation vapor pressure over liquid water, including subfreezing.
  result.append(max(0,min(100,100*math.exp(17.67*d/(d+243.5)-17.67*c/(c+243.5)))))
 return result

def normalize_bulk_shear_components(uc, vc, layer_depth_m):
 """Interpret native RAP VUCSH/VVCSH with paired, robust unit inference.

 Operational RAP products usually encode vertical gradients (s-1), but
 historical/alternate feeds can already contain m/s differences. Neither is
 safe to assume unconditionally. Normalize *both* retained components to m/s.
 """
 if len(uc)!=len(vc):raise ValueError('Shear component grids differ')
 step=max(1,len(uc)//2048)
 sample=sorted(math.hypot(uc[i],vc[i]) for i in range(0,len(uc),step)
               if math.isfinite(uc[i]) and math.isfinite(vc[i]) and
               abs(uc[i])<1e9 and abs(vc[i])<1e9)
 if not sample:raise ValueError('No valid RAP bulk-shear components')
 median=sample[len(sample)//2]
 # 0.03-0.2 is indeterminate: fail rather than publish deceptive values.
 if median>=0.2:scale=1.0;source_units='m s-1'
 elif median<=0.03:scale=float(layer_depth_m);source_units='s-1'
 else:raise ValueError(f'Ambiguous RAP shear component units: median={median:.5g}')
 uout=array('f');vout=array('f')
 for u,v in zip(uc,vc):
  if not (math.isfinite(u) and math.isfinite(v)) or abs(u)>=1e9 or abs(v)>=1e9:
   uout.append(float('nan'));vout.append(float('nan'));continue
  su,sv=u*scale,v*scale
  if math.hypot(su,sv)>125:
   uout.append(float('nan'));vout.append(float('nan'))
  else:uout.append(su);vout.append(sv)
 LOG.info('RAP 0-%dm shear components: median=%.6g, inferred=%s, multiplier=%g',
          layer_depth_m,median,source_units,scale)
 return uout,vout

def derive_bulk_shear(uc,vc,layer_depth_m):
 u,v=normalize_bulk_shear_components(uc,vc,layer_depth_m)
 return array('f',(math.hypot(a,b) if math.isfinite(a) and math.isfinite(b)
                  else float('nan') for a,b in zip(u,v)))

def derive_snowpresence(depth,threshold=0.01):
 return array('f',(float('nan') if not math.isfinite(v) else (1. if v>=threshold-1e-8 else 0.) for v in depth))

def stride_array(arr,nx,ny,stride):
 if stride==1:return arr,nx,ny
 out=array('f')
 for y in range(0,ny,stride):out.extend(arr[y*nx+x] for x in range(0,nx,stride))
 return out,(nx+stride-1)//stride,(ny+stride-1)//stride

def strided_grid(g,stride):
 if stride==1:return g
 g=dict(g);nx,ny=g['GRIB_Nx'],g['GRIB_Ny']
 g['GRIB_Nx']=(nx+stride-1)//stride;g['GRIB_Ny']=(ny+stride-1)//stride
 if g['GRIB_gridType']=='regular_ll':
  # Preserve first point; final point is actual selected original grid position.
  dx=(g['longitude_max']-g['longitude_min'])/(nx-1)
  dy=(g['latitude_max']-g['latitude_min'])/(ny-1)
  g['longitude_max']=g['longitude_min']+min(nx-1,(g['GRIB_Nx']-1)*stride)*dx
  g['latitude_max']=g['latitude_min']+min(ny-1,(g['GRIB_Ny']-1)*stride)*dy
 else:
  g['GRIB_DxInMetres']*=stride;g['GRIB_DyInMetres']*=stride
 g['sampling_stride']=stride;return g

def write_array(zroot,name,arr,valid_time,info,compression):
 nx=zroot['GRIB_Nx'];ny=zroot['GRIB_Ny']
 folder=Path(zroot['path'])/name;folder.mkdir(parents=True)
 atomic_json(folder/'.zarray',{'zarr_format':2,'shape':[1,ny,nx], 'chunks':[1,ny,nx],
   'dtype':'<f4','compressor':{'id':'zlib','level':compression},
   'fill_value':'NaN','order':'C','filters':None,'dimension_separator':'.'})
 atomic_json(folder/'.zattrs',{'_ARRAY_DIMENSIONS':['valid_time','y','x'],
    'units':FIELD_UNITS.get(name,''),**info})
 if sys.byteorder!='little':arr.byteswap()
 raw=arr.tobytes()
 compress_started=time.monotonic()
 compressed=zlib.compress(raw,compression)
 perf_monitor.add_metric('zlib_compress_seconds',time.monotonic()-compress_started)
 perf_monitor.add_metric('zlib_input_bytes',len(raw))
 perf_monitor.add_metric('zlib_output_bytes',len(compressed))
 write_started=time.monotonic()
 (folder/'0.0.0').write_bytes(compressed)
 perf_monitor.add_metric('zarr_chunk_write_seconds',time.monotonic()-write_started)

def nohrsc_depth_unit_from_verbose(info):
 """Return validated NOHRSC GRIB snowfall depth unit from wgrib2 -V.

 wgrib2 -V normally describes the variable as e.g. ``SNOW Snowfall [m]``;
 it does not provide a ``units=...`` key. Restrict parsing to the SNOW/
 ASNOW variable description, not incidental unrelated metadata. Unknown
 dimensional units (including water-equivalent mass units) remain fatal.
 """
 header=info.splitlines()[0] if info.splitlines() else ''
 m=re.search(r':\s*(?:SNOW|ASNOW)\b[^\r\n]*?\[\s*([^]\r\n]+?)\s*\]',header,re.I)
 if m:
  unit=m.group(1).strip().lower()
 else:
  # Alternate tool versions may expose an explicit unit key. This is
  # deliberately secondary to the documented descriptive [unit] format.
  m=re.search(r'\bunits?\s*=\s*\(?\s*([a-zA-Z]+)\s*\)?',info,re.I)
  unit=m.group(1).lower() if m else ''
 aliases={'meter':'m','meters':'m','metre':'m','metres':'m',
          'millimeter':'mm','millimeters':'mm','millimetre':'mm','millimetres':'mm',
          'centimeter':'cm','centimeters':'cm','centimetre':'cm','centimetres':'cm',
          'inch':'in','inches':'in'}
 unit=aliases.get(unit,unit)
 if unit not in {'m','mm','cm','in'}:
  raise ValueError('Unverified NOHRSC snowfall GRIB units '+repr(unit)+
                   '; inspect wgrib2 -d 1 -V before conversion')
 return unit

NOHRSC_DEPTH_METRE_FACTORS={'m':1.,'mm':.001,'cm':.01,'in':.0254}

@perf_monitor.timed_task('analysis_zarr_conversion', lambda a, k: '%s %s' % (a[0].get('product', 'analysis'), a[0].get('valid_time', 'unknown')))
def process_manifest(manifest,staging,outroot,wgrib2,level,stride,force=False,inspect=False,gfs_target=False):
 srcname=manifest.get('product')
 if manifest.get('status') not in ('complete','partial') or srcname not in STAGED_PRODUCTS:return None
 selected=manifest.get('selected',[])
 if not selected:return None
 stamp=datetime.fromisoformat(manifest['valid_time'].replace('Z','+00:00')).strftime('%Y%m%d%H')
 path=staging/manifest['file']
 if not path.is_file():raise FileNotFoundError(path)
 output_product=('gfs_'+srcname) if gfs_target else srcname
 id=f'analysis_{output_product}_{stamp}';dest=outroot/'analysis'/(id+'.zarr')
 if (dest/'.conversion.json').exists() and not force:return 'already'
 # NOHRSC grid184 cannot be assumed to use a viewer-supported projection.
 # Regrid its physical snowfall depth to the established 3-km Lambert grid;
 # keep provenance in the manifest, never silently relabel grid184 coordinates.
 nohrsc=srcname.startswith('nohrsc_')
 stageiv=srcname.startswith('stageiv_')
 if nohrsc:
  # Validate source physical units first; SNOW is stored as depth, not liquid water.
  info=run(wgrib2,path,'-d','1','-V')
  unit=nohrsc_depth_unit_from_verbose(info)
  workgrid=path.with_suffix('.gfs.grib2' if gfs_target else '.lambert.grib2')
  try:
   native_path=path
   if gfs_target:
    original_source=grid_details(wgrib2,native_path,1)
    run(wgrib2,path,'-new_grid_interpolation','bilinear',
        '-new_grid','latlon','160:601:0.25','5:281:0.25',workgrid)
   else:
    run(wgrib2,path,'-new_grid_interpolation','bilinear',
        '-new_grid','lambert:-97.5:38.5:38.5:38.5',
        '-122.719528:1799:3000','21.138123:1059:3000',workgrid)
   if not gfs_target:original_source=grid_details(wgrib2,native_path,1)
   path=workgrid
   # A conversion that silently yields a different grid must fail.
   if not path.is_file() or path.stat().st_size<100:raise ValueError('NOHRSC reprojection produced no data')
  except Exception:
   workgrid.unlink(missing_ok=True)
   raise
 # Native Stage IV is HRAP polar stereographic, not Lambert or regular_ll.
 # Explicitly regrid to the same CONUS Lambert as other operational overlays.
 # Preserve physical APCP in kg m-2; bilinear remapping is non-categorical.
 if gfs_target and (srcname.startswith('ccpa_') or srcname.startswith('mrms_apcp') or srcname=='mrms_prate'):
  workgrid=path.with_suffix('.gfs.grib2')
  try:
   run(wgrib2,path,'-new_grid_interpolation','bilinear',
       '-new_grid','latlon','160:601:0.25','5:281:0.25',workgrid)
   if not workgrid.is_file() or workgrid.stat().st_size<100:
    raise ValueError('MRMS/CCPA-to-GFS grid projection produced no data')
   path=workgrid
  except Exception:
   workgrid.unlink(missing_ok=True);raise
 if stageiv:
  workgrid=path.with_suffix('.gfs.grib2' if gfs_target else '.lambert.grib2')
  try:
   if gfs_target:
    run(wgrib2,path,'-new_grid_interpolation','bilinear',
        '-new_grid','latlon','160:601:0.25','5:281:0.25',workgrid)
   else:
    run(wgrib2,path,'-new_grid_interpolation','bilinear',
        '-new_grid','lambert:-97.5:38.5:38.5:38.5',
        '-122.719528:1799:3000','21.138123:1059:3000',workgrid)
   if not workgrid.is_file() or workgrid.stat().st_size<100:
    raise ValueError('Stage IV reprojection produced no data')
   path=workgrid
  except Exception:
   workgrid.unlink(missing_ok=True)
   raise
 original=grid_details(wgrib2,path,1)
 if inspect:
  LOG.info('%s %s -> grid %s',srcname,stamp,original)
  if nohrsc or stageiv or gfs_target:workgrid.unlink(missing_ok=True)
  return 'inspected'
 # Match every selected GRIB record to its actual grid, rather than assuming
 # that everything in a multi-message GRIB has a single projection.
 if len(selected)>1:
  for num in range(2,len(selected)+1):
   if grid_details(wgrib2,path,num)!=original:raise ValueError(f'Mixed grids in {path}, record {num}')
 nx,ny=original['GRIB_Nx'],original['GRIB_Ny']
 output_grid=strided_grid(original,stride if srcname.startswith('mrms') and not gfs_target else 1)
 dest.parent.mkdir(parents=True,exist_ok=True)
 tmp=Path(tempfile.mkdtemp(prefix='.'+id+'.',dir=dest.parent))
 try:
  atomic_json(tmp/'.zgroup',{'zarr_format':2})
  stamp_iso=manifest['valid_time'];source_at=manifest.get('source_time',stamp_iso)
  atomic_json(tmp/'.zattrs',{'model':'ANALYSIS','domain':'CONUS','init_time':stamp_iso,
     'valid_time':stamp_iso,'source_time':source_at,'source_product':output_product,
     'grid_order':'WE:SN','dimensions':{'valid_time':1,'y':output_grid['GRIB_Ny'],'x':output_grid['GRIB_Nx']},
     'grid':output_grid,'source_grid':original,
     'note':('Verification analysis explicitly aligned to 0.25-degree GFS grid; no extrapolation intended' if gfs_target else
             'Stage IV HRAP explicitly regridded to 3-km CONUS Lambert' if stageiv else
             'Native source projection; MRMS may be regularly subsampled for browser performance')})
  allfields={}
  with tempfile.TemporaryDirectory(prefix='evu_ieee_') as work:
   for i,item in enumerate(selected,1):
    name=item['field'];name='gust' if name=='gust10' else name
    if name not in FIELD_UNITS:continue
    vals=decode(wgrib2,path,i,nx,ny,Path(work)/f'{i}.ieee')
    vals=convert_units(name,srcname,vals)
    if nohrsc:
     factor=NOHRSC_DEPTH_METRE_FACTORS[unit]
     vals=array('f',(z*factor if math.isfinite(z) else float('nan') for z in vals))
    vals,_,_=stride_array(vals,nx,ny,stride if srcname.startswith('mrms') and not gfs_target else 1)
    allfields[name]=vals
  if srcname in ('urma','rtma') and 't2m' in allfields and 'dpt2m' in allfields:
   allfields['rh2m']=derive_rh(allfields['t2m'],allfields['dpt2m'])
  if srcname=='rap_pressure' and {'u300','v300'}<=allfields.keys():
   allfields['wspd300']=array('f',(math.hypot(u,v) if math.isfinite(u) and math.isfinite(v) and abs(u)<1e9 and abs(v)<1e9 else float('nan') for u,v in zip(allfields['u300'],allfields['v300'])))
  if srcname=='rap_surface' and 'snod' in allfields:
   allfields['snowpresence']=derive_snowpresence(allfields['snod'])
  if srcname=='rap_pressure':
   # Both layers are native UPP VUCSH/VVCSH records, not reconstructed from
   # pressure-level winds. Output their normalized m/s components and magnitude.
   # Treat each layer independently when its native records are unavailable.
   for suffix,depth in (('01',1000),('06',6000)):
    u_name,v_name='shru'+suffix,'shrv'+suffix
    if {u_name,v_name}<=allfields.keys():
     u,v=normalize_bulk_shear_components(allfields[u_name],allfields[v_name],depth)
     allfields[u_name],allfields[v_name]=u,v
     allfields['shear'+suffix]=array('f',(math.hypot(a,b) if math.isfinite(a) and math.isfinite(b) else float('nan') for a,b in zip(u,v)))
    else:
     allfields.pop(u_name,None);allfields.pop(v_name,None)
  # Derived RAP diagnostics use native RAP surface/wind and pressure/height
  # records from the SAME grid/time. Preserve native UPP shear/storm motion
  # if present; derived values are separately identified in variable metadata.
  derived_rap=set()
  if srcname=='rap_pressure':
   estimate=rapvd.derive_grid(allfields)
   for suffix in ('01',):
    pair=('shru'+suffix,'shrv'+suffix)
    if all(k in allfields for k in pair):
     for k in (*pair,'shear'+suffix):estimate.pop(k,None)
   if 'ustm' in allfields and 'vstm' in allfields:
    estimate.pop('ustm',None);estimate.pop('vstm',None)
   # Never publish a Zarr variable filled entirely with NaN.
   for k,vals in estimate.items():
    if any(math.isfinite(x) for x in vals):
     allfields[k]=vals;derived_rap.add(k)
  native_fields={entry['field']:entry for entry in selected
                 if entry.get('native_source')=='wrfprs'}
  for name,values in allfields.items():
   metadata={'long_name':name,'source_product':output_product,'source_time':source_at}
   if name in native_fields:
    metadata.update({'native_source_product':'wrfprs',
      'native_source_url':native_fields[name].get('native_url'),
      'regridded_to':'awp130', 'provenance':'NATIVE RAP wrfprs field regridded onto awp130'})
   if name in derived_rap:
    method=('linear height interpolation of native RAP 10-m and isobaric winds'
            if name in ('shru01','shrv01','shear01') else
            'Bunkers right mover: height-averaged 0-6 km wind and 7.5 m/s rightward deviation')
    metadata.update({'derivation':method,'provenance':'DERIVED RAP estimate (not native UPP field)',
       'derived_from':['hsfc','u10','v10']+[f'{c}{p}' for p in rapvd.LEVELS for c in ('h','u','v')],
       'approximate':True})
   if name in ('shru01','shrv01','shru06','shrv06') and name not in derived_rap:metadata.update({'derivation':'unit_aware_native_VUCSH_VVCSH_normalized_to_ms_v2'})
   if name in ('shear01','shear06') and name not in derived_rap:metadata.update({'derived_from':['shru'+name[-2:],'shrv'+name[-2:]],'derivation':'normalized_vector_magnitude_v2'})
   if name in ('ustm','vstm') and name not in derived_rap:metadata.update({'storm_motion':'Bunkers right-moving, native 0-6 km AGL'})
   if name=='rh2m':metadata.update({'derived_from':['t2m','dpt2m'],'formula':'Bolton water saturation vapor pressure; percent'})
   if name=='snowpresence':metadata.update({'derived_from':['snod'],'threshold_m':0.01,'note':'binary snow presence, NOT fractional snow cover'})
   if name=='apcp':
    metadata['accumulation_hours']=(int(srcname.split('_')[1][:-1]) if srcname.startswith('ccpa_') else
       (int(srcname.split('_')[1][:-1]) if stageiv and srcname!='stageiv_apcp' else
       (int(srcname.rsplit('_',1)[1][:-1]) if re.search(r'^mrms_apcp_\d+h$',srcname) else 1)))
    if stageiv:metadata['source_name']='NCEP Stage IV CONUS native precipitation analysis'
    if srcname.startswith('mrms_apcp'):
     metadata.update({'source_name':'MRMS multi-sensor QPE',
       'quality_pass':'Pass 2' if srcname!='mrms_apcp' else 'Pass 1'})
    if gfs_target:metadata['grid_alignment']='GFS 0.25-degree; no observations outside native coverage'
    if srcname.startswith('ccpa_'):metadata['source_name']='CCPA CONUS precipitation analysis; native accumulation, not forecast' 
   if nohrsc:
    metadata.update({'long_name':f'NOHRSC observed {name[4:]} snowfall',
       'accumulation_hours':int(name[4:-1]),'source_grid':original_source,
       'original_units':unit,'converted_to_units':'m'})
   if name=='rottrack_ml60':metadata.update({'long_name':'MRMS mid-level rotation track (3–6 km AGL; 60-min maximum)', 'accumulation_hours':1, 'statistic':'spatial maximum of absolute azimuthal shear over preceding 60 minutes', 'not_equivalent_to':'model 2–5 km updraft helicity'})
   if name=='prate':metadata['converted_from']='MRMS mm h-1 to kg m-2 s-1'
   write_array({'path':str(tmp),**output_grid},name,values,stamp_iso,metadata,level)
  if not allfields:raise ValueError('No supported GRIB records to convert')
  # mkdtemp() creates an owner-only (0700) directory. Zarr output is served
  # by a different web-server identity, so make committed artifacts readable
  # and directories traversable regardless of the local umask.
  for item in tmp.rglob('*'):
   if item.is_symlink():raise ValueError('Unexpected symlink in Zarr output: '+str(item))
   item.chmod(0o755 if item.is_dir() else 0o644)
  tmp.chmod(0o755)
  atomic_json(tmp/'.conversion.json',{'name':id,'product':output_product,'source_time':source_at,
   'valid_time':stamp_iso,'source_manifest':str(manifest.get('file')),
   'fields':sorted(allfields),'grid':output_grid,'source_grid':original,
   'aligned_to_gfs':bool(gfs_target),
   'shear_units_version':2 if srcname=='rap_pressure' and ('shear01' in allfields or 'shear06' in allfields) else None,
   'rap_diagnostics_version':2 if srcname=='rap_pressure' else None,
   'native_wrfprs_fields':sorted(native_fields),
   'derived_rap_fields':sorted(derived_rap)})
  (tmp/'.conversion.json').chmod(0o644)
  if dest.exists():
   backup=dest.with_name(dest.name+'.old');shutil.rmtree(backup,ignore_errors=True)
   os.replace(dest,backup);os.replace(tmp,dest);shutil.rmtree(backup,ignore_errors=True)
  else:os.replace(tmp,dest)
  LOG.info('CONVERTED %s valid=%s fields=%s grid=%s %sx%s',srcname,stamp_iso,','.join(allfields),
    output_grid['GRIB_gridType'],output_grid['GRIB_Nx'],output_grid['GRIB_Ny'])
  return 'converted'
 finally:
  if tmp.exists():shutil.rmtree(tmp)
  if nohrsc or stageiv or gfs_target:workgrid.unlink(missing_ok=True)

def prune(outroot,days,now=None,analysis_rules=()):
 from retention_policy import protects_analysis
 now=now or datetime.now(UTC);cutoff=now-timedelta(days=days);removed=0
 for folder in (outroot/'analysis').glob('analysis_*.zarr'):
  if folder.is_symlink() or not folder.is_dir() or protects_analysis(folder.name,analysis_rules):continue
  try:
   meta=json.loads((folder/'.conversion.json').read_text())
   t=datetime.fromisoformat(meta['valid_time'].replace('Z','+00:00'))
   if t<cutoff:shutil.rmtree(folder);removed+=1
  except (OSError,ValueError,KeyError) as e:LOG.warning('Skip pruning %s: %s',folder,e)
 return removed

def build_catalog(outroot):
 datasets=[]
 for folder in sorted((outroot/'analysis').glob('analysis_*.zarr')):
  try:
   conv=json.loads((folder/'.conversion.json').read_text());metadata=json.loads((folder/'.zattrs').read_text())
   variables=[];grid=metadata['grid'];ny,nx=grid['GRIB_Ny'],grid['GRIB_Nx']
   for field in conv['fields']:
    attrs=json.loads((folder/field/'.zattrs').read_text())
    if not (folder/field/'0.0.0').is_file():continue
    variables.append({'name':field,'shape':[1,ny,nx],'chunks':[1,ny,nx],'dtype':'<f4',
      'compressor':json.loads((folder/field/'.zarray').read_text())['compressor'],'available_hours':[0],'attrs':attrs})
   if not variables:continue
   datasets.append({'id':folder.stem,'model':conv['product'].upper(),
    'init_time':conv['valid_time'],'path':'analysis/'+folder.name,'grid':grid,'variables':variables,
    'source_time':conv['source_time'],'product':conv['product'],
    'analysis_source':('CCPA' if 'ccpa_' in conv['product'] else
       'Stage IV' if 'stageiv_' in conv['product'] else
       'MRMS' if 'mrms_apcp' in conv['product'] else conv['product'].upper()),
    'accumulation_hours':next((v['attrs'].get('accumulation_hours') for v in variables
         if v['name']=='apcp'),None),
    'quality_pass':next((v['attrs'].get('quality_pass') for v in variables
         if v['name']=='apcp'),None),
    'native_analysis':True})
  except Exception as e:LOG.warning('Skip invalid Zarr store %s: %s',folder,e)
 datasets.sort(key=lambda ds:(ds['init_time'],ds['product']),reverse=True)
 catalog={'schema':'rrfs-zarr-viewer-1','description':'OMD EVU verification data by UTC valid time',
   'datasets':datasets,'updated_at':datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}
 atomic_json(outroot/'catalog_analysis.json',catalog)
 return len(datasets)

@perf_monitor.monitored('verification_convert')
def main(argv=None):
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--root',required=True,help='Original verification ingestion staging root')
 p.add_argument('--output-root',required=True,help='Published-data staging root (contains analysis/ and catalog_analysis.json)')
 p.add_argument('--wgrib2',default=shutil.which('wgrib2'))
 p.add_argument('--lookback-hours',type=int,default=14)
 p.add_argument('--retention-days',type=int,default=7)
 p.add_argument('--protected-cycles-file',type=Path,default=Path(__file__).with_name('protected_cycles.json'))
 p.add_argument('--mrms-stride',type=int,default=1,help='MRMS grid sampling stride (default 1: retain every native grid point; higher values opt-in downsampling)')
 p.add_argument('--compression-level',type=int,default=3)
 p.add_argument('--force',action='store_true');p.add_argument('--inspect-grid',action='store_true')
 p.add_argument('--verbose',action='store_true')
 a=p.parse_args(argv)
 perf_monitor.record_event('run_config', workflow='verification_convert',
     mrms_stride=a.mrms_stride, compression_level=a.compression_level,
     lookback_hours=a.lookback_hours, inspect_grid=bool(a.inspect_grid))
 logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
 if not a.wgrib2: p.error('wgrib2 is required (--wgrib2)')
 if not 1<=a.mrms_stride<=16:p.error('--mrms-stride must be 1-16')
 if not 0<=a.compression_level<=9:p.error('compression-level must be 0-9')
 if not 1<=a.retention_days<=30:p.error('retention-days must be 1-30')
 from retention_policy import load_policy
 policy=load_policy(a.protected_cycles_file)
 stage=Path(a.root).expanduser().resolve();out=Path(a.output_root).expanduser().resolve();out.mkdir(parents=True,exist_ok=True)
 now=datetime.now(UTC);cutoff=now-timedelta(hours=a.lookback_hours)
 counts={'converted':0,'already':0,'inspected':0,'failed':0}
 for product in STAGED_PRODUCTS:
  for path in sorted((stage/product).glob('*/*.json')):
   try:
    manifest=json.loads(path.read_text())
    t=datetime.fromisoformat(manifest['valid_time'].replace('Z','+00:00'))
    if t<cutoff or t>now:continue
    result=process_manifest(manifest,stage,out,a.wgrib2,a.compression_level,a.mrms_stride,a.force,a.inspect_grid)
    if result:counts[result]+=1
    if product in ('nohrsc_6h','nohrsc_24h','ccpa_1h','ccpa_3h','ccpa_6h','stageiv_apcp','stageiv_6h','stageiv_24h','mrms_apcp','mrms_apcp_pass2','mrms_apcp_6h','mrms_apcp_24h','mrms_prate'):
     result=process_manifest(manifest,stage,out,a.wgrib2,a.compression_level,a.mrms_stride,a.force,a.inspect_grid,gfs_target=True)
     if result:counts[result]+=1
   except Exception as exc:
    LOG.exception('FAILED %s: %s',path,exc);counts['failed']+=1
 if not a.inspect_grid:
  removed=prune(out,a.retention_days,analysis_rules=policy.analysis_rules);total=build_catalog(out)
  LOG.info('SUMMARY %s, pruned=%d, catalog datasets=%d',counts,removed,total)
 else:LOG.info('GRID CHECK SUMMARY %s',counts)
 return 1 if counts['failed'] else 0
if __name__=='__main__':sys.exit(main())
