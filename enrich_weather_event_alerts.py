#!/usr/bin/env python3
"""Optional, fail-open (to annotated unknown) event/WWA correlation.

Standard library, Python 3.6-compatible. Never modifies source catalog/Zarr.
Matches selected scanner groups to IEM VTEC county/zone and storm-based warnings
near their representative forecast locations/times. This is NOT observed impact
verification, and US-only archives do not establish coverage elsewhere.
"""
from __future__ import print_function
import argparse
import datetime as dt
import json
import os
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error

BASE = 'https://mesonet.agron.iastate.edu'
USER_AGENT = 'EVU-WeatherEvents-experimental/1.0 (read-only event context)'
PHENOMENA = {'TO':'Tornado','SV':'Severe Thunderstorm','FF':'Flash Flood',
             'FA':'Flood','FL':'Flood','MA':'Special Marine','HU':'Hurricane',
             'TR':'Tropical Storm','HI':'High Wind','HW':'High Wind',
             'WW':'Winter Weather','WS':'Winter Storm','BZ':'Blizzard',
             'IS':'Ice Storm','SN':'Snow','RP':'Rip Current','CF':'Coastal Flood',
             'SU':'High Surf','EH':'Excessive Heat','HT':'Heat','FG':'Dense Fog',
             'WI':'Wind','LE':'Lake Effect Snow','FR':'Frost','FZ':'Freeze'}
SIG = {'W':'Warning','A':'Watch','Y':'Advisory','S':'Statement'}

def parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.strptime(value[:19], '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return None

def iso(value):
    return value.strftime('%Y-%m-%dT%H:%M:%SZ')

def safe_write(path, payload):
    directory = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(directory):
        os.makedirs(directory)
    fd, temp = tempfile.mkstemp(prefix='.weather_events_alerts_', suffix='.json', dir=directory)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
        os.rename(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)

def read_url(url, timeout=5, max_bytes=1500000):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT,
                                               'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        length = response.headers.get('Content-Length')
        if length and int(length) > max_bytes:
            raise ValueError('Alert response exceeds size limit')
        blob = response.read(max_bytes + 1)
    if len(blob) > max_bytes:
        raise ValueError('Alert response exceeds size limit')
    return json.loads(blob.decode('utf-8'))

def normalized_alert(item, source):
    """Conservative normalization; never claim precise geometry for IEM VTEC UGC."""
    if not isinstance(item, dict):
        return None
    props = item.get('properties', item)
    if not isinstance(props, dict): return None
    phen = props.get('phenomena', '')
    sig = props.get('significance', '')
    if props.get('name'):
        event = str(props['name'])
    elif props.get('event'):
        event = str(props['event'])
    else:
        event = (PHENOMENA.get(phen, phen) + ' ' + SIG.get(sig, sig)).strip()
    if not event: return None
    issue = props.get('issue') or props.get('issued') or props.get('start') or props.get('effective')
    expires = props.get('expire') or props.get('expires') or props.get('end')
    if not parse_time(issue) or not parse_time(expires):
        return None
    purl = props.get('url') or props.get('@id') or ''
    if purl.startswith('/'):
        purl = BASE + purl
    if purl and not purl.startswith('https://'):
        purl = ''
    result = {
        'event': event[:130], 'issued': issue, 'expires': expires,
        'source': source, 'link': purl,
        'location_match': ('storm_based_polygon_or_buffer' if source=='IEM storm-based warnings'
                           else 'county_or_zone_context'),
        'event_id': str(props.get('eventid') or props.get('id') or '')[:120],
        'wfo': str(props.get('wfo') or '')[:12],
        'ugc': str(props.get('ugc') or '')[:24],
        'significance': SIG.get(sig, sig),
    }
    return result

def sample_times(group, max_samples=2):
    # Candidate timestamps, not a continuous claim of hazard overlap.
    scenes = group.get('scenes') or []
    times = []
    for ev in scenes:
        when = parse_time(ev.get('valid_time'))
        if when and when not in times: times.append(when)
    for key in ('start_time','end_time'):
        when = parse_time(group.get(key))
        if when and when not in times: times.append(when)
    times.sort()
    if not times: return []
    if max_samples==1 or len(times)==1: return [times[len(times)//2]]
    if max_samples==2: return [times[0],times[-1]]
    result=[times[0],times[-1]]
    mid=times[len(times)//2]
    if mid not in result: result.insert(1,mid)
    return sorted(result[:max_samples])

def alert_key(item):
    return (item['source'],item['event'],item['issued'],item['expires'],
            item.get('wfo'),item.get('event_id'))

def correlate_group(group, fetcher=read_url, now=None, max_samples=2,
                    timeout=5, max_matches=8, sbw=True, max_queries=8, sleep=0):
    """Query IEM for a small number of points/times; return transparent provenance."""
    now=now or dt.datetime.utcnow()
    location=group.get('location') or {}
    lat=location.get('latitude');lon=location.get('longitude')
    base={'status':'not_checked', 'source_scope':'IEM archived US VTEC and storm-based warnings',
          'checked_utc':iso(now), 'matches':[], 'match_count':0,
          'notes':['Warning/advisory issuance does not verify an observed impact.'],
          'samples_checked':0, 'queries_failed':0}
    if not isinstance(lat,(float,int)) or not isinstance(lon,(float,int)) or not (-90<=lat<=90 and -180<=lon<=180):
        base['status']='location_unavailable';return base
    observations=sample_times(group,max_samples)
    if not observations:
        base['status']='time_unavailable';return base
    # Large offshore/high latitude areas remain explicitly US-only source scope.
    def norm(x):return str(round(x,3))
    matches={};failures=0;attempts=0
    for t in observations:
        qs={'lat':norm(lat),'lon':norm(lon)}
        specs=[('IEM VTEC county/zone', 'json/vtec_events_bypoint.py', 'at', 'events')]
        if sbw:specs.append(('IEM storm-based warnings','json/sbw_by_point.py','valid','sbws'))
        for name,path,dateparam,key in specs:
            if attempts>=max_queries:break
            params=dict(qs);params[dateparam]=iso(t)
            url=BASE+'/'+path+'?'+urllib.parse.urlencode(params)
            attempts+=1
            try:
                payload=fetcher(url, timeout=timeout)
                if not isinstance(payload,dict) or not isinstance(payload.get(key),list):
                    raise ValueError('Unexpected IEM alert schema')
                for raw in payload[key]:
                    item=normalized_alert(raw,name)
                    if item: matches[alert_key(item)]=item
            except (OSError, ValueError, TypeError, urllib.error.URLError, json.JSONDecodeError) as e:
                failures+=1
                base['notes'].append('%s lookup unavailable: %s' % (name, str(e)[:120]))
            if sleep: time.sleep(sleep)
        base['samples_checked']+=1
    vals=sorted(matches.values(), key=lambda x:(x['issued'],x['event']), reverse=True)
    base['matches']=vals[:max_matches]
    base['match_count']=len(vals)
    base['queries_failed']=failures
    base['queries_attempted']=attempts
    base['sample_times_utc']=[iso(x) for x in observations]
    base['status']=('matched' if vals else 'unavailable' if failures==attempts else
                    'partial_coverage' if failures else
                    'no_matching_us_alerts')
    if failures and vals:base['status']='partial_coverage_matched'
    if len(vals)>max_matches:
        base['notes'].append('Only the first %d of %d unique alert records are displayed.' % (max_matches,len(vals)))
    if min(observations)>now:
        base['notes'].append('Forecast valid time is in the future; additional alerts may be issued later.')
        if not vals and not failures:
            base['status']='forecast_period_upcoming'
    base['notes'].append('IEM VTEC county/zone lookup is broader than actual storm-based warning polygons.')
    return base

def enrich_document(data, fetcher=read_url, now=None, max_groups=20, max_samples=2,
                    timeout=5, max_matches=8, sbw=True, sleep=0):
    if data.get('schema')!='evu-event-discovery-preview-2':
        raise ValueError('Unsupported scanner JSON schema')
    groups=data.get('event_groups')
    if not isinstance(groups,list):raise ValueError('Scanner event_groups missing')
    now=now or dt.datetime.utcnow()
    count=0
    for g in groups:
        if count>=max_groups:
            g['alert_context']={'status':'not_checked_limit','matches':[], 'match_count':0}
            continue
        g['alert_context']=correlate_group(g,fetcher,now,max_samples,timeout,max_matches,sbw,sleep=sleep)
        count+=1
    # Summaries reflect the same observation evidence as the selected scene set.
    by_id={g['id']:g['alert_context'] for g in groups}
    for item in data.get('candidate_group_summaries',[]):
        if item.get('id') in by_id:item['alert_context']=by_id[item['id']]
    data['alert_enrichment']={
        'source':'Iowa Environmental Mesonet historical VTEC/Storm Based Warning services',
        'method':'point/time query, county-zone context plus storm-based polygons (not area-wide overlap)',
        'checked_utc':iso(now),
        'groups_checked':count,
        'groups_with_matches':sum(x['alert_context'].get('match_count',0)>0 for x in groups),
        'groups_unavailable':sum('unavailable' in x['alert_context'].get('status','') or
                                   x['alert_context'].get('queries_failed',0)>0 for x in groups),
        'disclaimer':'Issued watches, warnings, and advisories reflect operational hazard awareness, not verified observations, losses, or impacts. No alert match is not evidence of safety.',
    }
    return data

def main(argv=None):
    p=argparse.ArgumentParser(description='Optional IEM WWA context for selected experimental weather events')
    p.add_argument('--input',required=True)
    p.add_argument('--output',required=True)
    p.add_argument('--max-groups',type=int,default=20)
    p.add_argument('--samples',type=int,choices=(1,2,3),default=2)
    p.add_argument('--timeout',type=float,default=5)
    p.add_argument('--max-matches',type=int,default=8)
    p.add_argument('--delay',type=float,default=.10,help='Courtesy pause between requests (seconds)')
    p.add_argument('--no-storm-based',action='store_true')
    args=p.parse_args(argv)
    with open(args.input,encoding='utf-8') as handle:data=json.load(handle)
    data=enrich_document(data,max_groups=args.max_groups,max_samples=args.samples,
                         timeout=args.timeout,max_matches=args.max_matches,
                         sbw=not args.no_storm_based,sleep=args.delay)
    safe_write(args.output,data)
    a=data['alert_enrichment']
    print('Alert context: %s groups checked, %s with matches, %s incomplete lookups' %
          (a['groups_checked'],a['groups_with_matches'],a['groups_unavailable']))
    print('Output:',args.output)

if __name__=='__main__':main()
