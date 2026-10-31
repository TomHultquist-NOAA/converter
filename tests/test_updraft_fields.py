#!/usr/bin/env python3
"""Run: python3 -m unittest discover -s tests -v (no external deps)."""
import importlib.util
import tempfile
import json
import zlib
from array import array
from pathlib import Path
import unittest

SCRIPT=Path(__file__).resolve().parents[1]/'multi_model_to_zarr_wcoss2.py'
spec=importlib.util.spec_from_file_location('multi_model_to_zarr_wcoss2',SCRIPT)
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)

class FieldTests(unittest.TestCase):
 def r(self, name, level, interval, msg=1):
  return {'short_name':name,'text':f'{name}:{level}:{interval}:', 'msg':msg}
 def test_uh_one_hour_only(self):
  good=self.r('MXUPHL','5000-2000 m above ground','2-3 hour max')
  wrong=self.r('MXUPHL','5000-2000 m above ground','0-3 hour max',2)
  self.assertIs(c.choose_field([wrong,good],'mxuphl',3),good)
  self.assertIsNone(c.choose_field([wrong],'mxuphl',3))
  self.assertIsNone(c.choose_field([self.r('MXUPHL','5000-2000 m above ground','0-0 hour max')],'mxuphl',0))
  self.assertNotIn('uphl',c.FIELDS)
 def test_native_composites(self):
  for field,short in [('scp','SCCP'),('stp','STPC'),('stp_fixed','SIGT')]:
   self.assertEqual(c.choose_field([self.r(short,'effective layer','3 hour fcst')],field,3)['short_name'],short)
   self.assertIsNone(c.choose_field([self.r('SCP','surface','3 hour fcst')],field,3))
 def test_run_max_requires_every_intervening_hour_and_provenance(self):
  old_npts,old_bytes=c.core.NPTS,c.core.FIELD_BYTES
  c.core.NPTS,c.core.FIELD_BYTES=3,12
  try:
   with tempfile.TemporaryDirectory() as tmp:
    store=Path(tmp)/'hrrr_20261029_1200.zarr'
    (store/'mxuphl').mkdir(parents=True)
    manifest={'forecast_hours':{}}
    data={1:[2.,7.,-1e10],2:[4.,5.,3.],3:[1.,11.,9.]}
    for h,values in data.items():
     (store/'mxuphl'/f'{h}.0.0').write_bytes(zlib.compress(array('f',values).tobytes()))
     manifest['forecast_hours'][f'{h:03d}']={'selections':{'mxuphl':{'inventory':f'MXUPHL:5000-2000 m above ground:{h-1}-{h} hour max'}}}
    self.assertTrue(c.derive_run_max_uphl(store,manifest,1))
    f2=array('f');f2.frombytes(zlib.decompress((store/'mxuphl_run'/'2.0.0').read_bytes()))
    self.assertEqual(f2[0],4);self.assertEqual(f2[1],7)
    self.assertTrue(c.math.isnan(f2[2]))
    self.assertFalse((store/'mxuphl_run'/'0.0.0').exists())
    self.assertFalse(c.derive_run_max_uphl(store,manifest,1)) # unchanged
    (store/'mxuphl'/'2.0.0').unlink()
    self.assertTrue(c.derive_run_max_uphl(store,manifest,1))
    self.assertFalse((store/'mxuphl_run'/'2.0.0').exists())
    self.assertFalse((store/'mxuphl_run'/'3.0.0').exists())
    # Old arbitrary-interval record must also block continuity.
    (store/'mxuphl'/'2.0.0').write_bytes(zlib.compress(array('f',data[2]).tobytes()))
    manifest['forecast_hours']['002']['selections']['mxuphl']['inventory']='MXUPHL:0-2 hour max'
    c.derive_run_max_uphl(store,manifest,1)
    self.assertFalse((store/'mxuphl_run'/'3.0.0').exists())
  finally:
   c.core.NPTS,c.core.FIELD_BYTES=old_npts,old_bytes

if __name__=='__main__': unittest.main()
