"""Read-only publication selection and catalog consistency tests."""
import unittest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from repair_case_publication import case_targets,merged_catalog

class PublicationPlanTests(unittest.TestCase):
    def test_targets_are_case_and_product_limited(self):
        def ds(product,time):return {'product':product,'path':f'analysis/analysis_{product}_{time}.zarr',
            'init_time':f'{time[:4]}-{time[4:6]}-{time[6:8]}T{time[8:10]}:00:00Z','variables':[{'name':'snow6h'}]}
        catalog={'datasets':[ds('nohrsc_6h','2026102600'),ds('nohrsc_24h','2026102700'),
             ds('nohrsc_6h','2026103000'),ds('mrms_apcp','2026102700')]}
        result=case_targets(catalog,'2026102600',84)
        self.assertEqual(len(result),2)
    def test_merge_keeps_only_actual_remote_stores_and_preserves_remote_only(self):
        first={'product':'nohrsc_6h','path':'analysis/analysis_nohrsc_6h_2026102700.zarr',
               'variables':[{'name':'snow6h'}], 'init_time':'2026-10-27T00:00:00Z'}
        second={'product':'mrms_refc','path':'analysis/analysis_mrms_refc_2026102700.zarr',
                'variables':[{'name':'refc'}], 'init_time':'2026-10-27T00:00:00Z'}
        third={'product':'rtma','path':'analysis/analysis_rtma_2026102700.zarr',
                'variables':[{'name':'t2m'}], 'init_time':'2026-10-27T00:00:00Z'}
        out=merged_catalog({'datasets':[first,second]},{'datasets':[third]},
                           {'analysis_nohrsc_6h_2026102700.zarr','analysis_rtma_2026102700.zarr'})
        self.assertEqual({x['path'] for x in out['datasets']},{first['path'],third['path']})
        self.assertEqual(out['schema'],'rrfs-zarr-viewer-1')

if __name__=='__main__':unittest.main()
