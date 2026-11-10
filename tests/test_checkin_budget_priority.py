"""Retired daily quota rows cannot starve opted-in work."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from tests import test_checkin as fixtures
from continuity.extraction import Worker

class CheckinBudgetPriorityTests(unittest.TestCase):
    def fixture(self):
        case=fixtures.CheckinTests(); case.setUp(); self.addCleanup(case.doCleanups)
        return case

    def test_more_than_ten_actual_inferences_across_executions_have_no_daily_quota(self):
        case=self.fixture(); config=case.s.setting('checkin',{}); config['enabled']=False; case.s.configure('checkin',config)
        for i in range(12):
            case.s.ingest({'id':str(i),'role':'human','principal':'fixture','text':'A task','observed_at':fixtures.NOW})
        calls=[]
        def complete(**kw):
            calls.append(kw); return SimpleNamespace(parsed={'proposals':[]})
        with patch('hermes_cli.config.load_config_readonly',return_value={'agent':{'max_turns':7}}):
            for _ in range(3): Worker(case.s,SimpleNamespace(complete_structured=complete),clock=lambda:fixtures.NOW).tick(fixtures.NOW)
        self.assertEqual(len(calls),12)
        self.assertTrue(all(j['state']=='done' for j in case.s.jobs()))
        with case.s.tx() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM budgets WHERE kind IN ('model','checkin_model')").fetchone()[0],0)

    def test_legacy_model_and_checkin_model_counters_are_inert_not_reset(self):
        case=self.fixture()
        with case.s.tx() as c:
            c.executemany('INSERT INTO budgets VALUES(?,?,?)',[(k,'2026-11-02',999) for k in ('model','checkin_model')])
        case.worker().tick(fixtures.NOW)
        self.assertEqual(len(case.sent),1); self.assertEqual(len(case.calls),1)
        with case.s.tx() as c:
            self.assertEqual([r[0] for r in c.execute("SELECT n FROM budgets WHERE kind IN ('model','checkin_model')")],[999,999])
