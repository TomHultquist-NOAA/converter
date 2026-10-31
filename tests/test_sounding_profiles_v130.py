"""Unit checks for experimental native-level, reduced-grid soundings."""
import sys
import json
import math
from array import array
from pathlib import Path
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import sounding_profiles as sp

class SoundingTests(unittest.TestCase):
 def test_levels_and_units(self):
  self.assertEqual(len(sp.PRESSURES),37)
  self.assertEqual((sp.PRESSURES[0],sp.PRESSURES[-1]),(1000,100))
  self.assertTrue(all(a-b==25 for a,b in zip(sp.PRESSURES,sp.PRESSURES[1:])))
  self.assertEqual(sp.UNITS['q'],'kg kg-1')
 def test_dewpoint_and_invalid_q(self):
  self.assertAlmostEqual(sp.dewpoint_kelvin(.0076,1000)-273.15,10.2,delta=1.0)
  self.assertTrue(math.isnan(sp.dewpoint_kelvin(float('nan'),850)))
  self.assertTrue(math.isnan(sp.dewpoint_kelvin(-1,850)))
  self.assertTrue(math.isnan(sp.dewpoint_kelvin(.5,850)))
  self.assertLess(sp.dewpoint_kelvin(.003,700),sp.dewpoint_kelvin(.003,1000))
 def test_exact_pressure_record(self):
  items=[{'msg':1,'short_name':'TMP','text':'1:12:d=2026102812:TMP:1000 mb:0 hour fcst'},
         {'msg':2,'short_name':'TMP','text':'2:15:d=2026102812:TMP:100 mb:0 hour fcst'},
         {'msg':3,'short_name':'SPFH','text':'3:17:d=2026102812:SPFH:1000 mb:0 hour fcst'}]
  self.assertEqual(sp.native_record(items,'TMP',100)['msg'],2)
  self.assertEqual(sp.native_record(items,'TMP',1000)['msg'],1)
  self.assertEqual(sp.native_record(items,'SPFH',1000)['msg'],3)
  self.assertIsNone(sp.native_record(items,'TMP',975))
 def test_sample_and_mask(self):
  data=array('f',range(16)).tobytes()
  out=sp.downsample(data,2,'u',4,4)
  self.assertEqual(list(out),[0,2,8,10])
  vals=sp.downsample(array('f',[9999,200,220,230]*4).tobytes(),2,'t',4,4)
  self.assertTrue(math.isnan(vals[0]))
  self.assertEqual(vals[1],220)
 def test_store_metadata_and_separate_catalog(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)
   store,nx,ny=sp.create_store(root,'rrfs','20261028','12',16,5)
   self.assertEqual((nx,ny),(113,67))
   attrs=json.loads((store/'.zattrs').read_text())
   self.assertEqual(attrs['GRIB_DxInMetres'],48000.)
   self.assertEqual(attrs['pressure_levels_hpa'],list(sp.PRESSURES))
   self.assertEqual(json.loads((store/'td'/'.zarray').read_text())['chunks'],[1,37,67,113])
   self.assertFalse((root/'rrfs_20261028_1200.zarr').exists())
   (store/'.hours').mkdir()
   (store/'.hours'/'000.json').write_text('{}')
   out=sp.refresh_catalog(root)
   cat=json.loads(out.read_text())
   self.assertEqual(cat['datasets'][0]['available_hours'],[0])
   self.assertEqual(cat['datasets'][0]['path'],store.name)

if __name__=='__main__':unittest.main()
