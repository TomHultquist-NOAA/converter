"""RAP inventory definitions and bundled read-only audit sanity checks."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BASE=Path(__file__).resolve().parents[1]

class RAPAuditV132(unittest.TestCase):
    def test_inventory_layers(self):
        info=json.loads((BASE/'verification_sources.json').read_text())['fields']['rap_pressure']
        self.assertEqual(info['mucape'], ['CAPE','255-0 mb above ground'])
        for name in ('h300','u300','v300','h500','u500','v500','absv500','mlcape','srh01','srh03'):
            self.assertIn(name,info)

    def test_empty_root_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            result=subprocess.run([sys.executable,str(BASE/'audit_rap_case.py'),'--root',d,
                                   '--start-hour','2026102812','--end-hour','2026102812',
                                   '--refs-mean-cycle','2026102812','--refs-hour','1'],
                                  text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('h300',result.stdout)
            self.assertIn('absv500',result.stdout)
            self.assertIn('REFS Mean 2026102812 F001:',result.stdout)
            self.assertEqual(list(Path(d).iterdir()),[])

if __name__=='__main__':unittest.main()
