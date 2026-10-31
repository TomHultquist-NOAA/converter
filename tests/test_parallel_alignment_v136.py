"""Performance-mode scheduler bounds and case-study integration regression."""
import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import aligned_analysis as align
import create_case as case


class ImmediateFuture:
    def __init__(self, value): self.value = value
    def result(self): return self.value


class FakePool:
    seen = None
    def __init__(self, max_workers):
        assert 1 <= max_workers <= 4
        self.max_workers = max_workers
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def submit(self, func, store, dest):
        self.seen.append((store.name, dest.name))
        return ImmediateFuture(store.name in ('a.zarr', 'c.zarr', 'd.zarr'))


class AlignSchedulerTests(unittest.TestCase):
    def test_parallel_bounded_budget_skips_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            files = ['a.zarr','b.zarr','c.zarr','d.zarr','e.zarr']
            src = [(i, root/name) for i, name in enumerate(files)]
            seen = []
            FakePool.seen = seen
            with patch.object(align,'ProcessPoolExecutor',FakePool), patch.object(align,'as_completed', lambda fs: fs):
                changed = align.align_stores(src, root, max_new_hours=2, workers=2)
            self.assertEqual({p.name for p in changed}, {'a.zarr','c.zarr'})
            self.assertEqual([x[0] for x in seen], files[:3])
            self.assertEqual(len(seen), 3)

    def test_serial_preserves_mapping_cache_and_budget(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = [(i, root/name) for i,name in enumerate(['a.zarr','b.zarr','c.zarr'])]
            caches=[]
            def stub(store,dest,cache):
                caches.append(id(cache))
                return store.name!='b.zarr'
            with patch.object(align,'process',side_effect=stub):
                changed=align.align_stores(src,root,max_new_hours=2,workers=1)
            self.assertEqual([p.name for p in changed],['a.zarr','c.zarr'])
            self.assertEqual(len(set(caches)),1)

    def test_case_alignment_cli_has_bounded_workers(self):
        text=(Path(__file__).resolve().parents[1]/'create_case.py').read_text()
        self.assertIn("parser.add_argument('--aligned-workers'",text)
        self.assertIn("'--workers',str(getattr(args,'aligned_workers',2))",text)
        text2=(Path(__file__).resolve().parents[1]/'run_multimodel_cron.sh').read_text()
        self.assertIn('--workers "$ALIGNED_WORKERS"',text2)
        self.assertIn('WORKERS="${WORKERS:-16}"',text2)
        text3=(Path(__file__).resolve().parents[1]/'run_model_incremental_cron.sh').read_text()
        self.assertIn('INCREMENTAL_WORKERS="${INCREMENTAL_WORKERS:-12}"',text3)

if __name__=='__main__':unittest.main()
