"""Execution-wide inference turn regressions; synthetic providers, no effects."""
import json
import io
from contextlib import redirect_stdout
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from continuity.store import Store
from continuity.extraction import Worker
from tests import test_checkin as fixtures

NOW = fixtures.NOW

class ExecutionLimits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.s = Store(Path(self.tmp.name)/'continuity')
        self.s.configure('allow_model', True)
        self.calls = []
        def complete(**kw):
            self.calls.append(kw)
            return SimpleNamespace(parsed={'proposals': []})
        self.model = SimpleNamespace(complete_structured=complete)

    def ingest(self, key):
        self.s.ingest({'id':key, 'role':'human', 'principal':'fixture', 'text':'Remember the report', 'observed_at':NOW})

    def tick(self, limit):
        with patch('hermes_cli.config.load_config_readonly', return_value={'agent':{'max_turns':limit}}):
            return Worker(self.s,self.model,clock=lambda:NOW).tick(NOW)

    def test_live_host_limit_shared_across_jobs_and_durable_block_on_restart(self):
        for key in ('one','two','three'): self.ingest(key)
        report = self.tick(1)
        self.assertEqual(len(self.calls),1)
        blocked=[j for j in self.s.jobs() if j['state']=='blocked']
        self.assertEqual(len(blocked),2)
        self.assertTrue(all('inference_turn_limit_exhausted' in j['error'] for j in blocked))
        self.assertEqual(report['inference']['attempts'],1)
        self.assertEqual(report['inference']['max_turns'],1)
        self.s=Store(self.s.root)
        self.tick(1)
        self.assertEqual(len(self.calls),1, 'new tick cannot reset unchanged blocked work')
        for key in ('four','five'): self.ingest(key)
        self.tick(2)
        self.assertEqual(len(self.calls),3, 'next execution inherits changed host config')

    def test_failed_inference_consumes_turn_and_old_daily_quota_is_ignored(self):
        self.ingest('one'); self.ingest('two')
        with self.s.tx() as c: c.execute("INSERT INTO budgets VALUES('model','2026-11-02',999)")
        def failed(**kw):
            self.calls.append(kw)
            raise RuntimeError('private provider failure')
        self.model=SimpleNamespace(complete_structured=failed)
        self.tick(1)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.s.jobs()[1]['state'],'blocked')
        self.tick(1)
        self.assertEqual(len(self.calls),1)

    def test_extraction_and_checkin_share_execution_and_exhaustion_is_visible(self):
        case=fixtures.CheckinTests(); case.setUp(); self.addCleanup(case.doCleanups)
        case.s.ingest({'id':'extract','role':'human','principal':'other','text':'A task','observed_at':NOW})
        calls=[]
        def complete(**kw):
            calls.append(kw)
            return SimpleNamespace(parsed={'proposals':[]})
        for _ in range(2):
            with patch('hermes_cli.config.load_config_readonly', return_value={'agent':{'max_turns':1}}):
                Worker(case.s,SimpleNamespace(complete_structured=complete),clock=lambda:NOW,transport=case.transport).tick(NOW)
        self.assertEqual(len(calls),1)
        with case.s.tx() as c:
            day=dict(c.execute('SELECT * FROM checkin_days').fetchone())
        self.assertEqual(day['state'],'blocked')
        self.assertIn('inference_turn_limit_exhausted',day['reason'])
        self.assertFalse(case.sent)

    def test_exhaustion_fences_backlog_beyond_batch_size_and_pending_failures(self):
        for key in range(8): self.ingest(str(key))
        def complete(**kw):
            self.calls.append(kw)
            if len(self.calls)==1: raise RuntimeError('transient failure')
            return SimpleNamespace(parsed={'proposals':[]})
        self.model=SimpleNamespace(complete_structured=complete)
        self.tick(2)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(sum(j['state']=='blocked' for j in self.s.jobs()),7)
        self.tick(2)
        self.assertEqual(len(self.calls),2)

    def test_missing_config_uses_host_default_resolver_not_a_plugin_constant(self):
        from hermes_cli.config import resolve_turn_limit
        self.ingest('one')
        with patch('hermes_cli.config.load_config_readonly',return_value={}):
            report=Worker(self.s,self.model,clock=lambda:NOW).tick(NOW)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(report['inference']['max_turns'],resolve_turn_limit(None))

    def test_reused_checkin_object_gets_new_execution_on_new_day(self):
        from continuity.checkin import Checkins
        case=fixtures.CheckinTests(); case.setUp(); self.addCleanup(case.doCleanups)
        clock=[NOW]
        engine=Checkins(case.s,case.model,clock=lambda:clock[0],transport=case.transport)
        with patch('hermes_cli.config.load_config_readonly',return_value={'agent':{'max_turns':1}}):
            self.assertEqual(engine.tick(NOW),1)
            clock[0]+=86400
            self.assertEqual(engine.tick(clock[0]),0)  # Same subject, not a new contact.
        self.assertEqual(len(case.calls),2)  # Still a new, independently bounded execution.
        with case.s.tx() as c:
            self.assertEqual(c.execute('SELECT reason FROM checkin_days ORDER BY day DESC LIMIT 1').fetchone()[0],'unchanged_subject')

    def test_operator_why_shows_persisted_execution_and_block_reasons(self):
        from continuity.cli import Controller
        self.ingest('one'); self.ingest('two')
        self.tick(1)
        controller=Controller(Path(self.tmp.name),None); controller._store=self.s
        with redirect_stdout(io.StringIO()): result=controller.handle(SimpleNamespace(action='why'))
        self.assertIn('inference',result)
        self.assertEqual(result['inference']['executions'][0]['attempts'],1)
        self.assertEqual(result['inference']['blocks'][0]['reason'],'inference_turn_limit_exhausted')

    def test_host_plugin_llm_parse_failure_is_a_counted_inference_attempt(self):
        from agent.plugin_llm import make_plugin_llm_for_test, _TrustPolicy
        self.ingest('one'); self.ingest('two')
        def caller(**kw):
            self.calls.append(kw)
            return ('fixture','fixture',SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='not JSON'))]))
        self.model=make_plugin_llm_for_test(plugin_id='hermes-continuity',policy=_TrustPolicy(plugin_id='hermes-continuity'),sync_caller=caller)
        report=self.tick(1)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(report['inference']['attempts'],1)
        self.assertTrue(all(j['state']=='blocked' for j in self.s.jobs()))
        self.tick(1)
        self.assertEqual(len(self.calls),1)

    def test_extract_and_generated_draft_share_budget(self):
        self.ingest('one'); self.s.configure('mode','prepare')
        def complete(**kw):
            self.calls.append(kw)
            return SimpleNamespace(parsed={'proposals':[{'kind':'commitment','owner':'assistant','source':'one',
                'quote':'Remember the report','title':'Report','expectation':'local_draft','task':'draft'}]})
        self.model=SimpleNamespace(complete_structured=complete)
        report=self.tick(1)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(report['inference']['attempts'],1)
        draft=next(j for j in self.s.jobs() if j['kind']=='draft')
        self.assertEqual(draft['state'],'blocked')
        self.assertEqual(draft['error'],'inference_turn_limit_exhausted')
        self.assertFalse((self.s.root/'drafts').exists())

    def test_crash_after_counted_attempt_cannot_reset_exhausted_work(self):
        self.ingest('one')
        class Interrupted(BaseException): pass
        def interrupted(**kw):
            self.calls.append(kw); raise Interrupted()
        self.model=SimpleNamespace(complete_structured=interrupted)
        with self.assertRaises(Interrupted): self.tick(1)
        self.s=Store(self.s.root)
        def complete(**kw):
            self.calls.append(kw); return SimpleNamespace(parsed={'proposals':[]})
        with patch('hermes_cli.config.load_config_readonly',return_value={'agent':{'max_turns':1}}):
            Worker(self.s,SimpleNamespace(complete_structured=complete),clock=lambda:NOW+121).tick(NOW+121)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.s.jobs()[0]['state'],'blocked')
        self.assertEqual(self.s.jobs()[0]['error'],'inference_turn_limit_exhausted')

    def test_invalid_host_limit_fails_closed_without_inference(self):
        self.ingest('one')
        report=self.tick(False)
        self.assertFalse(self.calls)
        self.assertIn('invalid_agent_max_turns',json.dumps(report))
