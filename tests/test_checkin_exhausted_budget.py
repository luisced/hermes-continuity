import unittest
from unittest.mock import patch
from continuity.checkin import Checkins
from . import test_checkin as fixtures

class ExhaustedBudgetReadTests(unittest.TestCase):
    def test_retired_daily_synthesis_budget_does_not_suppress_needed_revalidation(self):
        case=fixtures.CheckinTests(); case.setUp(); self.addCleanup(case.doCleanups)
        with case.s.tx() as c:
            c.execute("INSERT INTO budgets VALUES('checkin_model','2026-11-02',999)")
        engine=Checkins(case.s,case.model,clock=lambda:fixtures.NOW,transport=case.transport)
        with patch.object(engine,'collect',wraps=engine.collect) as collect:
            self.assertEqual(engine.tick(fixtures.NOW),1)
            self.assertEqual(collect.call_count,2)
        self.assertEqual(len(case.calls),1)
