"""REFS Mean: direct native T/Td/U/V, never five-member synthesis."""
import json,math,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import sounding_profiles as sp

class NativeMeanTests(unittest.TestCase):
 def test_operational_mean_field_set(self):
  self.assertEqual(sp.MEAN_NATIVE,{'t':'TMP','td':'DPT','u':'UGRD','v':'VGRD'})
  self.assertNotIn('q',sp.MEAN_NATIVE)
  self.assertEqual(sp.VALID_MODELS.count('refs_mean'),1)
  self.assertTrue('refs_mean' in sp.NAME_RE.pattern)
 def test_mean_inventory_sparse_levels(self):
  levels={'TMP':[925,850,700,500,250], 'DPT':[925,850,700,500],
          'UGRD':[925,850,700,550,500,250], 'VGRD':[925,850,700,550,500,250]}
  rows=[]
  i=0
  for name,pressures in levels.items():
   for p in pressures:
    i+=1
    rows.append({'msg':i,'short_name':name,'text':'%d:0:d=2026102812:%s:%d mb:22 hour fcst' % (i,name,p)})
  complete=[p for p in sp.PRESSURES if all(sp.native_record(rows,k,p) is not None for k in ('TMP','DPT','UGRD','VGRD'))]
  self.assertEqual(complete,[925,850,700,500])
  self.assertIsNone(sp.native_record(rows,'DPT',250))
  self.assertIsNone(sp.native_record(rows,'SPFH',925))
  self.assertIsNotNone(sp.native_record(rows,'UGRD',550))
 def test_mean_store_metadata_direct_dewpoint(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   store,_,_=sp.create_store(root,'refs_mean','20261028','12',16,2)
   attrs=json.loads((store/'.zattrs').read_text())
   self.assertEqual(attrs['ensemble_method'],'native operational time-lagged REFS Mean GRIB2')
   self.assertIn('native mean DPT',attrs['dewpoint_source'])
   self.assertIn('not published',attrs['specific_humidity_source'])
   self.assertEqual(json.loads((store/'td'/'.zattrs').read_text())['provenance'],'native REFS Mean DPT')
   (store/'.hours').mkdir();(store/'.hours'/'022.json').write_text('{}')
   data=json.loads(sp.refresh_catalog(root).read_text())['datasets'][0]
   self.assertEqual(data['ensemble_method'],attrs['ensemble_method'])
   self.assertEqual([v['name'] for v in data['variables']],['t','td','u','v'])
 def test_mean_hours_use_processed_model_store_not_five_members(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   store=root/'refs_mean_20261028_1200.zarr';(store/'processed').mkdir(parents=True)
   (store/'processed'/'0').write_bytes(b'processed')
   def fake_read(_store,n):
    return [i in (1,12,25) for i in range(n)]
   with patch.object(sp.mm,'read_processed',side_effect=fake_read):
    self.assertEqual(sp.model_hour_list(root,'refs_mean','20261028','12',''),[1,12,25])
 def test_mean_urls_include_ensprod_product(self):
  u,idx=sp.mm.file_urls('refs_mean','20261028','12',22,'mean')
  self.assertIn('/refs.20261028/12/ensprod/',u)
  self.assertTrue(u.endswith('/refs.t12z.mean.f22.conus.grib2'))
  self.assertEqual(idx,u+'.idx')
if __name__=='__main__':unittest.main()
