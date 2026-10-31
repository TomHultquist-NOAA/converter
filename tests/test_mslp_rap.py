"""Regression tests for MSLP source selection and RAP F000 analysis fields."""
import json
import math
import unittest
from pathlib import Path
from array import array
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import multi_model_to_zarr_wcoss2 as model
import verification_ingest as ingest
import verification_convert as convert

ROOT=Path(__file__).resolve().parents[1]

class MslpRapTests(unittest.TestCase):
    def test_mslp_appropriate_nwp_reduction_and_not_surface_pressure(self):
        def record(short, level):
            return {'short_name':short,'text':f'1:0:d=2026102912:{short}:{level}:anl:', 'msg':1}
        inventories={'2dfld':[record('PRES','surface'), record('PRMSL','mean sea level')],
                     'pmmn':[record('MSLET','mean sea level')],
                     'wrfsfc':[record('MSLMA','mean sea level')]}
        self.assertEqual(model.select_mslp_record(inventories,'rrfs')[1]['short_name'],'PRMSL')
        self.assertEqual(model.select_mslp_record(inventories,'refs_mean')[1]['short_name'],'MSLET')
        self.assertEqual(model.select_mslp_record(inventories,'hrrr')[1]['short_name'],'MSLMA')
        self.assertIsNone(model.select_mslp_record({'2dfld':[record('PRES','surface')]},'rrfs'))

    def test_rap_native_mslma_variant_and_pressure_level(self):
        lines=['1:0:d=2026102912:PRES:surface:anl:',
               '2:10:d=2026102912:MSLMA M:mean sea level:anl:',
               '3:40:d=2026102912:TMP:2 m above ground:anl:']
        found,missing=ingest.select_inventory_records(lines,{'mslp':['MSLMA','mean sea level']})
        self.assertEqual(missing,[])
        self.assertIn('MSLMA M',found['mslp'])
        wrong,_=ingest.select_inventory_records(lines,{'t925':['TMP','925 mb']})
        self.assertFalse(wrong)

    def test_rap_layer_selection_does_not_mislabel_255_mb_as_mucape(self):
        cfg=json.loads((ROOT/'verification_sources.json').read_text())
        self.assertEqual(cfg['fields']['rap_pressure']['mlcape'],['CAPE','90-0 mb above ground'])
        self.assertEqual(cfg['fields']['rap_pressure']['mucape'],['CAPE','255-0 mb above ground'])
        self.assertEqual(cfg['fields']['rap_pressure']['shru06'],['VUCSH','0-6000 m above ground'])
        self.assertEqual(cfg['fields']['rap_pressure']['absv500'],['ABSV','500 mb'])
        self.assertEqual(cfg['fields']['rap_surface']['mslp'],['MSLMA','mean sea level'])
        self.assertNotIn('shear01',cfg['fields']['rap_pressure'])
        self.assertNotIn('mxuphl',cfg['fields']['rap_pressure'])

    def test_rap_bulk_shear_uses_vector_components_and_depth(self):
        result=convert.derive_bulk_shear(array('f',[0.003,math.nan]),array('f',[0.004,0.0]),6000)
        self.assertAlmostEqual(result[0],30.0,places=4)
        self.assertTrue(math.isnan(result[1]))
        self.assertEqual(convert.FIELD_UNITS['mslp'],'Pa')
        self.assertEqual(convert.FIELD_UNITS['shear06'],'m s-1')

if __name__ == '__main__':unittest.main()
