"""Operational profile coverage including REFS missing native levels and retention."""
from pathlib import Path
from tempfile import TemporaryDirectory
import datetime as dt
import json
import os
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import sounding_profiles as sp

class OperationalSoundings(unittest.TestCase):
 def test_refs_native_levels_missing_remain_missing(self):
  inventory=[{'msg':1,'short_name':'TMP','text':'1:0:d=2026102812:TMP:1000 mb:anl'},
             {'msg':2,'short_name':'TMP','text':'2:0:d=2026102812:TMP:850 mb:anl'},
             {'msg':3,'short_name':'SPFH','text':'3:0:d=2026102812:SPFH:850 mb:anl'}]
  self.assertIsNone(sp.native_record(inventory,'TMP',975))
  self.assertIsNone(sp.native_record(inventory,'SPFH',1000))
  self.assertEqual(sp.native_record(inventory,'TMP',850)['msg'],2)
  self.assertTrue(sp.native_record(inventory,'SPFH',850))
 def test_model_selection_and_non_synoptic_rap(self):
  args=sp.parse_args(['--date','20261028','--cycle','12','--models','rrfs,refs_m001,refs_m005,hrrr'])
  self.assertEqual(args.models,('rrfs','refs_m001','refs_m005','hrrr'))
  args=sp.parse_args(['--date','20261028','--cycle','13','--models','rap_analysis'])
  self.assertEqual(args.cycle,'13')
  with self.assertRaises(SystemExit):
   sp.parse_args(['--date','20261028','--cycle','13','--models','rrfs'])
  with self.assertRaises(SystemExit):
   sp.parse_args(['--date','20261028','--cycle','12','--models','rrfs,rap_analysis'])
 def test_case_protection_prevents_pruning_model_and_rap(self):
  with TemporaryDirectory() as temp:
   root=Path(temp)/'zarr';work=Path(temp)/'work';root.mkdir();work.mkdir()
   pinned=root/'sounding_refs_m004_20261028_1200.zarr';pinned.mkdir()
   unpinned=root/'sounding_refs_m005_20261001_0000.zarr';unpinned.mkdir()
   rap_pinned=root/'sounding_rap_analysis_20261029_1200.zarr';rap_pinned.mkdir()
   rap_unpinned=root/'sounding_rap_analysis_20261001_1300.zarr';rap_unpinned.mkdir()
   config=Path(temp)/'protected_cycles.json';config.write_text(json.dumps({'schema':'omd-evu-protected-cycles-1','protected_cycles':['20261028_12'],'protected_analysis':[]}))
   removed=sp.prune_profiles(root,work,config,14,now=dt.datetime(2026,11,30,tzinfo=dt.timezone.utc))
   self.assertEqual(set(removed),{unpinned.name,rap_unpinned.name})
   self.assertTrue(pinned.exists());self.assertTrue(rap_pinned.exists())
 def test_remote_catalog_only_published_successful_stores(self):
  with TemporaryDirectory() as temp:
   root=Path(temp)/'zarr';root.mkdir();work=Path(temp)/'work';work.mkdir();(work/'.published').mkdir()
   for model in ('refs_m001','refs_m002'):
    store,_,_=sp.create_store(root,model,'20261028','12',16,2)
    (store/'.hours').mkdir();(store/'.hours'/'000.json').write_text('{}')
   (work/'.published'/'sounding_refs_m001_20261028_1200.zarr.json').write_text('{}')
   got=json.loads(sp.refresh_catalog(root,published_only=True,published_work=work,destination=Path(temp)/'remote_cat.json').read_text())
   self.assertEqual([x['model'] for x in got['datasets']],['refs_m001'])
 def test_cron_enabled_and_ensemble_rotation_present(self):
  source=(Path(__file__).resolve().parents[1]/'run_model_incremental_cron.sh').read_text()
  self.assertIn('SOUNDING_PROFILES_ENABLED:-1',source)
  self.assertIn('SOUNDING_PROFILE_REFS_ENABLED:-1',source)
  self.assertIn('refs_m%03d',source)
  self.assertIn('--cleanup-only',source)

if __name__=='__main__':unittest.main()
