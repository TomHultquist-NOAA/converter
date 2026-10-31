"""Regression coverage: changing RAP requests must invalidate old complete stores."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import verification_pipeline as pipeline
import verification_ingest as ingest

CONFIG = Path(__file__).resolve().parents[1] / 'verification_sources.json'
UTC = dt.timezone.utc

class InventoryRefreshTests(unittest.TestCase):
    def test_stale_manifest_with_only_three_levels_is_not_current(self):
        cfg = json.loads(CONFIG.read_text())
        old = {'status':'complete', 'selected':[
            {'field':'t700','inventory':'1:0:d=2026102812:TMP:700 mb:anl:'},
            {'field':'t850','inventory':'2:0:d=2026102812:TMP:850 mb:anl:'},
            {'field':'t925','inventory':'3:0:d=2026102812:TMP:925 mb:anl:'}], 'missing':[]}
        self.assertFalse(pipeline.manifest_matches_current_selection(old,cfg['fields']['rap_pressure']))

    def test_explicitly_missing_native_fields_are_accounted_for(self):
        wanted = {'h300':['HGT','300 mb'],'mucape':['CAPE','255-0 mb above ground']}
        manifest = {'status':'partial', 'selected':[{'field':'h300','inventory':'4:0:d=2026102812:HGT:300 mb:anl:'}], 'missing':['mucape']}
        self.assertTrue(pipeline.manifest_matches_current_selection(manifest,wanted))
        self.assertFalse(pipeline.manifest_matches_current_selection({**manifest,'missing':[]},wanted))

    def test_wrong_mucape_layer_invalidates_staging(self):
        wanted = {'mucape':['CAPE','255-0 mb above ground']}
        old = {'status':'complete','selected':[{'field':'mucape','inventory':'4:0:d=2026102812:CAPE:180-0 mb above ground:anl:'}], 'missing':[]}
        self.assertFalse(pipeline.manifest_matches_current_selection(old,wanted))
        self.assertTrue(pipeline.manifest_matches_current_selection(
            {**old,'selected':[{'field':'mucape','inventory':'4:0:d=2026102812:CAPE:255-0 mb above ground:anl:'}]},wanted))

    def test_complete_stage_is_not_reused_with_changed_native_layer(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root/'rap_pressure/20261028'
            path.mkdir(parents=True)
            grib = path/'rap_pressure_2026102812.grib2'
            grib.write_bytes(b'GRIB' + b'\x00'*100)
            old = {'status':'complete','file':str(grib.relative_to(root)), 'selected':[
                    {'field':'mucape','inventory':'2:0:d=2026102812:CAPE:180-0 mb above ground:anl:'}], 'missing':[]}
            manifest = path/'rap_pressure_2026102812.json'
            manifest.write_text(json.dumps(old))
            cfg = {'fields':{'rap_pressure':{'mucape':['CAPE','255-0 mb above ground']}},
                   'derived_at_zarr_stage':{},'source_delays_minutes':{'rap':0}}
            with patch.object(ingest.Client,'download', side_effect=RuntimeError('intentionally called')) as download:
                result = None
                try:
                    result=ingest.process_job(ingest.Client(),'/bin/true',root,
                        dt.datetime(2026,10,28,12,tzinfo=UTC),
                        ('rap_pressure','https://example.invalid/rap.grib2','rap'),
                        cfg, dt.datetime(2026,10,30,18,tzinfo=UTC))
                except RuntimeError as exc:
                    self.assertEqual(str(exc),'intentionally called')
                self.assertTrue(download.called, 'Old MUCAPE selector should force a fresh source inventory')
                self.assertNotEqual(result,'already')

    def test_hourly_workflow_revisits_existing_but_obsolete_store(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            zarr=root/'zarr'
            work=root/'work'
            store=zarr/'analysis'/'analysis_rap_pressure_2026102812.zarr'
            (store/'t700').mkdir(parents=True)
            (store/'t700'/'0.0.0').write_bytes(b'not-empty')
            (store/'.conversion.json').write_text(json.dumps({'fields':['t700']}))
            manifest=work/'verification_staging'/'rap_pressure'/'20261028'/'rap_pressure_2026102812.json'
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({'status':'complete','selected':[
                {'field':'t700','inventory':'1:0:d=2026102812:TMP:700 mb:anl:'}], 'missing':[]}))
            protection=root/'protected_cycles.json'
            protection.write_text(json.dumps({'schema':'omd-evu-protected-cycles-1','protected_cycles':[], 'protected_analysis':[]}))
            args=argparse.Namespace(protected_cycles_file=protection,config=CONFIG,
                 lookback_hours=20,nohrsc_lookback_hours=72,mrms_stride=1,wgrib2='/bin/true',
                 output_root=str(zarr),work_root=str(work),no_publish=True,timeout=1,retries=1,
                 source='rap',force=False,inspect_grid=False,compression_level=3,keep_grib=False,
                 retention_days=7,no_local_cleanup=True,start_hour='2026102812',end_hour='2026102812')
            calls=[]
            def mock_stage(*args_,**kwargs_):
                calls.append('re-inventory')
                # Simulation stops after entering the right recovery path.
                return 'pending'
            with patch.object(pipeline.ingest,'jobs_for',return_value=[
                     ('rap_pressure','https://example.invalid/rap.grib2','rap')]), \
                 patch.object(pipeline.ingest,'valid_now',return_value=True), \
                 patch.object(pipeline.ingest,'process_job',side_effect=mock_stage), \
                 patch.object(pipeline.convert,'build_catalog',return_value=1):
                result=pipeline.run(args,lambda *a,**k:None,lambda *a,**k:None,
                    now=dt.datetime(2026,10,30,18,tzinfo=UTC))
            self.assertEqual(result,0)
            self.assertEqual(calls,['re-inventory'])

if __name__ == '__main__':
    unittest.main()
