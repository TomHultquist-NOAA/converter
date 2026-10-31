"""Regression: optional repairs are not logged as missing forecast hours."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('converter_v112', ROOT/'multi_model_to_zarr_wcoss2.py')
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class FakeClient:
    def __init__(self, **kw): pass
    def text(self, url):
        # A published model hour with a valid temperature inventory, but
        # without the severe-weather parameter requested by an incremental repair.
        return '1:0:d=2026102918:TMP:2 m above ground:0 hour fcst:\n'
    def size(self, url): return 2048

class FakeNoSupportedFields(FakeClient):
    def text(self, url):
        return '1:0:d=2026102918:FAKE:unknown level:0 hour fcst:\n'

class NoMatchTests(unittest.TestCase):
    def test_previously_processed_hour_with_no_optional_fields_is_not_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            work=Path(td)/'work'
            store=Path(td)/'rrfs_20261029_1800.zarr'
            store.mkdir()
            args=('rrfs','20261029','18',0,str(store),str(work),'wgrib2',3,30,1,False,False,False,('stp','scp'),None)
            with patch.object(mod.core, 'HTTPClient', FakeClient):
                result=mod.process_job(args)
            self.assertEqual(result['status'],'repair_no_match')
            self.assertEqual(result['requested_fields'],['stp','scp'])
            self.assertEqual(result['missing_products'],{})
            self.assertFalse(result['fields'])

    def test_new_hour_with_no_matching_fields_remains_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            work=Path(td)/'work'
            store=Path(td)/'rrfs_20261029_1800.zarr'
            store.mkdir()
            args=('rrfs','20261029','18',0,str(store),str(work),'wgrib2',3,30,1,False,False,False,('stp',),None)
            # A fresh ingest without any matching records must still be
            # treated as unavailable, rather than as a completed-hour repair.
            args=args[:13]+(None,None)
            with patch.object(mod.core, 'HTTPClient', FakeNoSupportedFields):
                result=mod.process_job(args)
            self.assertEqual(result['status'],'unavailable')
            self.assertEqual(result['missing_products'],{})

    def test_repair_no_match_never_erases_an_existing_chunk(self):
        with tempfile.TemporaryDirectory() as td:
            work=Path(td)/'work'
            store=Path(td)/'rrfs_20261029_1800.zarr'
            chunk=store/'t2m'/'0.0.0'
            chunk.parent.mkdir(parents=True)
            chunk.write_bytes(b'existing-good-data')
            args=('rrfs','20261029','18',0,str(store),str(work),'wgrib2',3,30,1,False,False,False,('stp',),None)
            with patch.object(mod.core, 'HTTPClient', FakeClient):
                result=mod.process_job(args)
            self.assertEqual(result['status'],'repair_no_match')
            self.assertEqual(chunk.read_bytes(),b'existing-good-data')

if __name__=='__main__': unittest.main()
