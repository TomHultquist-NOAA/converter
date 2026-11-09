#!/usr/bin/env python3
"""Conservative, explicitly derived RAP shear and Bunkers right-mover estimates.

Works on one native RAP grid using 10-m winds, surface HGT, and native
isobaric height/wind records. Does not silently extrapolate missing layers.
Native GRIB USTM/VSTM and native VUCSH/VVCSH take precedence downstream.
"""
import math
from array import array

LEVELS = (925, 850, 700, 500, 300)
MIN_SHEAR_MS, MAX_SHEAR_MS = 0., 100.


def _valid(x, limit=250.):
    return math.isfinite(x) and abs(x) < limit


def _at(points, z):
    """Linearly interpolate vector at AGL height, disallow extrapolation."""
    if z < points[0][0] or z > points[-1][0]:
        return None
    for a, b in zip(points, points[1:]):
        if a[0] <= z <= b[0]:
            fraction = (z - a[0]) / (b[0] - a[0])
            return (a[1] + (b[1]-a[1])*fraction, a[2] + (b[2]-a[2])*fraction)
    return None


def _mean(points, bottom, top):
    """Height-weighted (not pressure-weighted) wind in an AGL layer."""
    levels=[bottom] + [z for z,_,_ in points if bottom < z < top] + [top]
    winds=[_at(points,z) for z in levels]
    if any(p is None for p in winds):return None
    u=v=0.
    for i in range(len(levels)-1):
        dz=levels[i+1]-levels[i]
        u+=.5*(winds[i][0]+winds[i+1][0])*dz
        v+=.5*(winds[i][1]+winds[i+1][1])*dz
    return u/(top-bottom),v/(top-bottom)


def point_diagnostics(surface_height, u10, v10, pressure_levels):
    """Return (0-1 km shear, Bunkers right mover) or None for missing data.

    Height in geopotential metres MSL and wind components in m/s. A 10-m wind
    represents the bottom of the profile. Valid pressure winds below ground
    are excluded; no 0-1 km value if 1000 m AGL cannot be bracketed. The
    0-6-km Bunkers calculation requires coverage to 6 km above ground.
    """
    if not (_valid(surface_height,15000) and _valid(u10,150) and _valid(v10,150)):
        return None,None
    points=[(0.,float(u10),float(v10)),(10.,float(u10),float(v10))]
    for height, u, v in pressure_levels:
        if not (_valid(height,40000) and _valid(u,150) and _valid(v,150)):
            continue
        above=height-surface_height
        if 10.1 <= above <= 15000.:
            points.append((above,float(u),float(v)))
    points.sort()
    # Reject clearly inconsistent height coordinates or vertical gaps.
    cleaned=[points[0]]
    for item in points[1:]:
        if item[0] - cleaned[-1][0] >= 1:
            cleaned.append(item)
    if len(cleaned)<3:return None,None
    upper=_at(cleaned,1000.)
    shear=None
    if upper is not None and min(z for z,_,_ in cleaned if z>10.) <= 2200:
        du,dv=upper[0]-u10,upper[1]-v10
        if math.hypot(du,dv)<=MAX_SHEAR_MS:shear=du,dv
    if cleaned[-1][0]<6000.:return shear,None
    mean=_mean(cleaned,0.,6000.)
    low=_mean(cleaned,0.,500.)
    high=_mean(cleaned,5500.,6000.)
    if any(x is None for x in (mean,low,high)):return shear,None
    du,dv=high[0]-low[0],high[1]-low[1]
    strength=math.hypot(du,dv)
    if strength<2.:return shear,None
    # Right of the deep-layer shear vector in (east,north) coordinates.
    rm=(mean[0]+7.5*dv/strength,mean[1]-7.5*du/strength)
    return shear,rm if math.hypot(*rm)<=150 else None


def derive_grid(fields):
    """Return derived field arrays on the source RAP native grid.

    Uses no extra packages; input arrays must all share the same grid.
    One-time per hourly analysis; missing inputs leave corresponding outputs
    unavailable rather than inventing wind vectors.
    """
    required=('hsfc','u10','v10')
    if not all(x in fields for x in required):return {}
    levels=[p for p in LEVELS if all(f'{name}{p}' in fields for name in ('h','u','v'))]
    if not levels:return {}
    count=len(fields['hsfc'])
    if any(len(fields[x])!=count for x in required):raise ValueError('RAP surface winds/grid length mismatch')
    if any(len(fields[f'{name}{p}'])!=count for p in levels for name in ('h','u','v')):
        raise ValueError('RAP pressure wind/grid length mismatch')
    result={name:array('f') for name in ('shru01','shrv01','shear01','ustm','vstm')}
    nan=float('nan')
    for i in range(count):
        sample=[(fields[f'h{p}'][i],fields[f'u{p}'][i],fields[f'v{p}'][i]) for p in levels]
        shear,rm=point_diagnostics(fields['hsfc'][i],fields['u10'][i],fields['v10'][i],sample)
        if shear is None:
            result['shru01'].append(nan);result['shrv01'].append(nan);result['shear01'].append(nan)
        else:
            result['shru01'].append(shear[0]);result['shrv01'].append(shear[1]);result['shear01'].append(math.hypot(*shear))
        result['ustm'].append(rm[0] if rm is not None else nan)
        result['vstm'].append(rm[1] if rm is not None else nan)
    return result
