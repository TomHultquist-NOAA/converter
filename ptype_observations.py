#!/usr/bin/env python3
"""Fetch public-eligible Synoptic and licensed mPING present-weather reports.

Writes sanitized, time-binned GeoJSON. Authentication is through environment
variables only. Does not depend on third-party Python packages. Publication is
opt-in and requires an explicit rights confirmation for this public viewer.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

UTC = dt.timezone.utc
BBOX = (-125.0, 24.0, -66.0, 50.0)
SCHEMA = 'omd-evu-ptype-observations-1'
MPING_ROOT = 'https://mping.ou.edu/mping/api/v2/reports'
SYNOPTIC_ROOT = 'https://api.synopticdata.com/v2/stations/nearesttime'
LABELS = {1:'Rain', 2:'Snow', 3:'Freezing rain', 4:'Sleet', 5:'Mixed'}
LOG = logging.getLogger('ptype_obs')


def utc_date(value):
    if not isinstance(value, str):
        raise ValueError('Invalid observation date')
    obj = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if obj.tzinfo is None:
        raise ValueError('UTC time zone missing')
    return obj.astimezone(UTC)


def classify_description(description):
    """Classify present weather only; never infer type from visibility or totals."""
    if not isinstance(description, str):
        return None
    s = description.lower().replace('_', ' ').replace('-', ' ')
    # Ignore non-precipitation descriptions such as blowing snow, freezing fog.
    for junk in ('blowing snow', 'blowing dust', 'freezing fog', 'ice fog',
                 'snow cover', 'snow depth', 'no precipitation', 'unknown precipitation',
                 'fog', 'haze', 'clear sky'):
        s = s.replace(junk, ' ')
    hit = set()
    if re.search(r'freezing\s+(?:rain|drizzle)|frz\s*(?:rain|drizzle)', s):
        hit.add(3)
        s = re.sub(r'freezing\s+(?:rain|drizzle)|frz\s*(?:rain|drizzle)', '', s)
    if re.search(r'ice pellets?|sleet|snow pellets?|small hail|graupel', s):
        hit.add(4)
        s = re.sub(r'ice pellets?|sleet|snow pellets?|small hail|graupel', '', s)
    if re.search(r'\bsnow(?:fall|flakes| grains| showers?)?\b|\bsnowing\b', s):
        hit.add(2)
    if re.search(r'\brain(?:fall| showers?|ing)?\b|\bdrizzle\b', s):
        hit.add(1)
    if re.search(r'\bmix(?:ed|ture)?\b', s) and hit:
        return 5
    return 5 if len(hit) > 1 else next(iter(hit), None)


# Synoptic weather condition number (WNUM), base-80 packed categories.
_WNUM_RAIN = {1, 2, 13, 14, 16, 17, 18, 51, 52, 66, 77, 78}
_WNUM_SNOW = {3, 20, 21, 22, 24, 55, 56, 59, 60}
_WNUM_FREEZING = {15, 19, 49, 50, 53, 54}
_WNUM_SLEET = {23, 25, 57, 58, 61, 62, 63, 67, 75, 76}


def classify_wnum(raw):
    try:
        val = int(float(raw))
    except (ValueError, TypeError, OverflowError):
        return None
    if val <= 0:
        return None
    codes = [(val // 6400) % 80, (val // 80) % 80, val % 80]
    kinds = set()
    for code in codes:
        if code in _WNUM_RAIN: kinds.add(1)
        if code in _WNUM_SNOW: kinds.add(2)
        if code in _WNUM_FREEZING: kinds.add(3)
        if code in _WNUM_SLEET: kinds.add(4)
    return 5 if len(kinds) > 1 else next(iter(kinds), None)


def json_request(url, headers=None):
    # The URL may contain Synoptic's secret token: never log it or propagate it
    # through exception text. Provider response bodies are also not logged.
    try:
        req = Request(url, headers={'Accept':'application/json', 'User-Agent':'OMD-EVU-PtypeObs/1.0', **(headers or {})})
        with urlopen(req, timeout=35) as response:
            if int(response.headers.get('Content-Length', '0') or '0') > 24_000_000:
                raise RuntimeError('API response too large')
            raw = response.read(24_000_001)
            if len(raw) > 24_000_000:
                raise RuntimeError('API response too large')
        return json.loads(raw)
    except HTTPError as exc:
        raise RuntimeError(f'API returned HTTP {exc.code}') from None
    except URLError:
        raise RuntimeError('API network request failed') from None


def coordinates(lon, lat, bbox=BBOX):
    try:
        lo, la = float(lon), float(lat)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lo) and math.isfinite(la) and bbox[0] <= lo <= bbox[2] and bbox[1] <= la <= bbox[3]):
        return None
    return [round(lo, 5), round(la, 5)]


def feature(source, coords, ptype, observed, label, name, identity):
    return {'type':'Feature', 'geometry':{'type':'Point','coordinates':coords},
            'properties':{'source':source,'ptype':ptype,'description':str(label)[:120],
                          'observed_time':observed.isoformat().replace('+00:00','Z'),
                          'station':str(name)[:100], 'id':str(identity)[:80]}}


def nearest_obs(obs, keys):
    candidates = []
    for key, value in obs.items():
        if not any(key.startswith(prefix + '_') for prefix in keys):
            continue
        if isinstance(value, dict) and value.get('date_time'):
            candidates.append((value['date_time'], value.get('value'), key))
    return candidates


def get_synoptic(valid_hour, minutes=30, token=None):
    if not token:
        raise RuntimeError('Synoptic token unavailable')
    # nearesttime is *backward-looking*: shift requested time forward by the
    # tolerance, then 'within' scans the symmetric window about valid_hour.
    request_time = valid_hour + dt.timedelta(minutes=minutes)
    params = {'token': token, 'attime':request_time.strftime('%Y%m%d%H%M'),
              'within':minutes * 2, 'bbox':','.join(map(str, BBOX)),
              'vars':'weather_cond_code,weather_condition', 'network':'1', 'hfmetars':'0', 'obtimezone':'UTC',
              'output':'json', 'showemptystations':'0'}
    payload = json_request(SYNOPTIC_ROOT + '?' + urlencode(params))
    status = payload.get('SUMMARY', {})
    if str(status.get('RESPONSE_CODE')) not in ('1','2'):
        raise RuntimeError('Synoptic request unsuccessful: code ' + str(status.get('RESPONSE_CODE', 'unknown')))
    results = []
    for station in payload.get('STATION', []):
        # Restricted station records must never enter the publicly served JSON.
        if str(station.get('RESTRICTED', '')).lower() not in ('false', '0'):
            continue
        xy = coordinates(station.get('LONGITUDE'), station.get('LATITUDE'))
        if not xy: continue
        obs = station.get('OBSERVATIONS', {})
        if not isinstance(obs, dict): continue
        entries = nearest_obs(obs, ('weather_cond_code','weather_condition'))
        entries.sort(key=lambda p:abs((utc_date(p[0])-valid_hour).total_seconds()))
        for timestamp, value, name in entries:
            try: when = utc_date(timestamp)
            except (TypeError, ValueError): continue
            if abs((when - valid_hour).total_seconds()) > minutes*60: continue
            kind = classify_wnum(value) if name.startswith('weather_cond_code_') else classify_description(value)
            if kind is None: continue
            results.append(feature('synoptic',xy,kind,when, LABELS[kind],station.get('STID',''),station.get('STID','')))
            break
    return results


def get_mping(valid_hour, minutes=30, key=None, max_pages=50):
    if not key: raise RuntimeError('mPING key unavailable')
    start = valid_hour - dt.timedelta(minutes=minutes)
    end = valid_hour + dt.timedelta(minutes=minutes)
    params = {'obtime_gte': start.strftime('%Y-%m-%d %H:%M:%S'),
              'obtime_lte': end.strftime('%Y-%m-%d %H:%M:%S'),
              'in_bbox': ','.join(map(str, BBOX)), 'category':'Rain/Snow'}
    url = MPING_ROOT + '?' + urlencode(params)
    headers = {'Authorization': 'Token ' + key}
    seen_urls = set()
    reports = []
    for _ in range(max_pages):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != 'mping.ou.edu' or parsed.path != '/mping/api/v2/reports':
            raise RuntimeError('mPING pagination redirected outside approved endpoint')
        if url in seen_urls: raise RuntimeError('mPING pagination loop')
        seen_urls.add(url)
        payload = json_request(url, headers=headers)
        for rep in payload.get('results', []):
            if rep.get('category','').strip().lower() != 'rain/snow': continue
            kind = classify_description(rep.get('description',''))
            geom = rep.get('geom') or {}
            xy_raw = geom.get('coordinates') if isinstance(geom, dict) else None
            xy = coordinates(*xy_raw[:2]) if isinstance(xy_raw,list) and len(xy_raw)>=2 else None
            if kind is None or not xy: continue
            try: when = utc_date(rep.get('obtime',''))
            except (ValueError, TypeError): continue
            if abs((when-valid_hour).total_seconds()) > minutes*60: continue
            reports.append(feature('mping',xy,kind,when,rep.get('description',''), 'mPING report', rep.get('id','')))
        nxt = payload.get('next')
        if not nxt:
            return reports
        url = urljoin(url, nxt)
    raise RuntimeError('mPING pagination limit reached; refusing incomplete data')


def build_hour(when, args, old=None, getter_synoptic=get_synoptic, getter_mping=get_mping):
    sections = {}
    failures = []
    for src, env, func in [('synoptic','SYNOPTIC_API_TOKEN',getter_synoptic), ('mping','MPING_API_KEY',getter_mping)]:
        try:
            sections[src] = func(when, minutes=args.window_minutes, **{'token' if src=='synoptic' else 'key':os.getenv(env)})
        except Exception as exc:
            failures.append(src + ': ' + str(exc))
            # Retain previous valid observations for an unavailable provider.
            sections[src] = [f for f in (old or {}).get('features', []) if f.get('properties',{}).get('source')==src]
    if len(failures) == 2 and not (old or {}).get('features'):
        raise RuntimeError('; '.join(failures))
    feats = sections['synoptic'] + sections['mping']
    # A station or report may occur more than once due to derived variables.
    unique = {}
    for f in feats:
        p = f['properties']
        unique[(p['source'],p['id'],p['observed_time'])] = f
    return {'type':'FeatureCollection','schema':SCHEMA,'valid_time':when.isoformat().replace('+00:00','Z'),
            'window_minutes':args.window_minutes,'provider_status':{'synoptic':'stale' if any(x.startswith('synoptic:') for x in failures) else 'ok',
                                                                    'mping':'stale' if any(x.startswith('mping:') for x in failures) else 'ok'},
            'features':list(unique.values())}, failures



def path_for_hour(root, when):
    return root / 'ptype_observations' / (when.strftime('%Y%m%d%H') + '.json')


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(path.suffix + '.incoming')
    staging.write_text(json.dumps(payload, separators=(',',':'), ensure_ascii=True) + '\n',encoding='utf8')
    staging.replace(path)


def safe_publish(path, args):
    host, remote = args.publish_host, args.publish_dir
    if not re.fullmatch(r'[\w.@-]+', host) or not remote.startswith('/') or not re.fullmatch(r'[/\w.\-]+',remote):
        raise ValueError('Unsafe publishing destination')
    dest = remote.rstrip('/') + '/ptype_observations'
    remote_tmp = '.' + path.name + '.incoming'
    subprocess.run(['ssh','-o','BatchMode=yes',host,'mkdir -p '+dest],check=True,timeout=90)
    subprocess.run(['scp','-q','-o','BatchMode=yes',str(path),host+':'+dest+'/'+remote_tmp],check=True,timeout=90)
    subprocess.run(['ssh','-o','BatchMode=yes',host,'mv '+dest+'/'+remote_tmp+' '+dest+'/'+path.name],check=True,timeout=90)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--date',help='YYYYMMDD for a specific valid UTC date')
    p.add_argument('--hour',type=int,help='UTC valid hour (0-23), requires --date')
    p.add_argument('--lookback-hours',type=int,default=3,help='Recent completed valid hours to refresh (cron default 3)')
    p.add_argument('--start',help='UTC first valid hour (YYYYMMDDHH), for a bounded backfill')
    p.add_argument('--end',help='UTC final valid hour (YYYYMMDDHH), inclusive; requires --start')
    p.add_argument('--window-minutes',type=int,default=30,help='Symmetric matching window (default +/-30 minutes)')
    p.add_argument('--output-root',type=Path,default=Path('zarr'))
    p.add_argument('--publish',action='store_true',help='Publish sanitized GeoJSON to configured RZDM web host')
    p.add_argument('--rights-confirmed',action='store_true',help='Affirm permission to publicly display both provider datasets')
    p.add_argument('--publish-host',default='thultquist@rzdm.ncep.noaa.gov')
    p.add_argument('--publish-dir',default='/home/people/emc/www/htdocs/users/verification_restricted/test/trh/dynamic_viewer/data')
    p.add_argument('--dry-run',action='store_true')
    a=p.parse_args(argv)
    if a.publish and not a.rights_confirmed:
        p.error('Public publication requires --rights-confirmed (confirm both provider licenses first).')
    if not 1 <= a.window_minutes <= 120 or not 1 <= a.lookback_hours <= 48:
        p.error('Out-of-range window/lookback')
    if (a.date is None) != (a.hour is None):
        p.error('--date and --hour are required together')
    if (a.start is None) != (a.end is None):p.error('--start and --end are required together')
    if a.start and a.date:p.error('Use either --date/--hour or --start/--end')
    now=dt.datetime.now(UTC)
    if a.start:
        first=dt.datetime.strptime(a.start,'%Y%m%d%H').replace(tzinfo=UTC)
        last=dt.datetime.strptime(a.end,'%Y%m%d%H').replace(tzinfo=UTC)
        delta=int((last-first).total_seconds()/3600)
        if delta<0 or delta>167:p.error('Historical backfill must be 1–168 consecutive UTC hours')
        times=[first+dt.timedelta(hours=i) for i in range(delta+1)]
    elif a.date:
        if not 0<=a.hour<=23:p.error('--hour must be 0-23')
        start=dt.datetime.strptime(a.date,'%Y%m%d').replace(tzinfo=UTC,hour=a.hour)
        times=[start]
    else:
        # Wait until the entire symmetric time window has elapsed.
        latest=(now - dt.timedelta(minutes=a.window_minutes)).replace(minute=0,second=0,microsecond=0)
        times=[latest-dt.timedelta(hours=n) for n in range(a.lookback_hours)]
    failed_hours = []
    completed_hours = 0
    for valid_hour in times:
        if valid_hour+dt.timedelta(minutes=a.window_minutes)>now:
            LOG.info('Skip future/incomplete valid window %s',valid_hour);continue
        path=path_for_hour(a.output_root,valid_hour)
        if a.dry_run:
            print('Would fetch UTC valid hour',valid_hour.isoformat(),'->',path.name)
            continue
        old=None
        if path.is_file():
            try:
                prior=json.loads(path.read_text(encoding='utf8'))
                if prior.get('schema')==SCHEMA:old=prior
            except (ValueError,OSError): pass
        try:
            data, failures=build_hour(valid_hour,a,old)
            atomic_json(path,data)
            print('Observations',valid_hour.isoformat(),'Synoptic',sum(f['properties']['source']=='synoptic' for f in data['features']),
                  'mPING',sum(f['properties']['source']=='mping' for f in data['features']))
            for issue in failures:LOG.warning('%s %s',valid_hour,issue)
            if a.publish:safe_publish(path,a)
            completed_hours += 1
        except Exception as exc:
            # A single historical hour (often API time-range/authorization
            # failure) must not abort the remaining 168-hour case backfill.
            # Leave the failed hour absent rather than publish a false zero.
            failed_hours.append((valid_hour, str(exc)))
            LOG.error('%s observation update failed: %s',valid_hour,exc)
    if failed_hours:
        LOG.error('Observation run: %d succeeded; %d failed. First failed UTC hour: %s. '
                  'Retry the failed valid hours after resolving the provider/publish error.',
                  completed_hours,len(failed_hours),failed_hours[0][0].strftime('%Y%m%d%H'))
    return 1 if failed_hours else 0

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(levelname)s: %(message)s')
    sys.exit(main())
