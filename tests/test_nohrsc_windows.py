"""Native NOHRSC download cadence and derived-window configuration tests."""
import datetime as dt
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import verification_ingest as ingest

UTC=dt.timezone.utc
class NohrscWindowTests(unittest.TestCase):
    def test_only_6_and_24_hour_downloads(self):
        for hour in range(24):
            timestamp=dt.datetime(2026,10,29,hour,tzinfo=UTC)
            jobs=ingest.nohrsc_jobs(timestamp)
            products=[name for name,_,_ in jobs]
            if hour in (0,12):self.assertEqual(products,['nohrsc_6h','nohrsc_24h'])
            elif hour in (6,18):self.assertEqual(products,['nohrsc_6h'])
            else:self.assertEqual(products,[])
            self.assertFalse(any('48h' in name or '72h' in name for name in products))
    def test_grib_urls_and_48h_derived_from_24h(self):
        timestamp=dt.datetime(2026,10,29,12,tzinfo=UTC)
        jobs=ingest.nohrsc_jobs(timestamp)
        self.assertTrue(jobs[0][1].endswith('/202610/sfav2_CONUS_6h_2026102912_grid184.grb2'))
        self.assertTrue(jobs[1][1].endswith('/202610/sfav2_CONUS_24h_2026102912_grid184.grb2'))
        config=json.loads((Path(__file__).resolve().parents[1]/'verification_sources.json').read_text())
        self.assertEqual(config['field_sources']['snow48h'],'nohrsc_24h')
        self.assertEqual(config['derived_in_viewer']['snow48h']['method'],'sum_two_nonoverlapping_24_hour_analyses')
        self.assertNotIn('snow72h',config['field_sources'])
        for n in [24,48]:
            self.assertEqual(config['field_sources'][f'apcp{n}h'],'mrms_apcp')
if __name__=='__main__':unittest.main()
