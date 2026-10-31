import datetime as dt
import json
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import ptype_observations as obs

H=dt.datetime(2026,10,28,12,tzinfo=dt.timezone.utc)
class TestObservationMapping(unittest.TestCase):
    def test_synoptic_wnum(self):
        for code,kind in [(1,1),(20,2),(49,3),(57,4),(1*80+3,5),(20*6400+32*80+30,2)]:
            self.assertEqual(obs.classify_wnum(code),kind)
        for code in [0,30,32,41,-1]:self.assertIsNone(obs.classify_wnum(code))
    def test_mping_classification(self):
        examples={'Rain':1,'Drizzle':1,'Heavy Snow':2,'Ice Pellets/Sleet':4,
                  'Freezing Rain':3,'Mixed Ice Pellets and Snow':5,'Rain and Snow':5,
                  'Freezing Drizzle':3,'Blowing snow':None,'Freezing Fog':None,'Hail':None}
        for desc,expected in examples.items():
            with self.subTest(desc=desc): self.assertEqual(obs.classify_description(desc),expected)
    def test_synoptic_restriction_and_time(self):
        def station(stid,restricted,time,value):return {'STID':stid,'LATITUDE':'39.0','LONGITUDE':'-90.0','RESTRICTED':restricted,
            'OBSERVATIONS':{'weather_cond_code_value_1':{'date_time':time,'value':value}}}
        data={'SUMMARY':{'RESPONSE_CODE':1},'STATION':[
            station('OK',False,'2026-10-28T11:50:00Z',23),
            station('NO',True,'2026-10-28T12:00:00Z',20),
            station('OLD',False,'2026-10-28T11:20:00Z',1),
            station('UNKNOWN',None,'2026-10-28T12:00:00Z',1)]}
        with patch.object(obs,'json_request',return_value=data) as mock:
            ret=obs.get_synoptic(H,token='FAKE')
        self.assertEqual([(x['properties']['station'],x['properties']['ptype']) for x in ret],[('OK',4)])
        self.assertIn('attime=202610281230',mock.call_args.args[0]);self.assertIn('within=60',mock.call_args.args[0])
        self.assertIn('network=1',mock.call_args.args[0]);self.assertIn('hfmetars=0',mock.call_args.args[0])
    def test_mping_time_and_category(self):
        data={'next':None,'results':[{'category':'Rain/Snow','description':'Rain',
            'geom':{'type':'Point','coordinates':[-89,40]},'id':1,'obtime':'2026-10-28T12:09:00Z'},
            {'category':'Hail','description':'Hail','geom':{'coordinates':[-89,40]},'obtime':'2026-10-28T12:00:00Z'}]}
        with patch.object(obs,'json_request',return_value=data) as mock:
            ret=obs.get_mping(H,key='FAKE')
        self.assertEqual(len(ret),1)
        self.assertIn('in_bbox=',mock.call_args.args[0]);self.assertEqual(mock.call_args.kwargs['headers']['Authorization'],'Token FAKE')
    def test_partial_api_failure_preserves_previous_provider(self):
        old={'features':[obs.feature('mping',[-90,40],2,H,'Snow','mPING',1)]}
        args=type('Args',(),{'window_minutes':30})()
        with patch.dict(os.environ,{'SYNOPTIC_API_TOKEN':'x','MPING_API_KEY':'y'}):
            new,problems=obs.build_hour(H,args,old,lambda *a,**kw:[],lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('offline')))
        self.assertEqual(len(problems),1);self.assertEqual(len(new['features']),1)
        self.assertEqual(new['provider_status']['mping'],'stale')
    def test_credentials_not_in_json(self):
        with tempfile.TemporaryDirectory() as t:
            path=obs.path_for_hour(Path(t),H)
            data={'type':'FeatureCollection','schema':obs.SCHEMA,'valid_time':H.isoformat().replace('+00:00','Z'),
                  'features':[obs.feature('mping',[-89,38],1,H,'Rain','mPING',123)]}
            obs.atomic_json(path,data)
            self.assertNotIn('key',path.read_text())
            self.assertEqual(json.loads(path.read_text()),data)
if __name__=='__main__': unittest.main()

class TestHistoricalObservationResilience(unittest.TestCase):
    def test_failed_early_hour_does_not_abort_remaining_backfill(self):
        """Regression: one API failure at F000 must not discard F001 onward."""
        start=dt.datetime(2026,10,26,0,tzinfo=dt.timezone.utc)
        end=start+dt.timedelta(hours=2)
        successful={'type':'FeatureCollection','schema':obs.SCHEMA,
            'valid_time':(start+dt.timedelta(hours=1)).isoformat().replace('+00:00','Z'),
            'features':[],'provider_status':{'synoptic':'ok','mping':'ok'}}
        def build_hour(hour,*_):
            if hour==start:raise RuntimeError('temporary provider timeout')
            return {**successful,'valid_time':hour.isoformat().replace('+00:00','Z')},[]
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(obs,'build_hour',side_effect=build_hour) as mocked:
                result=obs.main(['--start','2026102600','--end','2026102602',
                                 '--output-root',tmp])
            self.assertEqual(result,1,'failed hour must not be concealed')
            self.assertEqual(mocked.call_count,3,'all hours must be attempted')
            self.assertFalse(obs.path_for_hour(Path(tmp),start).exists())
            for offset in (1,2):
                f=obs.path_for_hour(Path(tmp),start+dt.timedelta(hours=offset))
                self.assertTrue(f.is_file())
                self.assertEqual(json.loads(f.read_text())['schema'],obs.SCHEMA)
