"""Regression checks for read-only RAP/REFS Mean diagnostic audit."""
import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path

import audit_rap_case as audit


def put_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def store_field(store, field, hour=0):
    folder = store / field
    folder.mkdir(parents=True, exist_ok=True)
    put_json(folder / '.zarray', {'zarr_format': 2})
    (folder / f'{hour}.0.0').write_bytes(b'compressed sample')


class TestAudit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'zarr'
        self.root.mkdir()
        self.work = Path(self.tmp.name) / 'work'
        self.hour = audit.parse_utc('2026102812')

    def test_model_display_name_not_internal_id(self):
        store = self.root / 'refs_mean_20261028_1200.zarr'
        store_field(store, 'h500', 1)
        store_field(store, 'u500', 1)
        put_json(self.root / 'catalog_refs_mean.json', {
            'schema': 'rrfs-zarr-viewer-1',
            'generated_utc': '2026-10-30T12:00:00+00:00',
            'datasets': [{'model': 'REFS Mean',
                          'path': store.name,
                          'init_time': '2026-10-28T12:00:00Z',
                          'variables': [{'name': 'h500', 'available_hours': [1]},
                                        {'name': 'u500', 'available_hours': [1]}]}]
        })
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            found = audit.assess_mean(self.root, self.hour, 1)
        self.assertTrue(found)
        self.assertIn('catalog=True', output.getvalue())
        self.assertIn('REFS Mean', output.getvalue())
        self.assertIn('h500', output.getvalue())

    def test_rap_manifest_and_catalog_field_mismatch(self):
        store = self.root / 'analysis' / 'analysis_rap_pressure_2026102812.zarr'
        store_field(store, 't850')
        store_field(store, 'h500')
        put_json(store / '.conversion.json', {'fields': ['h500', 't850']})
        put_json(self.work / 'verification_staging' / 'rap_pressure' / '20261028' / 'rap_pressure_2026102812.json', {
            'status': 'partial', 'selected': [{'field': 't850'}, {'field': 'h500'}],
            'missing': ['h300', 'u300']
        })
        entry = {'product': 'rap_pressure', 'init_time': '2026-10-28T12:00:00Z',
                 'variables': [{'name': 't850', 'available_hours': [0]}]}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            flags = audit.assess_rap(self.root, self.work, [entry], [], 'rap_pressure', self.hour)
        self.assertEqual(flags, (True, True, False, False))
        self.assertIn('WARNING native chunks absent from catalog: h500', output.getvalue())
        self.assertIn('staging manifest: partial', output.getvalue())
        self.assertIn('missing=h300,u300', output.getvalue())
        self.assertIn('h500', output.getvalue())

    def test_staged_mucape_missing_from_conversion_is_explicitly_flagged(self):
        store = self.root / 'analysis' / 'analysis_rap_pressure_2026102812.zarr'
        store_field(store, 't700')
        store_field(store, 'mucape')  # orphan chunk not listed in committed metadata
        put_json(store / '.conversion.json', {'fields': ['t700']})
        put_json(self.work / 'verification_staging' / 'rap_pressure' / '20261028' / 'rap_pressure_2026102812.json', {
            'status': 'complete', 'selected': [{'field': 't700'}, {'field': 'mucape'}],
            'missing': []
        })
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            audit.assess_rap(self.root, self.work, [], [], 'rap_pressure', self.hour)
        self.assertIn('STALE CONVERSION: staged target fields missing from conversion metadata: mucape',out.getvalue())

    def test_catalog_timestamp_for_model_and_analysis(self):
        path = self.root / 'catalog_aligned_analysis.json'
        put_json(path, {'schema': 'rrfs-zarr-viewer-1', 'generated_utc': '2026-10-30T12:00:00Z', 'datasets': []})
        self.assertEqual(audit.load_catalog(path)[1], '2026-10-30T12:00:00Z')


if __name__ == '__main__':
    unittest.main()
