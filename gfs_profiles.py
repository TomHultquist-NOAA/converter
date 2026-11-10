#!/usr/bin/env python3
"""GFS native-quarter-degree profile Zarr and optional native-layer kinematics.

No horizontal or vertical interpolation is performed for the stored soundings.
Shear/SRH are diagnosed at AGL boundaries from *native* isobaric heights,
winds and surface height, with no extrapolation across missing layers.
"""
from __future__ import annotations
from array import array
import datetime as dt
import json
import math
from pathlib import Path
import re
import sys

import sounding_profiles as sp
import perf_monitor
import multi_model_to_zarr_wcoss2 as mm

PRESSURES=sp.PRESSURES
VARIABLES=('t','q','td','u','v')
SURFACE=('sp','t2m','td2m','u10','v10')
PROFILE_MAP={'t':'TMP','q':'SPFH','u':'UGRD','v':'VGRD','z':'HGT'}


def select(rows):
    """Return records by (component, level); never pretend a missing level exists."""
    found={}
    for p in PRESSURES:
        for key,short in PROFILE_MAP.items():
            rec=sp.native_record(rows,short,p)
            if rec is not None:
                found[(key,p)]=rec
        if ('q',p) not in found:
            for key,short in (('td_native','DPT'),('rh','RH')):
                rec=sp.native_record(rows,short,p)
                if rec is not None:
                    found[(key,p)]=rec
                    break
    z_sfc=sp.surface_record(rows,'HGT','surface')
    if z_sfc is not None:
        found[('z_sfc','surface')]=z_sfc
    return found


def _arr(raw, npts):
    a=array('f');a.frombytes(raw)
    if sys.byteorder!='little':a.byteswap()
    if len(a)!=npts:raise ValueError('GFS profile field length mismatch')
    return a


def _bytes(a):
    if sys.byteorder=='little':return a.tobytes()
    x=array('f',a);x.byteswap();return x.tobytes()


def _native_surface(raw_map, key, npts):
    entry=raw_map.get(key)
    return (_arr(entry[0],npts) if entry else array('f',[math.nan])*npts)


def create_store(root, model, date, cycle, nx, ny, grid, compression):
    store=root/f'sounding_{model}_{date}_{cycle}00.zarr'
    store.mkdir(parents=True,exist_ok=True)
    hours=241
    init=mm.iso_init(date,cycle)
    if (store/'.zattrs').is_file():
        old=json.loads((store/'.zattrs').read_text())
        if (old.get('model_id')!=model or old.get('grid_stride')!=1 or
            old.get('dimensions',{}).get('x')!=nx or old.get('dimensions',{}).get('y')!=ny or
            old.get('source_grid')!=grid or old.get('init_time')!=init.strftime('%Y-%m-%dT%H:%M:%SZ')):
            raise ValueError('Incompatible existing GFS native-grid sounding store')
    else:
        mm.core.json_write(store/'.zgroup',{'zarr_format':2})
        mm.core.json_write(store/'.zattrs',{
            'model_id':model,'init_time':init.strftime('%Y-%m-%dT%H:%M:%SZ'),
            **grid,'source_grid':grid,'grid_stride':1,'grid_order':'WE:SN',
            'grid_spacing_degrees':.25, 'native_grid':True,'regridded':False,
            'pressure_levels_hpa':list(PRESSURES),
            'dimensions':{'valid_time':hours,'pressure_hpa':len(PRESSURES),'y':ny,'x':nx},
            'source':'Native GFS 0.25-degree GRIB2 pressure-level profiles',
            'missing_levels':'NaN; no vertical interpolation',
            'dewpoint_formula':'Specific humidity or native DPT/RH when SPFH is missing',
            'notes':'Native horizontal model cells; no spatial downsampling or 3-km upscaling',
        })
    for var in VARIABLES:
        mm.core.ensure_array(store,var,(hours,len(PRESSURES),ny,nx),(1,len(PRESSURES),ny,nx),'<f4','NaN',
            {'_ARRAY_DIMENSIONS':['valid_time','pressure_hpa','y','x'],'units':sp.UNITS[var],
             'long_name':sp.LONG[var],'provenance':'native GRIB; Td from native moisture'},compression)
    for var in SURFACE:
        mm.core.ensure_array(store,var,(hours,ny,nx),(1,ny,nx),'<f4','NaN',
            {'_ARRAY_DIMENSIONS':['valid_time','y','x'],'units':sp.UNITS[var],
             'provenance':'native GFS GRIB2 on native grid'},compression)
    for name,dtype,values,unit,dim in (
        ('pressure_hpa','<i2',mm.core.pack_int16_le(PRESSURES),'hPa','pressure_hpa'),
        ('forecast_hour','<i2',mm.core.pack_int16_le(range(hours)),'hours','valid_time'),
        ('valid_time','<i8',mm.core.pack_int64_le(int((init+dt.timedelta(hours=h)).timestamp()) for h in range(hours)),
         'seconds since 1970-01-01 UTC','valid_time')):
        n=len(PRESSURES) if name=='pressure_hpa' else hours
        mm.core.ensure_array(store,name,(n,),(n,),dtype,None,
            {'_ARRAY_DIMENSIONS':[dim],'units':unit},compression)
        if not (store/name/'0').exists():mm.core.write_compressed_chunk(store/name/'0',values,compression)
    return store


def _wind_at(height, levels):
    """Interpolate between bracketing valid native AGL levels, no extrapolation."""
    for (z0,u0,v0),(z1,u1,v1) in zip(levels,levels[1:]):
        if z0<=height<=z1 and z1>z0:
            factor=(height-z0)/(z1-z0)
            return (u0+(u1-u0)*factor,v0+(v1-v0)*factor)
    return None


def _srh(levels, motion_u, motion_v):
    if not (math.isfinite(motion_u) and math.isfinite(motion_v)):return math.nan
    value=0.0
    for (_,u0,v0),(_,u1,v1) in zip(levels,levels[1:]):
        value+=(v0-motion_v)*(u1-u0)-(u0-motion_u)*(v1-v0)
    return value


def derive_kinematics(profile, map_fields, npts):
    """Physical 0–1/0–6 km shear and SRH using 10m anchor and valid AGL heights.

    All required levels must span target height; keep NaN if surface terrain or
    vertical winds are missing. No artificial vertical extrapolation.
    """
    z_sfc=profile.get(('z_sfc','surface'))
    if z_sfc is None:return {}
    z_ground=_arr(z_sfc,npts)
    u10=_native_surface(map_fields,'u10',npts)
    v10=_native_surface(map_fields,'v10',npts)
    pressure=_native_surface(map_fields,'psfc',npts)
    u_stm=_native_surface(map_fields,'ustm',npts)
    v_stm=_native_surface(map_fields,'vstm',npts)
    vertical=[]
    for p in PRESSURES:
        if all((v,p) in profile for v in ('u','v','z')):
            vertical.append((p,_arr(profile[('u',p)],npts),_arr(profile[('v',p)],npts),_arr(profile[('z',p)],npts)))
    if not vertical:return {}
    names=('shru01','shrv01','shear01','shru06','shrv06','shear06','srh01','srh03')
    outputs={k:array('f',[math.nan])*npts for k in names}
    for i in range(npts):
        ground=z_ground[i]
        if not (math.isfinite(ground) and -500<ground<9000 and
                math.isfinite(u10[i]) and -250<u10[i]<250 and
                math.isfinite(v10[i]) and -250<v10[i]<250 and
                math.isfinite(pressure[i]) and 40000<=pressure[i]<=115000):continue
        levels=[(10.,u10[i],v10[i])]
        for p,u,v,z in vertical:
            if p*100>=pressure[i]:continue
            if not (math.isfinite(u[i]) and -250<u[i]<250 and
                    math.isfinite(v[i]) and -250<v[i]<250 and
                    math.isfinite(z[i]) and -500<z[i]<40000):continue
            agl=z[i]-ground
            if 10<agl<20000:levels.append((agl,u[i],v[i]))
        levels.sort(key=lambda a:a[0])
        # Need a reliable near-surface profile; no extrapolation of 1/3/6 km.
        for threshold,suffix in ((1000.,'01'),(6000.,'06')):
            end=_wind_at(threshold,levels)
            if end is None:continue
            du,dv=end[0]-u10[i],end[1]-v10[i]
            outputs['shru'+suffix][i]=du
            outputs['shrv'+suffix][i]=dv
            outputs['shear'+suffix][i]=math.hypot(du,dv)  # m s-1; viewer converts to knots
        for threshold,suffix in ((1000.,'01'),(3000.,'03')):
            end=_wind_at(threshold,levels)
            if end is None:continue
            interior=[pt for pt in levels if pt[0]<threshold]
            value=_srh(interior+[(threshold,end[0],end[1])],u_stm[i],v_stm[i])
            if math.isfinite(value):outputs['srh'+suffix][i]=value
    # Never advertise a derived map layer if it contains no valid grid cells.
    return {k:_bytes(v) for k,v in outputs.items() if any(math.isfinite(x) for x in v)}


@perf_monitor.timed_task('gfs_sounding_commit', lambda a,k: '%s %s%s F%03d' % (a[1], a[2], a[3], a[4]))
def commit(root, model, date, cycle, hour, profile, map_fields, nx,ny,grid,compression):
    """Write independent profile store and completion stamp only after all chunks."""
    if not any(k[0]=='t' and isinstance(k[1],int) for k in profile):
        return None,{}
    npts=nx*ny; nlev=len(PRESSURES)
    store=create_store(root,model,date,cycle,nx,ny,grid,compression)
    # Construct one variable at a time to limit peak memory, preserving exact
    # native pressure levels with NaNs where no source record exists.
    for var in ('t','q','u','v'):
        output=array('f',[math.nan])*(nlev*npts)
        for idx,p in enumerate(PRESSURES):
            data=profile.get((var,p))
            if data is None:continue
            values=_arr(data,npts)
            offset=idx*npts
            output[offset:offset+npts]=array('f',(sp.valid_sample(x,var) for x in values))
        mm.core.write_compressed_chunk(store/var/f'{hour}.0.0.0',_bytes(output),compression)
    # Derive dewpoint only at truly populated native levels; never fill by
    # interpolation or extrapolation between pressure levels.
    td=array('f',[math.nan])*(nlev*npts)
    for idx,p in enumerate(PRESSURES):
        q=profile.get(('q',p));t=profile.get(('t',p))
        fallback=profile.get(('td_native',p));rh=profile.get(('rh',p))
        if not (q or fallback or (t and rh)):continue
        qq=_arr(q,npts) if q else None
        tt=_arr(t,npts) if t else None
        native=_arr(fallback,npts) if fallback else None
        rel=_arr(rh,npts) if rh else None
        start=idx*npts
        for i in range(npts):
            v=sp.dewpoint_kelvin(qq[i],p) if qq is not None else math.nan
            if not math.isfinite(v) and native is not None:v=sp.valid_sample(native[i],'td')
            if not math.isfinite(v) and tt is not None and rel is not None:v=sp.dewpoint_from_rh_kelvin(tt[i],rel[i])
            td[start+i]=v
    mm.core.write_compressed_chunk(store/'td'/f'{hour}.0.0.0',_bytes(td),compression)
    for var in SURFACE:
        data=map_fields.get({'sp':'psfc','td2m':'dpt2m'}.get(var,var))
        if data is not None:
            mm.core.write_compressed_chunk(store/var/f'{hour}.0.0',data[0],compression)
    derived=derive_kinematics(profile,map_fields,npts)
    return store,derived


def mark_hour_complete(store, hour, profile, derived_names):
    # Completion is advertised only after profile + all derived map writes.
    mm.core.json_write(store/'.hours'/f'{hour:03d}.json',{
        'hour':hour,'processing_revision':1,
        'native_levels':{name:[p for p in PRESSURES if (name,p) in profile]
                         for name in ('t','q','u','v','z')},
        'dewpoint_source':'SPFH, native DPT, RH fallback; no vertical interpolation',
        'grid_stride':1, 'derived_kinematics':list(sorted(derived_names)),
    })
