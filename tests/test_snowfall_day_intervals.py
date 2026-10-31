"""Regression tests: GRIB ASNOW day intervals and safe staged recovery."""
import importlib.util
import json
import tempfile
import unittest
import zlib
from array import array
from pathlib import Path

MODULE=Path(__file__).resolve().parents[1]/'multi_model_to_zarr_wcoss2.py'
spec=importlib.util.spec_from_file_location('model_converter_asnow_test',MODULE)
converter=importlib.util.module_from_spec(spec)
spec.loader.exec_module(converter)

class TestSnowfallAccumulations(unittest.TestCase):
    def test_interval_hours_and_days(self):
        f=converter.parse_acc_interval
        for text,lead,expected in [
            ('100:200:d=2026102912:ASNOW:surface:0-1 day acc f:',24,(0,24)),
            ('100:200:d=2026102912:ASNOW:surface:0-2 day acc f:',48,(0,48)),
            ('100:200:d=2026102912:ASNOW:surface:0-3 day acc f:',72,(0,72)),
            ('100:200:d=2026102912:ASNOW:surface:0-0 day acc f:',0,(0,0)),
            ('100:200:d=2026102912:ASNOW:surface:1-2 day acc f:',48,(24,48)),
            ('100:200:d=2026102912:ASNOW:surface:24 hour acc:',48,(24,48)),
            ('100:200:d=2026102912:ASNOW:surface:0-24 hour acc:',24,(0,24)),
            ('100:200:d=2026102912:ASNOW:surface:6 hr acc:',9,(3,9)),
        ]:
            with self.subTest(text=text):self.assertEqual(f(text,lead),expected)
        self.assertEqual(f('ASNOW:surface:acc',24),'unknown')
        self.assertIsNone(f('ASNOW:surface:24 hour fcst',24))
    def test_ranking_prefers_correct_cumulative_day(self):
        rows=[{'msg':1,'short_name':'ASNOW','text':'ASNOW:surface:23-24 hour acc'},
              {'msg':2,'short_name':'ASNOW','text':'ASNOW:surface:0-1 day acc f'}]
        self.assertEqual(converter.choose_field(rows,'asnow',24)['msg'],2)
    def test_recover_staging_without_overwriting_other_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            snow=root/'asnow';snow.mkdir()
            stage=root/'.accum_intervals'/'asnow';stage.mkdir(parents=True)
            raw=array('f',[0.5,1.0,1.5]).tobytes()
            (stage/'24.zlib').write_bytes(zlib.compress(raw))
            manifest={'forecast_hours':{'024':{'fields':['t2m'],'pending':{'asnow':'unknown'},
                'selections':{'asnow':{'inventory':'20:123:d=2026102912:ASNOW:surface:0-1 day acc f:',
                                        'interval':'unknown'}}}}}
            # Match a synthetic tiny three-cell test grid; production constants
            # are restored in finally so other tests remain unaffected.
            original=converter.core.FIELD_BYTES
            converter.core.FIELD_BYTES=len(raw)
            try:
                recovered=converter.restore_staged_snowfall(root,manifest,3,[24])
            finally:
                converter.core.FIELD_BYTES=original
            self.assertEqual(recovered,[24])
            self.assertEqual(zlib.decompress((snow/'24.0.0').read_bytes()),raw)
            self.assertEqual(manifest['forecast_hours']['024']['selections']['asnow']['interval'],[0,24])
            self.assertNotIn('asnow',manifest['forecast_hours']['024']['pending'])
            self.assertIn('t2m',manifest['forecast_hours']['024']['fields'])
            self.assertEqual(converter.restore_staged_snowfall(root,manifest,3,[24]),[])
    def test_no_unverified_staged_data_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            stage=root/'.accum_intervals'/'asnow';stage.mkdir(parents=True)
            (stage/'24.zlib').write_bytes(zlib.compress(b'fake'))
            manifest={'forecast_hours':{'024':{'selections':{'asnow':{'inventory':'ASNOW:surface:acc'}}}}}
            self.assertEqual(converter.restore_staged_snowfall(root,manifest,3,[24]),[])
            self.assertFalse((root/'asnow'/'24.0.0').exists())
    def test_backfill_arg_restrictions(self):
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/'protected_cycles.json'
            f.write_text('{"schema":"omd-evu-protected-cycles-1","protected_cycles":[]}')
            a=converter.arguments(['--date','20261029','--cycle','12','--models','hrrr',
                                   '--repair-snowfall','--no-publish','--no-local-cleanup',
                                   '--protected-cycles-file',str(f)])
            self.assertTrue(a.repair_snowfall)

if __name__=='__main__':unittest.main()
