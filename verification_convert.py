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
import tempfile
import zlib

LOG=logging.getLogger('verify-convert')
UTC=timezone.utc
STAGED_PRODUCTS=['mrms_refc','mrms_prate','mrms_apcp','urma','rtma','rap_surface','rap_pressure']
FIELD_UNITS={'refc':'dBZ','prate':'kg m-2 s-1','apcp':'kg m-2','t2m':'K','dpt2m':'K',
             'rh2m':'%','wspd10':'m s-1','gust':'m s-1','u10':'m s-1','v10':'m s-1',
             'weasd':'kg m-2','snod':'m','snowpresence':'1','t925':'K','t850':'K','t700':'K'}

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
  # older formats may report metres. Cross-check on WCOSS2 via --inspect-grid.
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
 if field in ('prate','apcp') and v<0:return float('nan')
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
 (folder/'0.0.0').write_bytes(zlib.compress(raw,compression))

def process_manifest(manifest,staging,outroot,wgrib2,level,stride,force=False,inspect=False):
 srcname=manifest.get('product')
 if manifest.get('status') not in ('complete','partial') or srcname not in STAGED_PRODUCTS:return None
 selected=manifest.get('selected',[])
 if not selected:return None
 stamp=datetime.fromisoformat(manifest['valid_time'].replace('Z','+00:00')).strftime('%Y%m%d%H')
 path=staging/manifest['file']
 if not path.is_file():raise FileNotFoundError(path)
 id=f'analysis_{srcname}_{stamp}';dest=outroot/'analysis'/(id+'.zarr')
 if (dest/'.conversion.json').exists() and not force:return 'already'
 original=grid_details(wgrib2,path,1)
 if inspect:LOG.info('%s %s -> grid %s',srcname,stamp,original);return 'inspected'
 # Match every selected GRIB record to its actual grid, rather than assuming
 # that everything in a multi-message GRIB has a single projection.
 if len(selected)>1:
  for num in range(2,len(selected)+1):
   if grid_details(wgrib2,path,num)!=original:raise ValueError(f'Mixed grids in {path}, record {num}')
 nx,ny=original['GRIB_Nx'],original['GRIB_Ny']
 output_grid=strided_grid(original,stride if srcname.startswith('mrms') else 1)
 dest.parent.mkdir(parents=True,exist_ok=True)
 tmp=Path(tempfile.mkdtemp(prefix='.'+id+'.',dir=dest.parent))
 try:
  atomic_json(tmp/'.zgroup',{'zarr_format':2})
  stamp_iso=manifest['valid_time'];source_at=manifest.get('source_time',stamp_iso)
  atomic_json(tmp/'.zattrs',{'model':'ANALYSIS','domain':'CONUS','init_time':stamp_iso,
     'valid_time':stamp_iso,'source_time':source_at,'source_product':srcname,
     'grid_order':'WE:SN','dimensions':{'valid_time':1,'y':output_grid['GRIB_Ny'],'x':output_grid['GRIB_Nx']},
     'grid':output_grid,'source_grid':original,
     'note':'Native source projection; MRMS may be regularly subsampled for browser performance'})
  allfields={}
  with tempfile.TemporaryDirectory(prefix='evu_ieee_') as work:
   for i,item in enumerate(selected,1):
    name=item['field'];name='gust' if name=='gust10' else name
    if name not in FIELD_UNITS:continue
    vals=decode(wgrib2,path,i,nx,ny,Path(work)/f'{i}.ieee')
    vals=convert_units(name,srcname,vals)
    vals,_,_=stride_array(vals,nx,ny,stride if srcname.startswith('mrms') else 1)
    allfields[name]=vals
  if srcname in ('urma','rtma') and 't2m' in allfields and 'dpt2m' in allfields:
   allfields['rh2m']=derive_rh(allfields['t2m'],allfields['dpt2m'])
  if srcname=='rap_surface' and 'snod' in allfields:
   allfields['snowpresence']=derive_snowpresence(allfields['snod'])
  for name,values in allfields.items():
   metadata={'long_name':name,'source_product':srcname,'source_time':source_at}
   if name=='rh2m':metadata.update({'derived_from':['t2m','dpt2m'],'formula':'Bolton water saturation vapor pressure; percent'})
   if name=='snowpresence':metadata.update({'derived_from':['snod'],'threshold_m':0.01,'note':'binary snow presence, NOT fractional snow cover'})
   if name=='apcp':metadata['accumulation_hours']=1
   if name=='prate':metadata['converted_from']='MRMS mm h-1 to kg m-2 s-1'
   write_array({'path':str(tmp),**output_grid},name,values,stamp_iso,metadata,level)
  if not allfields:raise ValueError('No supported GRIB records to convert')
  # mkdtemp() creates an owner-only (0700) directory. Zarr output is served
  # by a different web-server identity, so make committed artifacts readable
  # and directories traversable regardless of the WCOSS2 umask.
  for item in tmp.rglob('*'):
   if item.is_symlink():raise ValueError('Unexpected symlink in Zarr output: '+str(item))
   item.chmod(0o755 if item.is_dir() else 0o644)
  tmp.chmod(0o755)
  atomic_json(tmp/'.conversion.json',{'name':id,'product':srcname,'source_time':source_at,
   'valid_time':stamp_iso,'source_manifest':str(manifest.get('file')),
   'fields':sorted(allfields),'grid':output_grid,'source_grid':original})
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
    'source_time':conv['source_time'],'product':conv['product']})
  except Exception as e:LOG.warning('Skip invalid Zarr store %s: %s',folder,e)
 datasets.sort(key=lambda ds:(ds['init_time'],ds['product']),reverse=True)
 catalog={'schema':'rrfs-zarr-viewer-1','description':'OMD EVU verification data by UTC valid time',
   'datasets':datasets,'updated_at':datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}
 atomic_json(outroot/'catalog_analysis.json',catalog)
 return len(datasets)

def main(argv=None):
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--root',required=True,help='Original verification ingestion staging root')
 p.add_argument('--output-root',required=True,help='Published-data staging root (contains analysis/ and catalog_analysis.json)')
 p.add_argument('--wgrib2',default=shutil.which('wgrib2'))
 p.add_argument('--lookback-hours',type=int,default=14)
 p.add_argument('--retention-days',type=int,default=14)
 p.add_argument('--protected-cycles-file',type=Path,default=Path(__file__).with_name('protected_cycles.json'))
 p.add_argument('--mrms-stride',type=int,default=1,help='MRMS grid sampling stride (default 1: retain every native grid point; higher values opt-in downsampling)')
 p.add_argument('--compression-level',type=int,default=3)
 p.add_argument('--force',action='store_true');p.add_argument('--inspect-grid',action='store_true')
 p.add_argument('--verbose',action='store_true')
 a=p.parse_args(argv)
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
   except Exception as exc:
    LOG.exception('FAILED %s: %s',path,exc);counts['failed']+=1
 if not a.inspect_grid:
  removed=prune(out,a.retention_days,analysis_rules=policy.analysis_rules);total=build_catalog(out)
  LOG.info('SUMMARY %s, pruned=%d, catalog datasets=%d',counts,removed,total)
 else:LOG.info('GRID CHECK SUMMARY %s',counts)
 return 1 if counts['failed'] else 0
if __name__=='__main__':sys.exit(main())
