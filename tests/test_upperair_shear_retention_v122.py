"""Regression: 300-hPa data and source-specific shear, protected 14-day cleanup."""
import datetime as dt
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from array import array
import zlib
import multi_model_to_zarr_wcoss2 as m

UTC=dt.timezone.utc


def raw(*values):return array('f',values).tobytes()


def nums(blob):a=array('f');a.frombytes(blob);return list(a)

class TestV122(unittest.TestCase):
    def test_300_hpa_model_fields(self):
        for f in ('h300','u300','v300','wspd300'):
            self.assertIn(f,m.FIELDS)
        self.assertEqual(m.FIELDS['wspd300']['units'],'m s-1')
        self.assertEqual(m.choose_field([{'short_name':'UGRD','text':'0:UGRD:300 mb:6 hour fcst','msg':1}], 'u300',6)['msg'],1)

    def test_shear_rrfs_wind_difference_is_not_multiplied(self):
        u=raw(12,20,30,15)*1000;v=raw(5,0,4,8)*1000
        units,med=m.infer_bulk_shear_units(u,v,'rrfs')
        self.assertEqual(units,'m/s')
        self.assertAlmostEqual(nums(m.derive_bulk_shear_bytes(u,v,6000,'rrfs'))[0],13,places=3)
        self.assertAlmostEqual(nums(m.derive_bulk_shear_bytes(u,v,1000,'refs_m001'))[0],13,places=3)

    def test_shear_hrrr_gradient_needs_layer_depth(self):
        u=raw(.002,.003,.004,.005)*1000;v=raw(.003,.002,.001,.004)*1000
        self.assertEqual(m.infer_bulk_shear_units(u,v,'hrrr')[0],'s-1')
        self.assertAlmostEqual(nums(m.derive_bulk_shear_bytes(u,v,6000,'hrrr'))[0],(13**.5)*6,places=2)

    def test_temporary_cleanup_and_protected_observations(self):
        with TemporaryDirectory() as td:
            root=Path(td);out=root/'zarr';work=root/'work';logs=root/'logs'
            for path in (out,work,logs,out/'ptype_observations',work/'.published'):path.mkdir(parents=True,exist_ok=True)
            older=out/'ptype_observations'/'2026090100.json';protected=out/'ptype_observations'/'2026090200.json'
            older.write_text('{}');protected.write_text('{}')
            for n in ('multisource_20260901_000000.log','model_incremental_20260901_010000.log'):
                f=logs/n;f.write_text('old log')
                import os;os.utime(f,(dt.datetime(2026,9,1,tzinfo=UTC).timestamp(),)*2)
            store_state=work/'.published'/'rrfs_20260901_0000.zarr.json';store_state.write_text('{}')
            clock=dt.datetime(2026,10,30,tzinfo=UTC)
            stats=m.cleanup_local(out,work,14,now=clock,logs_dir=logs,
                                  analysis_rules=(('2026090200','2026090200',('rap_surface',)),))
            self.assertEqual(stats['observations'],1)
            self.assertEqual(stats['logs'],2)
            self.assertEqual(stats['publish_states'],1)
            self.assertFalse(older.exists());self.assertTrue(protected.exists())
            self.assertFalse(store_state.exists())

    def test_remote_retention_script_compiles(self):
        compile(m.REMOTE_RETENTION_SCRIPT,'remote_retention','exec')

if __name__=='__main__':unittest.main()
