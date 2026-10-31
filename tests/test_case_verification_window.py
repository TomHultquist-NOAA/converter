"""Historical fixed UTC analysis windows (including protected hours older than retention)."""
import argparse
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import verification_pipeline as pipeline
UTC=dt.timezone.utc

class HistoryTest(unittest.TestCase):
    def test_protected_historic_hours_are_considered(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);protect=root/'protected_cycles.json'
            protect.write_text(json.dumps({'schema':'omd-evu-protected-cycles-1',
                 'protected_cycles':['20260801_00'], 'protected_analysis':[]}))
            args=argparse.Namespace(protected_cycles_file=protect,
                  config=Path(__file__).resolve().parents[1]/'verification_sources.json',
                  lookback_hours=20,nohrsc_lookback_hours=72,mrms_stride=1,wgrib2='/bin/true',
                  output_root=str(root/'zarr'),work_root=str(root/'work'),
                  no_publish=True, timeout=1,retries=1,source='all',force=False,inspect_grid=False,
                  compression_level=3,keep_grib=False,retention_days=14,no_local_cleanup=True,
                  start_hour='2026080100',end_hour='2026080102')
            checks=[]
            with patch.object(pipeline.ingest,'jobs_for',side_effect=lambda t,config: checks.append(t) or []):
                status=pipeline.run(args,lambda *a,**k:None,lambda *a,**k:None,
                      now=dt.datetime(2026,10,30,12,tzinfo=UTC))
            self.assertEqual(status,0)
            self.assertEqual([x.strftime('%Y%m%d%H') for x in checks],
                             ['2026080100','2026080101','2026080102'])
            self.assertTrue((root/'zarr'/'catalog_analysis.json').exists())

if __name__=='__main__':unittest.main()
