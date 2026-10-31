#!/usr/bin/env python3
"""Collect SPC preliminary local storm reports, one 12Z--12Z report day per JSON.

This is an observation-only process; it does not modify GRIB or Zarr datasets.
Use --start-day YYMMDD --end-day YYMMDD for historical backfills. Only
published preliminary reports are plotted, not surveyed tornado tracks.
"""
from __future__ import annotations
import argparse, csv, datetime as dt, io, json, math, os, re, subprocess, sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
UTC=dt.timezone.utc
SCHEMA='omd-evu-spc-lsr-1'
ROOT='https://www.spc.noaa.gov/climo/reports/'
TYPES={'f_scale':'tornado','fscale':'tornado','speed':'wind','size':'hail'}


def report_date(label):
    return dt.datetime.strptime(label,'%y%m%d').replace(tzinfo=UTC,hour=12)


def parse_csv(body, day):
    """Parse SPC combined filtered CSV with repeating headers for each hazard."""
    anchor=report_date(day); typ=None; reports=[];seen=set()
    for row in csv.reader(io.StringIO(body)):
        if not row: continue
        row=[s.strip() for s in row]
        if len(row)>1 and row[0].lstrip('\ufeff').lower()=='time':
            typ=TYPES.get(re.sub(r'[^a-z_]','',row[1].lower()))
            continue
        if typ is None or len(row)<8:continue
        time=row[0].zfill(4)
        if not re.fullmatch(r'[0-2][0-9][0-5][0-9]',time):continue
        hour=int(time[:2]);minute=int(time[2:]);
        if hour>23:continue
        when=(anchor+dt.timedelta(days=1 if hour<12 else 0)).replace(hour=hour,minute=minute)
        try: lat=float(row[5]);lon=float(row[6])
        except ValueError:continue
        if not all(map(math.isfinite,(lat,lon))) or not (-170<=lon<=-60 and 15<=lat<=72):continue
        ident=(typ,when.isoformat(),round(lat,4),round(lon,4),row[1])
        if ident in seen:continue
        seen.add(ident)
        reports.append({'type':'Feature','geometry':{'type':'Point','coordinates':[lon,lat]},
          'properties':{'type':typ,'time':when.isoformat().replace('+00:00','Z'),
          'magnitude':row[1][:24],'location':row[2][:100],
          'county':row[3][:80],'state':row[4][:12],
          'remarks':row[7][:500]}})
    return reports


def fetch(day):
    url=f'{ROOT}{day}_rpts_filtered.csv'
    request=Request(url,headers={'User-Agent':'OMD-EVU-SPC-Reports/1.0','Accept':'text/csv'})
    with urlopen(request,timeout=35) as response:
        raw=response.read(3_000_001)
    if len(raw)>3_000_000:raise ValueError('SPC CSV unusually large')
    return raw.decode('utf-8-sig',errors='replace')


def publish(path,host,destination):
    if not re.fullmatch(r'[\w.@-]+',host) or not re.fullmatch(r'/[\w./-]+',destination):
        raise ValueError('Unsafe publishing destination')
    target=destination.rstrip('/')+'/spc_reports';tmp='.'+path.name+'.incoming'
    subprocess.run(['ssh','-o','BatchMode=yes',host,'mkdir -p '+target],check=True,timeout=90)
    subprocess.run(['scp','-q','-o','BatchMode=yes',str(path),host+':'+target+'/'+tmp],check=True,timeout=90)
    subprocess.run(['ssh','-o','BatchMode=yes',host,'mv '+target+'/'+tmp+' '+target+'/'+path.name],check=True,timeout=90)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--lookback-days',type=int,default=3)
    p.add_argument('--start-day',help='YYMMDD (SPC 12 UTC report day)')
    p.add_argument('--end-day',help='YYMMDD (inclusive)')
    p.add_argument('--output-root',type=Path,default=Path('zarr'))
    p.add_argument('--publish',action='store_true')
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('--publish-host',default='thultquist@rzdm.ncep.noaa.gov')
    p.add_argument('--publish-dir',default='/home/people/emc/www/htdocs/users/verification_restricted/test/trh/dynamic_viewer/data')
    a=p.parse_args(argv)
    if (a.start_day is None)!=(a.end_day is None):p.error('Provide both --start-day and --end-day')
    if a.start_day:
        try:first=report_date(a.start_day);last=report_date(a.end_day)
        except ValueError:p.error('SPC dates must be YYMMDD')
        if last<first or (last-first).days>30:p.error('Range must be 1–31 SPC days')
    else:
        if not 1<=a.lookback_days<=14:p.error('--lookback-days must be 1–14')
        now=dt.datetime.now(UTC)
        today=(now-dt.timedelta(hours=12)).strftime('%y%m%d')
        last=report_date(today);first=last-dt.timedelta(days=a.lookback_days-1)
    errors=0
    days=(last-first).days+1
    for i in range(days):
        day=(first+dt.timedelta(days=i)).strftime('%y%m%d')
        path=a.output_root/'spc_reports'/f'{day}.json'
        if a.dry_run:
            print(day,'->',path);continue
        try:
            report=parse_csv(fetch(day),day)
            data={'type':'FeatureCollection','schema':SCHEMA,'spc_day':day,
                  'start_utc':report_date(day).isoformat().replace('+00:00','Z'),
                  'end_utc':(report_date(day)+dt.timedelta(days=1)).isoformat().replace('+00:00','Z'),
                  'source':'NOAA SPC preliminary filtered storm reports',
                  'features':report}
            path.parent.mkdir(parents=True,exist_ok=True)
            temp=path.with_suffix('.json.incoming');temp.write_text(json.dumps(data,separators=(',',':'))+'\n');temp.replace(path)
            print('SPC',day,'reports',len(report),flush=True)
            if a.publish:publish(path,a.publish_host,a.publish_dir)
        except HTTPError as exc:
            if exc.code==404:
                # During early publication a current SPC day may not have a CSV
                # yet. Leave previously collected data untouched and retry later.
                print('SPC',day,'CSV not yet available (HTTP 404); will retry',file=sys.stderr)
            else:
                print('SPC',day,'HTTP',exc.code,'; will retry',file=sys.stderr);errors+=1
        except (URLError,OSError,ValueError,subprocess.CalledProcessError) as exc:
            print('SPC',day,'fetch/publish failed:',type(exc).__name__,str(exc)[:160],file=sys.stderr)
            errors+=1
    return int(errors>0)

if __name__=='__main__':sys.exit(main())
