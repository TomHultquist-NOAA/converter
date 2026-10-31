"""RZDM retention parity: only expired, unprotected observation records removed."""
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
import multi_model_to_zarr_wcoss2 as conv

class RemoteRetentionV122(unittest.TestCase):
    def test_expired_obs_and_transfer_files_only(self):
        with TemporaryDirectory() as td:
            root=Path(td)/'data';obs=root/'ptype_observations';obs.mkdir(parents=True)
            expired=obs/'2026090100.json';pinned=obs/'2026090200.json';recent=obs/'2026102900.json'
            for f in (expired,pinned,recent):f.write_text('{}')
            archive=root/'.rrfs_20260901_0000.delta.tar.incoming';archive.write_text('old')
            stamp=dt.datetime(2026,9,1,tzinfo=dt.timezone.utc).timestamp();os.utime(archive,(stamp,stamp))
            unrelated=root/'README.md';unrelated.write_text('leave me')
            cutoff='2026-10-16T12:00:00Z'
            args=[sys.executable,'-',str(root),cutoff,'0','[]',json.dumps([['2026090200','2026090200']])]
            proc=subprocess.run(args,input=conv.REMOTE_RETENTION_SCRIPT,text=True,capture_output=True)
            self.assertEqual(proc.returncode,0,proc.stderr)
            self.assertFalse(expired.exists());self.assertTrue(pinned.exists());self.assertTrue(recent.exists())
            self.assertFalse(archive.exists());self.assertTrue(unrelated.exists())

if __name__=='__main__':unittest.main()
