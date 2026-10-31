"""Resolution, retention, and non-destructive profile-grid migration tests."""
from pathlib import Path
from tempfile import TemporaryDirectory
import datetime as dt
import json
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import sounding_profiles as sp
import migrate_profile_grid as mig

R=Path(__file__).resolve().parents[1]

class ProfileResolutionTests(unittest.TestCase):
 def test_profile_defaults(self):
  a=sp.parse_args(['--date','20261028','--cycle','12'])
  self.assertEqual(a.grid_stride,4)
  self.assertEqual(a.retention_days,7)
  rap=sp.parse_args(['--date','20261028','--cycle','13','--models','rap_analysis'])
  self.assertEqual(rap.grid_stride,1)
  self.assertEqual(sp.coarse_dims(4),(265,450))
  self.assertEqual(sp.coarse_dims(1,451,337),(337,451))
 def test_cron_defaults(self):
  model=(R/'run_model_incremental_cron.sh').read_text()
  multi=(R/'run_multimodel_cron.sh').read_text()
  self.assertIn('RETENTION_DAYS="${RETENTION_DAYS:-7}"',model)
  self.assertIn('RETENTION_DAYS="${RETENTION_DAYS:-7}"',multi)
  self.assertIn('SOUNDING_PROFILE_GRID_STRIDE:-4',model)
  self.assertIn('SOUNDING_PROFILE_RAP_STRIDE:-1',model)
 def test_safe_migration_and_case_backup(self):
  with TemporaryDirectory() as tmp:
   root=Path(tmp)/'zarr';work=Path(tmp)/'work';root.mkdir();work.mkdir()
   old,_,_=sp.create_store(root,'rrfs','20261028','12',16,2)
   rap,_,_=sp.create_store(root,'rap_analysis','20261029','13',4,2,source_grid=dict(sp.mm.core.GRID_ATTRS,GRIB_Nx=451,GRIB_Ny=337))
   already,_,_=sp.create_store(root,'refs_m001','20261028','12',4,2)
   plans,backup=mig.migrate(root,work,lock_directory=Path(tmp))
   self.assertEqual(len(plans),2)
   self.assertIsNone(backup)
   self.assertTrue(old.exists())
   plans,backup=mig.migrate(root,work,apply=True,lock_directory=Path(tmp),now=dt.datetime(2026,10,30,tzinfo=dt.timezone.utc))
   self.assertEqual(len(plans),2)
   self.assertFalse(old.exists())
   self.assertFalse(rap.exists())
   self.assertTrue(already.exists())
   self.assertTrue((backup/old.name/'.zattrs').exists())
   self.assertTrue((backup/rap.name/'.zattrs').exists())
   self.assertEqual(json.loads((backup/'MIGRATION_INFO.json').read_text())['rap_grid_stride'],1)
   new,_,_=sp.create_store(root,'rrfs','20261028','12',4,2)
   self.assertEqual(json.loads((new/'.zattrs').read_text())['grid_spacing_km'],12)
   none,blank=mig.migrate(root,work,apply=False,lock_directory=Path(tmp))
   self.assertEqual(none,[])
   self.assertIsNone(blank)
 def test_grid_mismatch_still_refuses(self):
  with TemporaryDirectory() as tmp:
   root=Path(tmp);sp.create_store(root,'rrfs','20261028','12',16,2)
   with self.assertRaisesRegex(RuntimeError,'migrate_profile_grid.py'):
    sp.create_store(root,'rrfs','20261028','12',4,2)

if __name__=='__main__':unittest.main()
