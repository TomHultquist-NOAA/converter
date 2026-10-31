"""Incremental ingestion, conservative missing fields and atomic delta publishing."""
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock
from array import array

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('converter_v110',ROOT/'multi_model_to_zarr_wcoss2.py')
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class IngestTests(unittest.TestCase):
    def test_incremental_frontier(self):
        flags=bytearray(85)
        self.assertEqual(list(mod.incremental_hours('rrfs',flags,84,5)),list(range(0,5)))
        flags[0]=flags[1]=flags[2]=1
        self.assertEqual(max(mod.incremental_hours('rrfs',flags,84,5)),7)
        self.assertEqual(list(mod.incremental_hours('refs_mean',bytearray(61),60,3)),[1,2,3])

    def test_missing_field_is_based_on_chunks(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/'h500').mkdir()
            (root/'h500'/'2.0.0').write_bytes(b'yes')
            (root/'.accum_intervals'/'asnow').mkdir(parents=True)
            (root/'.accum_intervals'/'asnow'/'2.zlib').write_bytes(b'staged')
            missing=mod.missing_fields_for_hour(root,'refs_mean',2)
            self.assertNotIn('h500',missing)
            self.assertNotIn('asnow',missing)
            self.assertNotIn('absv500',missing) # derived from mean winds
            self.assertIn('u500',missing)
            self.assertNotIn('apcp',mod.missing_fields_for_hour(root,'rrfs',0))

    def test_mean_pressure_fields_in_standard_product(self):
        inventory=[dict(msg=i,short_name=short,text=f'{i}:{short}:500 mb:22 hour fcst:')
                   for i,short in enumerate(('HGT','UGRD','VGRD','TMP'),1)]
        self.assertIsNotNone(mod.choose_field(inventory,'h500',22))
        self.assertIsNotNone(mod.choose_field(inventory,'u500',22))
        self.assertIsNotNone(mod.choose_field(inventory,'v500',22))
        self.assertIsNone(mod.choose_field(inventory,'absv500',22))

    def test_vorticity_f_plane_zeros_and_no_data(self):
        nx,ny=5,4
        zero=array('f',[0.0]*(nx*ny))
        got=mod.derive_absolute_vorticity_from_winds(zero.tobytes(),zero.tobytes(),nx,ny)
        vals=array('f');vals.frombytes(got)
        self.assertTrue(all(math.isfinite(x) and 3e-5<x<1e-4 for x in vals))
        self.assertAlmostEqual(vals[2],vals[7],delta=3e-7)
        v=array('f',zero);v[2]=math.nan
        vals=array('f');vals.frombytes(mod.derive_absolute_vorticity_from_winds(zero.tobytes(),v.tobytes(),nx,ny))
        self.assertTrue(math.isnan(vals[2]))

    def test_derive_missing_refs_mean_500_when_native_is_absent(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'refs_mean_20261029_0000.zarr';root.mkdir()
            nx0,ny0=mod.core.NX,mod.core.NY
            with mock.patch.object(mod.core,'NX',5),mock.patch.object(mod.core,'NY',4):
                for name in ('u500','v500','absv500'):
                    (root/name).mkdir()
                    (root/name/'.zattrs').write_text(json.dumps({'units':'s-1'}))
                for name in ('u500','v500'):
                    mod.core.write_compressed_chunk(root/name/'1.0.0',array('f',[0.0]*20).tobytes(),3)
                manifest={'forecast_hours':{'001':{'status':'complete','fields':['u500','v500']}}}
                got=mod.fill_refs_mean_500_vorticity(root,manifest,3)
                self.assertEqual(got,[1])
                self.assertEqual(mod.fill_refs_mean_500_vorticity(root,manifest,3),[])
                self.assertIn('absv500',manifest['forecast_hours']['001']['fields'])
                self.assertEqual(json.loads((root/'absv500'/'.zattrs').read_text())['GRIB_shortName'],'DERIVED')
                self.assertTrue((root/'absv500'/'1.0.0').is_file())

class DeltaTests(unittest.TestCase):
    def test_full_then_delta_and_failed_publish_does_not_acknowledge(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            src=p/'src';src.mkdir();work=p/'work';work.mkdir()
            remote=p/'remote';remote.mkdir()
            store=src/'rrfs_20261029_0000.zarr';store.mkdir()
            (store/'.zgroup').write_text('{"zarr_format":2}')
            (store/'t2m').mkdir();(store/'t2m'/'0.0.0').write_bytes(b'first')
            catalog=src/'catalog_rrfs.json';catalog.write_text('{}')
            called=[]
            real_run=subprocess.run
            def fake_run(argv,check=True,**kwargs):
                called.append(argv)
                if argv[0]=='scp':
                    shutil.copy2(argv[1],remote/Path(argv[2].split(':',1)[1]).name)
                elif argv[0]=='ssh':
                    real_run(['/bin/bash','-c',argv[2]],check=True)
                else:raise AssertionError(argv)
                return mock.Mock(returncode=0)
            with mock.patch.object(mod.shutil,'which',return_value='/usr/bin/ssh'), mock.patch.object(mod.subprocess,'run',side_effect=fake_run):
                mod.publish_one(store,catalog,work,'tester@fake',str(remote))
                self.assertEqual((remote/store.name/'t2m'/'0.0.0').read_bytes(),b'first')
                (store/'t2m'/'0.0.0').write_bytes(b'second new content')
                (store/'t2m'/'1.0.0').write_bytes(b'another hour')
                mod.publish_one(store,catalog,work,'tester@fake',str(remote),keep_archive=True)
                self.assertEqual((remote/store.name/'t2m'/'0.0.0').read_bytes(),b'second new content')
                self.assertEqual((remote/store.name/'t2m'/'1.0.0').read_bytes(),b'another hour')
                with tarfile.open(work/(store.stem+'.delta.tar')) as t:
                    members=set(t.getnames())
                self.assertIn(store.name+'/t2m/0.0.0',members)
                self.assertIn(store.name+'/t2m/1.0.0',members)
                self.assertNotIn(store.name+'/.zgroup',members)
                self.assertIn('catalog_rrfs.json',members)
                stamp=work/'.published'/f'{store.name}.json'
                old=json.loads(stamp.read_text())
                (store/'t2m'/'2.0.0').write_bytes(b'failed')
                with mock.patch.object(mod.subprocess,'run',side_effect=RuntimeError('remote offline')):
                    with self.assertRaises(RuntimeError):
                        mod.publish_one(store,catalog,work,'tester@fake',str(remote))
                self.assertEqual(old,json.loads(stamp.read_text()))

if __name__=='__main__':unittest.main()
