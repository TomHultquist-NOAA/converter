#!/usr/bin/env python3
"""Incremental GFSv16/v17 marine-wave ingest on the existing display grid.

Only the six scalar products known in BOTH versions' global 0p25 inventories,
and their three paired direction fields, are requested. Each version is read
independently; no placeholder data are produced for missing files/records.
"""
from __future__ import annotations
from array import array
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.error

# The operational GFS-Wave inventory is the common-denominator contract.
# In particular, MAXWH is not in that inventory, and is deliberately omitted.
import perf_monitor
SPECS = {
 'wave_height_sig': ('HTSGW', 'surface', 'm', 'Significant wave height'),
 'wave_height_wind': ('WVHGT', 'surface', 'm', 'Wind-wave significant height'),
 'wave_height_swell1': ('SWELL', '1 in sequence', 'm', 'Primary swell height'),
 'wave_period_primary': ('PERPW', 'surface', 's', 'Primary wave period'),
 'wave_period_wind': ('WVPER', 'surface', 's', 'Wind-wave period'),
 'wave_period_swell1': ('SWPER', '1 in sequence', 's', 'Primary swell period'),
 'wave_direction_primary': ('DIRPW', 'surface', 'degree_true', 'Primary wave direction'),
 'wave_direction_wind': ('WVDIR', 'surface', 'degree_true', 'Wind-wave direction'),
 'wave_direction_swell1': ('SWDIR', '1 in sequence', 'degree_true', 'Primary swell direction'),
}
DIRECTIONS = {
 'wave_direction_primary': ('wave_u_primary','wave_v_primary'),
 'wave_direction_wind': ('wave_u_wind','wave_v_wind'),
 'wave_direction_swell1': ('wave_u_swell1','wave_v_swell1'),
}


def wave_path(root,date,cycle,hour):
 return Path(root)/f'gfs.{date}'/cycle/'products/wave/gridded/global.0p25'/f'gfs.t{cycle}z.global.0p25.f{hour:03d}.grib2'


def v16_wave_url(base,date,cycle,hour):
 return (base.rstrip('/')+f'/gfs.{date}/{cycle}/wave/gridded/'
         f'gfswave.t{cycle}z.global.0p25.f{hour:03d}.grib2')


def inventory(text):
 """Return GRIB message numbers, matched on both short name and partition."""
 rows=[]
 for line in text.splitlines():
  parts=line.split(':')
  if len(parts)<5 or not parts[0].isdigit():continue
  rows.append((int(parts[0]),parts[3].strip().upper(),parts[4].strip().lower()))
 found={}
 for key,(short,level,_,_) in SPECS.items():
  matches=[n for n,s,l in rows if s==short and l==level]
  if matches:found[key]=matches[0]
 return found


def components(data):
 """Oceanographic wave headings: direction *toward*, degrees clockwise from N."""
 u=array('f');v=array('f')
 for deg in data:
  if not math.isfinite(deg) or abs(deg)>1e8:
   u.append(math.nan);v.append(math.nan)
  else:
   angle=math.radians(deg%360)
   u.append(math.sin(angle));v.append(math.cos(angle))
 return u,v


def decode_one(source,record,wgrib2,shape,grid,target,directional=False):
 # Wave source is the global 0.25-degree product; ocean coverage still differs from the atmospheric grid;
 # empty cells beyond source coverage remain undefined/transparent.
 mode='neighbor' if directional else 'bilinear'
 cmd=[wgrib2,str(source),'-d',str(record),'-new_grid_winds','earth',
      '-new_grid_interpolation',mode,'-new_grid','latlon',grid[0],grid[1],str(target)]
 subprocess.run(cmd,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
 from multi_model_to_zarr_wcoss2 import core
 binary=target.with_suffix('.ieee')
 core.decode_to_ieee(wgrib2,target,binary)
 raw=binary.read_bytes()
 if len(raw)!=4*shape[0]*shape[1]:
  raise RuntimeError(f'Marine regrid returned {len(raw)} bytes; expected {4*shape[0]*shape[1]}')
 return raw


@perf_monitor.timed_task('gfs_marine_hour', lambda a,k: '%s %s%s F%03d' % (a[0], a[2], a[3], a[4]))
def process_hour(model,gfs,date,cycle,hour,root,wgrib2,nx,ny,west,south,step):
 with tempfile.TemporaryDirectory(prefix='evu_gfs_wave_') as tmp:
  tmp=Path(tmp)
  if model=='gfs_v16':
   source_url=v16_wave_url(root,date,cycle,hour)
   client=gfs.core.HTTPClient(timeout=60,retries=2)
   try:
    index=client.text(source_url+'.idx')
    size=client.size(source_url)
   except (urllib.error.HTTPError,urllib.error.URLError,TimeoutError) as exc:
    return hour,'unavailable',{},str(exc)
   rows=gfs.parse_idx(index,size)
   matches=inventory(index)
   if not matches:return hour,'unavailable',{},'no common wave parameters in v16 index'
   selected={str(n):(next(r for r in rows if r['msg']==n),None) for n in set(matches.values())}
   source=tmp/'wave_selected.grib2'
   ordered=gfs.fetch_v16_grib(client,source_url,rows,selected,source)
   record_lookup={r['msg']:i for i,r in enumerate(ordered,1)}
   matches={k:record_lookup[n] for k,n in matches.items()}
  else:
   source=wave_path(root,date,cycle,hour)
   if not source.is_file() or source.stat().st_size==0:
    return hour,'unavailable',{},'v17 wave GRIB2 not yet available'
   result=subprocess.run([wgrib2,str(source),'-s'],check=True,capture_output=True,text=True)
   matches=inventory(result.stdout)
   if not matches:return hour,'unavailable',{},'no common wave parameters in v17 inventory'
  fields={}
  for key,rec in matches.items():
   target=tmp/f'{key}.grib2'
   fields[key]=decode_one(source,rec,wgrib2,(nx,ny),
     (f'{west:g}:{nx}:{step:g}',f'{south:g}:{ny}:{step:g}'),target,key in DIRECTIONS)
  return hour,'complete',fields,''


@perf_monitor.timed_task('gfs_marine_ingest', lambda a,k: '%s%s' % (a[3], a[4]), include_metrics=False)
def ingest(gfs,output,work,date,cycle,hours,root16,root17,wgrib2,workers,compression,force=False,models=('gfs_v16','gfs_v17')):
 """Process only missing wave hours for already-committed atmospheric leads."""
 touched=[]
 for model in models:
  store=output/f'{model}_{date}_{cycle}00.zarr'
  if not store.is_dir():continue
  status=gfs.read_status(store)
  # A committed shared scalar is the per-model progress marker, separate from
  # atmospheric progress and without a second filesystem manifest.
  candidates=[h for h in hours if status[h] and
              (force or not (store/'.marine_global_0p25'/f'{h:03d}.complete').is_file())]
  if not candidates:continue
  logging.info('%s marine: %d eligible missing hours',model,len(candidates))
  changed=False
  with ThreadPoolExecutor(max_workers=max(1,workers)) as pool:
   tasks={pool.submit(process_hour,model,gfs,date,cycle,h,
      root16 if model=='gfs_v16' else root17,wgrib2,
      gfs.NX,gfs.NY,gfs.WEST,gfs.SOUTH,gfs.STEP):h for h in candidates}
   for task in as_completed(tasks):
    hour=tasks[task]
    try: hour,state,fields,reason=task.result()
    except Exception as exc:
     logging.exception('%s marine F%03d failed: %s',model,hour,exc);continue
    if state!='complete':
     logging.info('%s marine F%03d: %s %s',model,hour,state,reason);continue
    for key,data in fields.items():
     short,_,units,label=SPECS[key]
     attrs={'_ARRAY_DIMENSIONS':['valid_time','y','x'],'long_name':label,'units':units,
            'GRIB_shortName':short,'source':'GFS global 0.25-degree wave GRIB2, regridded to regional 0.25-degree'}
     gfs.core.ensure_array(store,key,(241,gfs.NY,gfs.NX),(1,gfs.NY,gfs.NX),'<f4','NaN',attrs,compression)
     gfs.core.write_compressed_chunk(store/key/f'{hour}.0.0',data,compression)
     if key in DIRECTIONS:
      a=gfs.arrfloat(data)
      for name,comp in zip(DIRECTIONS[key],components(a)):
       gfs.core.ensure_array(store,name,(241,gfs.NY,gfs.NX),(1,gfs.NY,gfs.NX),'<f4','NaN',
         {'_ARRAY_DIMENSIONS':['valid_time','y','x'],'long_name':'Wave propagation '+('east' if name.startswith('wave_u') else 'north')+' unit component',
          'units':'1','source_direction':key},compression)
       gfs.core.write_compressed_chunk(store/name/f'{hour}.0.0',gfs.packfloat(comp),compression)
    if all(name in fields for name in SPECS):
     stamp=store/'.marine_global_0p25'/f'{hour:03d}.complete'
     stamp.parent.mkdir(parents=True,exist_ok=True)
     stamp.write_text('global.0p25\n')
    else:
     logging.warning('%s marine F%03d: only %d/%d expected records; will retry later',model,hour,len(fields),len(SPECS))
    changed=True
    logging.info('%s marine F%03d: %d shared wave records written',model,hour,len(fields))
  if changed:touched.append(store)
 return touched
