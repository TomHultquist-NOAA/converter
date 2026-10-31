"""Case-study backfill utility safety and integration command regression tests."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import create_case as case
from retention_policy import load_policy, MODELS

UTC=dt.timezone.utc

class CaseTests(unittest.TestCase):
    def test_cycles_and_validation(self):
        self.assertEqual(len(case.cycles_between(case.parse_cycle('2026102900'),case.parse_cycle('2026103018'))),8)
        for invalid in ('2026102903','2026023000','2026102919','abc','20261029000'):
            with self.assertRaises(ValueError):case.parse_cycle(invalid)
        with self.assertRaises(ValueError):case.cycles_between(case.parse_cycle('2026103018'),case.parse_cycle('2026102900'))
        self.assertEqual(case.model_ids('all'),MODELS)

    def test_168_hour_batches_exactly_cover_valid_times(self):
        first=dt.datetime(2026,10,1,0,tzinfo=UTC)
        last=first+dt.timedelta(hours=366)
        batches=list(case.chunk_hours(first,last))
        self.assertEqual([int((b-a).total_seconds()/3600)+1 for a,b in batches],[168,168,31])
        self.assertEqual(batches[1][0],batches[0][1]+dt.timedelta(hours=1))
        self.assertEqual(batches[-1][1],last)

    def test_preserve_old_pins_analyses_merge_partial(self):
        original={'schema':'omd-evu-protected-cycles-1',
            'protected_cycles':['2026102700_00',{'date':'20261028','cycle':'12','models':['rrfs']}],
            'protected_analysis':[{'start':'2026102500','end':'2026102512','sources':['rap']}]}
        # Correct operational cycle format is YYYYMMDD_CC.
        original['protected_cycles'][0]='20261027_00'
        cycles=[case.parse_cycle('2026102812'),case.parse_cycle('2026102818')]
        updated=case.updated_policy(original,cycles,('hrrr','refs_mean'))
        self.assertEqual(original['protected_cycles'][1]['models'],['rrfs'])
        self.assertEqual(updated['protected_cycles'][1]['models'],['rrfs','refs_mean','hrrr'])
        self.assertEqual(updated['protected_analysis'],original['protected_analysis'])
        self.assertEqual(len(updated['protected_cycles']),3)
        self.assertEqual(case.updated_policy(updated,cycles,('hrrr','refs_mean')),updated)

    def test_atomic_backup_policy_is_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'protected_cycles.json'
            src={'schema':'omd-evu-protected-cycles-1','protected_cycles':[], 'protected_analysis':[]}
            path.write_text(json.dumps(src))
            new=case.updated_policy(src,[case.parse_cycle('2026102900')],tuple(MODELS))
            self.assertTrue(case.save_protection(path,new))
            self.assertEqual(len(list(Path(tmp).glob('protected_cycles.json.case-backup-*'))),1)
            self.assertEqual(set(load_policy(path).model_stores),set(f'{m}_20261029_0000.zarr' for m in MODELS))
            self.assertFalse(case.save_protection(path,new))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_plan_requires_no_operational_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'no-policy.json'
            argv=['--cycle','2026102900','--protected-cycles-file',str(path),'--no-publish']
            self.assertEqual(case.main(argv),0)
            self.assertFalse(path.exists())

    def test_execute_uses_shared_locks_and_exact_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            protected=root/'protected_cycles.json'
            protected.write_text(json.dumps({'schema':'omd-evu-protected-cycles-1','protected_cycles':[], 'protected_analysis':[]}))
            secrets=root/'ptype_obs_secrets.sh'
            secrets.write_text('export SYNOPTIC_API_TOKEN=private\nexport MPING_API_KEY=private\n')
            secrets.chmod(0o600)
            args=argparse.Namespace(protected_cycles_file=protected,output_root=root/'zarr',work_root=root/'work',
                secrets_file=secrets,skip_observations=False,skip_analysis=False,rights_confirmed=True,
                no_publish=False,mode='missing',execute=True,workers=2,max_aligned_stores=1000,
                publish_host='fake.remote',publish_dir='/safe/data',wgrib2=None,python=sys.executable)
            commands=[]
            with patch.object(case,'command',side_effect=lambda cmd,**kw: commands.append(cmd) or 0),\
                 patch.object(case,'collect_observations',return_value=0) as obs:
                status=case.execute(args,[case.parse_cycle('2026102900')],tuple(MODELS),
                    now=dt.datetime(2026,10,30,12,0,tzinfo=UTC))
            self.assertEqual(status,0)
            self.assertEqual(len(commands),5)
            self.assertIn('spc_lsr.py',str(commands[4]))
            self.assertIn('--incremental',commands[0]);self.assertIn('--force-missing-check',commands[0])
            self.assertIn('--publish-only',commands[1]);self.assertIn('--start-hour',commands[2])
            self.assertEqual(commands[2][commands[2].index('--end-hour')+1],'2026103011')
            self.assertIn('--until',commands[3]);self.assertTrue(obs.called)
            self.assertEqual(len(load_policy(protected).model_stores),8)

    def test_future_valid_times_are_deferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);protected=root/'protected_cycles.json'
            protected.write_text(json.dumps({'schema':'omd-evu-protected-cycles-1','protected_cycles':[], 'protected_analysis':[]}))
            args=argparse.Namespace(protected_cycles_file=protected,output_root=root/'zarr',work_root=root/'work',
                secrets_file=root/'not-there',skip_observations=True,skip_analysis=False,rights_confirmed=False,
                no_publish=True,mode='missing',execute=True,workers=2,max_aligned_stores=500,
                publish_host='fake.remote',publish_dir='/safe/data',wgrib2=None,python=sys.executable)
            commands=[]
            with patch.object(case,'command',side_effect=lambda cmd,**kw: commands.append(cmd) or 0):
                status=case.execute(args,[case.parse_cycle('2026102918')],tuple(MODELS),
                    now=dt.datetime(2026,10,29,21,0,tzinfo=UTC))
            self.assertEqual(status,0)
            self.assertIn('--end-hour',commands[1]);self.assertEqual(commands[1][commands[1].index('--end-hour')+1],'2026102920')
            self.assertTrue(all('--publish-only' not in cmd for cmd in commands))

if __name__=='__main__':unittest.main()
