"""Ensure staged RAP fields are represented in committed conversion metadata."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import verification_pipeline as pipeline

CONFIG=Path(__file__).resolve().parents[1]/'verification_sources.json'
UTC=dt.timezone.utc

class ConversionManifestConsistencyTests(unittest.TestCase):
    def test_selected_mucape_missing_from_conversion_triggers_repair(self):
        with tempfile.TemporaryDirectory() as td:
            store=Path(td)/'analysis_rap_pressure_2026102812.zarr'
            store.mkdir()
            (store/'.conversion.json').write_text(json.dumps({'fields':['h500','t700']}))
            for field in ('h500','t700'):
                (store/field).mkdir()
                (store/field/'0.0.0').write_bytes(b'chunk')
            manifest={'product':'rap_pressure','status':'complete','selected':[
                {'field':'t700'}, {'field':'h500'}, {'field':'mucape'}], 'missing':[]}
            self.assertTrue(pipeline.completed_store(store),'old conversion is internally complete')
            self.assertFalse(pipeline.converted_store_matches_staging(store,manifest),
                             'staged but unconverted MUCAPE must invalidate earlier conversion')
            (store/'mucape').mkdir()
            (store/'mucape'/'0.0.0').write_bytes(b'legacy chunk')
            self.assertFalse(pipeline.converted_store_matches_staging(store,manifest),
                             'orphan native chunk without metadata still requires repair')
            (store/'.conversion.json').write_text(json.dumps({'fields':['h500','t700','mucape']}))
            self.assertTrue(pipeline.converted_store_matches_staging(store,manifest))

    def test_missing_native_field_does_not_cause_redownload(self):
        with tempfile.TemporaryDirectory() as td:
            store=Path(td)
            (store/'.conversion.json').write_text(json.dumps({'fields':['t700']}))
            manifest={'product':'rap_pressure','status':'partial','selected':[{'field':'t700'}],
                      'missing':['mucape']}
            self.assertTrue(pipeline.converted_store_matches_staging(store,manifest))

    def test_derived_fields_reconcile(self):
        with tempfile.TemporaryDirectory() as td:
            store=Path(td)
            (store/'.conversion.json').write_text(json.dumps({'fields':['u300','v300','wspd300','shear06']}))
            manifest={'product':'rap_pressure','status':'complete','selected':[
                {'field':'u300'},{'field':'v300'},{'field':'shru06'},{'field':'shrv06'}]}
            self.assertTrue(pipeline.converted_store_matches_staging(store,manifest))
            (store/'.conversion.json').write_text(json.dumps({'fields':['u300','v300','wspd300']}))
            self.assertFalse(pipeline.converted_store_matches_staging(store,manifest))

    def test_current_staging_but_old_conversion_is_revisited(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);zarr=root/'zarr';work=root/'work'
            store=zarr/'analysis'/'analysis_rap_pressure_2026102812.zarr'
            (store/'t700').mkdir(parents=True)
            (store/'t700'/'0.0.0').write_bytes(b'data')
            (store/'.conversion.json').write_text(json.dumps({'fields':['t700']}))
            cfg=json.loads(CONFIG.read_text())
            fields=cfg['fields']['rap_pressure']
            manifest={'status':'partial','product':'rap_pressure',
                      'selected':[{'field':'t700'},{'field':'mucape'}],
                      'missing':sorted(set(fields)-{'t700','mucape'}),'file':'rap_pressure/20261028/test.grib2'}
            mfile=work/'verification_staging'/'rap_pressure'/'20261028'/'rap_pressure_2026102812.json'
            mfile.parent.mkdir(parents=True);mfile.write_text(json.dumps(manifest))
            self.assertTrue(pipeline.manifest_matches_current_selection(manifest,fields))
            self.assertFalse(pipeline.converted_store_matches_staging(store,manifest))
            policy=root/'protected_cycles.json';policy.write_text(json.dumps({
                'schema':'omd-evu-protected-cycles-1','protected_cycles':[],'protected_analysis':[]}))
            args=argparse.Namespace(protected_cycles_file=policy,config=CONFIG,
                  lookback_hours=20,nohrsc_lookback_hours=72,mrms_stride=1,wgrib2='/bin/true',
                  output_root=str(zarr),work_root=str(work),no_publish=True,timeout=1,retries=1,
                  source='rap',force=False,inspect_grid=False,compression_level=3,keep_grib=False,
                  retention_days=7,no_local_cleanup=True,start_hour='2026102812',end_hour='2026102812')
            calls=[]
            with patch.object(pipeline.ingest,'jobs_for',return_value=[
                   ('rap_pressure','https://example.invalid/rap','rap')]), \
                 patch.object(pipeline.ingest,'valid_now',return_value=True), \
                 patch.object(pipeline.ingest,'process_job',side_effect=lambda *a,**k: calls.append('stage') or 'pending'), \
                 patch.object(pipeline.convert,'build_catalog',return_value=1):
                status=pipeline.run(args,lambda *a,**kw:None,lambda *a,**kw:None,
                    now=dt.datetime(2026,10,30,18,tzinfo=UTC))
            self.assertEqual(status,0)
            self.assertEqual(calls,['stage'])

if __name__=='__main__': unittest.main()
