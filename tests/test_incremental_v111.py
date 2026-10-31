"""Regression tests for incremental missing-field repairs with unnamed repair_kind."""
import importlib.util
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('converter_v111', ROOT/'multi_model_to_zarr_wcoss2.py')
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class RepairSummaryTests(unittest.TestCase):
    def test_incremental_unnamed_repair_does_not_crash(self):
        previous={
            'status':'complete',
            'fields':['t2m','h925'],
            'selections':{'t2m':{'product':'2dfld'}},
            'products':{'2dfld':{'selected':1}},
            'pending':{'asnow':'unknown'},
            'last_missing_check_utc':'2026-10-29T07:00:00+00:00',
        }
        result={
            'status':'complete', 'repair_only':True, 'repair_kind':None,
            'fields':['h925','h500'],
            'selections':{'h500':{'product':'prslev'}},
            'products':{'prslev':{'selected':1}},
            'pending':{},
        }
        merged=mod.merge_repair_result(previous,result)
        self.assertEqual(merged['fields'],['h500','h925','t2m'])
        self.assertEqual(merged['repair_fields_added'],['h925','h500'])
        self.assertIn('asnow',merged['pending'])
        self.assertIn('t2m',merged['selections'])
        self.assertIn('h500',merged['selections'])
        self.assertIn('2dfld',merged['products'])
        self.assertIn('prslev',merged['products'])
        self.assertEqual(merged['last_missing_check_utc'],'2026-10-29T07:00:00+00:00')
        self.assertNotIn('repair_None_added',merged)
        self.assertNotIn('repair_none_added',merged)
        self.assertEqual(previous['fields'],['t2m','h925'])

    def test_snowfall_repair_replaces_pending_asnow_only(self):
        previous={'fields':['t2m'],'pending':{'asnow':'unknown','frzr':'unknown'}}
        result={'fields':['asnow'],'repair_kind':'snowfall','pending':{}}
        merged=mod.merge_repair_result(previous,result)
        self.assertEqual(merged['pending'],{'frzr':'unknown'})
        self.assertEqual(merged['repair_snowfall_added'],['asnow'])
        self.assertEqual(merged['fields'],['asnow','t2m'])

    def test_empty_explicit_repair_kind_falls_back(self):
        merged=mod.merge_repair_result({}, {'fields':['h500'],'repair_kind':''})
        self.assertEqual(merged['repair_fields_added'],['h500'])
        self.assertNotIn('pending',merged)

if __name__ == '__main__':
    unittest.main()
