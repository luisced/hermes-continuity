"""Typed workflow acceptance through real scoped reads and managed files."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from continuity.adapters import AdapterRegistry, MarkdownAdapter
from continuity.extraction import Worker
from continuity.inference import Execution
from continuity.lifecycle import Lifecycle
from continuity.store import Store, Stale


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.s = Store(self.root/'data')
        self.s.configure('mode', 'prepare')
        self.s.configure('allow_model', True)
        self.s.configure('identities', {'cli:owner': 'person'})
        self.notes = self.root/'notes'
        self.notes.mkdir()
        (self.notes/'selected.md').write_text('The selected project needs an explicit review of risks and owners.')
        self.s.configure('adapters', [dict(type='markdown', role='notes', scope='project', root=str(self.notes))])
        self.registry = AdapterRegistry()
        self.registry.add('notes', 'project', MarkdownAdapter(self.notes))
        with self.s.tx() as c:
            c.execute('INSERT INTO events VALUES(?,?)', ('request', json.dumps(dict(id='request', principal='person', role='human', text='Prepare a useful document from the selected notes.'))))
            self.item = self.s._create_item(c, 'commitment', 'person', 'Prepare selected document', 'request', 'Prepare a useful document', actor='assistant', task='draft', expectation='local_draft')
        self.config = dict(enabled=True, presets={'document': dict(type='local_draft', principal='person', selectors=[dict(role='notes', scope='project', path='selected.md', required=True)], completion_condition='private_file_verified', delivery_policy='owner_review')})
        self.calls = []
        self.response = dict(title='Project review', markdown='# Project review\nReview the selected project risks and assign an owner before making a decision.', context_sources=['notes:project:selected.md'])
        outer = self
        class Model:
            def complete_structured(me, **kw):
                outer.calls.append(kw)
                return SimpleNamespace(parsed=copy.deepcopy(outer.response))
        self.model = Model()
        self.clock_value = 100
        self.clock = lambda: self.clock_value
        self.limit = patch('hermes_cli.config.load_config_readonly', return_value={'agent': {'max_turns': 8}})
        self.limit.start(); self.addCleanup(self.limit.stop)

    def runner(self):
        self.assertIsNotNone(importlib.util.find_spec('continuity.workflows'), 'typed workflow runner is missing')
        from continuity.workflows import WorkflowRunner
        from continuity.workflow_config import validate_workflow_config
        self.s.configure('workflows', validate_workflow_config(self.config))
        return WorkflowRunner(self.s, self.model, self.registry, Execution(self.s), self.clock)

    def enqueue(self, runner, preset='document', workflow='local_draft'):
        return runner.enqueue(self.item['id'], self.item['revision'], workflow, preset, now=100)

    def meeting(self):
        from continuity.adapters import HostReadAdapter
        self.calendar = self.root/'event.json'
        self.calendar.write_text(json.dumps({'events': [{'summary': 'Project review', 'start': '2026-11-09T09:00:00Z', 'status': 'confirmed'}]}))
        schema = dict(type='object', additionalProperties=False, required=['events'], properties={'events': dict(type='array', maxItems=2, items=dict(type='object', additionalProperties=False, required=['summary', 'start', 'status'], properties={k: dict(type='string') for k in ('summary', 'start', 'status')}))})
        mapping = dict(tool='selected_event', args={'event_id': 'review'}, schema=schema)
        self.reads = []
        def dispatch(tool, args):
            self.reads.append((tool, args))
            return json.loads(self.calendar.read_text())
        describe = lambda name: {'annotations': {'readOnlyHint': True, 'continuityScopedRead': {'tool': mapping['tool'], 'args': mapping['args']}}}
        self.registry.add('calendar', 'review', HostReadAdapter(mapping, dispatch, describe))
        self.s.configure('adapters', self.s.setting('adapters')+[dict(type='host-read', role='calendar', scope='review', mapping=mapping)])
        self.config['presets']['brief'] = dict(self.config['presets']['document'], type='meeting_brief', selectors=[dict(role='calendar', scope='review', required=True), dict(role='notes', scope='project', path='selected.md', required=True)])
        self.response['context_sources'] = ['calendar:review:', 'notes:project:selected.md']

    def test_meeting_brief_reads_selected_event_and_notes_without_attendance_claim(self):
        self.meeting()
        runner = self.runner(); self.enqueue(runner, 'brief', 'meeting_brief')
        result = runner.run(self.s.claim('test', 100, 120), now=100)
        self.assertEqual(result['status'], 'verified'); self.assertTrue(result['evidence_ids'])
        self.assertEqual(self.reads, [('selected_event', {'event_id': 'review'})])
        with self.s.tx() as c:
            selected = c.execute('SELECT artifact FROM items WHERE id=?', (self.item['id'],)).fetchone()[0]
            body = json.loads(c.execute('SELECT body FROM artifacts WHERE id=?', (selected,)).fetchone()[0])
        self.assertEqual(body['type'], 'meeting_brief')
        self.assertIn('not proof of attendance', body['content'])
        self.assertTrue(body['uncertainties'])
        self.assertEqual(len(body['coverage']), 2)
        self.assertEqual(Lifecycle(self.s).snapshot(self.item['id'], 'person')['result_state'], 'verified')

    def test_empty_malformed_unavailable_required_sources_block_without_synthesis(self):
        for content in ({'events': []}, {'events': 'invalid'}, None):
            with self.subTest(content=content):
                self.meeting()
                if content is None: self.calendar.unlink()
                else: self.calendar.write_text(json.dumps(content))
                runner = self.runner(); self.enqueue(runner, 'brief', 'meeting_brief')
                result = runner.run(self.s.claim('test', 100, 120), now=100)
                self.assertEqual(result['status'], 'blocked')
                self.assertIsNone(result['result_id']); self.assertFalse(result['evidence_ids'])
                self.assertFalse(self.calls)
                with self.s.tx() as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM lifecycle_results').fetchone()[0], 0)
                    c.execute('DELETE FROM jobs')

    def test_revoke_and_restore_workflow_config_fences_inflight_read(self):
        runner = self.runner(); self.enqueue(runner)
        original = MarkdownAdapter.read
        def revoke(adapter, path):
            value = original(adapter, path)
            self.s.configure('workflows', dict(self.config, enabled=False))
            self.s.configure('workflows', self.config)
            return value
        with patch.object(MarkdownAdapter, 'read', revoke):
            with self.assertRaises(Stale):
                runner.run(self.s.claim('test', 100, 120), now=100)
        self.assertFalse(self.calls)
        with self.s.tx() as c: self.assertEqual(c.execute('SELECT count(*) FROM artifacts').fetchone()[0], 0)

    def test_worker_dispatches_workflow_with_shared_execution(self):
        runner = self.runner(); self.enqueue(runner)
        report = Worker(self.s, self.model, clock=self.clock, adapters=self.registry).tick(100)
        self.assertEqual(report['processed'], 1)
        self.assertEqual(report['inference']['attempts'], 1)
        self.assertEqual(self.s.jobs()[0]['state'], 'done')
        self.assertEqual(Lifecycle(self.s).snapshot(self.item['id'], 'person')['result_state'], 'verified')

    def test_pending_workflow_epoch_change_is_not_reauthorized_by_claim(self):
        runner = self.runner(); self.enqueue(runner)
        self.s.pause(True); self.s.pause(False)
        self.assertIsNone(self.s.claim('test', 100, 120))
        self.assertEqual(self.s.jobs()[0]['state'], 'blocked')

    def test_cli_config_requires_explicit_sources_and_enqueue_names(self):
        import argparse
        from continuity.cli import Controller, setup_parser
        parser = argparse.ArgumentParser(); setup_parser(parser)
        ctl = Controller(self.root, SimpleNamespace(llm=self.model)); ctl._store = self.s
        path = self.root/'presets.json'; path.write_text(json.dumps(self.config))
        ctl.handle(parser.parse_args(['workflow-config', str(path)]))
        result = ctl.handle(parser.parse_args(['workflow-enqueue', self.item['id'], '--revision', '0', '--workflow', 'local_draft', '--preset', 'document']))
        self.assertEqual(len(self.s.jobs()), 1)
        self.assertEqual(result['job'], self.s.jobs()[0]['id'])
        with self.assertRaises(SystemExit): parser.parse_args(['workflow-enqueue', self.item['id'], '--revision', '0'])
        bad = copy.deepcopy(self.config); bad['presets']['document']['selectors'][0]['scope'] = 'unselected'
        path.write_text(json.dumps(bad))
        before = self.s.setting('workflows')
        with self.assertRaises(ValueError): ctl.handle(parser.parse_args(['workflow-config', str(path)]))
        self.assertEqual(self.s.setting('workflows'), before)
        self.assertFalse(self.calls)

    def test_config_rejects_missing_required_meeting_inputs_and_model_capabilities(self):
        from continuity.workflow_config import validate_workflow_config
        mutations = [
            lambda p: p.update(type='meeting_brief'),
            lambda p: p['selectors'][0].update(role='terminal'),
            lambda p: p['selectors'][0].update(required=False),
            lambda p: p['selectors'][0].update(path='../outside.md'),
            lambda p: p['selectors'][0].update(tool='arbitrary', args={}),
            lambda p: p.update(delivery_policy='send'),
        ]
        for mutate in mutations:
            config = copy.deepcopy(self.config); mutate(config['presets']['document'])
            with self.subTest(config=config), self.assertRaises(ValueError): validate_workflow_config(config)

    def test_optional_missing_source_preserves_partial_coverage_and_uncertainty(self):
        self.config['presets']['document']['selectors'].append(dict(role='notes', scope='project', path='missing.md', required=False))
        runner = self.runner(); self.enqueue(runner)
        result = runner.run(self.s.claim('test', 100, 120), now=100)
        self.assertEqual(result['status'], 'verified')
        with self.s.tx() as c:
            selected = c.execute('SELECT artifact FROM items WHERE id=?', (self.item['id'],)).fetchone()[0]
            body = json.loads(c.execute('SELECT body FROM artifacts WHERE id=?', (selected,)).fetchone()[0])
        self.assertEqual([v['status'] for v in body['coverage']], ['available', 'unavailable'])
        self.assertIn('notes:project:missing.md', ' '.join(body['uncertainties']))
        self.assertIn('Unavailable', body['content'])
        self.assertNotIn('missing.md', self.response['context_sources'])

    def test_deleted_or_tampered_source_evidence_invalidates_verified_result(self):
        runner = self.runner(); self.enqueue(runner)
        result = runner.run(self.s.claim('test', 100, 120), now=100)
        sid = result['evidence_ids'][1]
        with self.s.tx() as c:
            original = c.execute('SELECT body FROM artifacts WHERE id=?', (sid,)).fetchone()[0]
            source = json.loads(original); source['content'] = {'text': 'substituted context'}
            c.execute('UPDATE artifacts SET body=? WHERE id=?', (json.dumps(source), sid))
        self.assertNotEqual(Lifecycle(self.s).snapshot(self.item['id'], 'person')['result_state'], 'verified')
        with self.s.tx() as c:
            c.execute('UPDATE artifacts SET body=? WHERE id=?', (original, sid))
        self.assertEqual(Lifecycle(self.s).snapshot(self.item['id'], 'person')['result_state'], 'verified')
        with self.s.tx() as c: c.execute('DELETE FROM artifacts WHERE id=?', (sid,))
        self.assertNotEqual(Lifecycle(self.s).snapshot(self.item['id'], 'person')['result_state'], 'verified')

    def test_shared_maxturn_one_extraction_workflow_and_checkin_is_durable(self):
        from tests.test_checkin import NOW
        self.clock_value = NOW
        self.s.configure('allow_delivery', True)
        self.s.configure('delivery_target', 'telegram:fixture')
        self.s.configure('checkin', dict(enabled=True, principal='person', sources={}, calendars=[], primary='ledger', allow_reads=True))
        runner = self.runner()
        self.s.enqueue('extract', {'event_id': 'request'}, 'extract-request', 99)
        self.enqueue(runner)
        def complete(**kw):
            self.calls.append(kw)
            return SimpleNamespace(parsed={'proposals': []})
        self.model = SimpleNamespace(complete_structured=complete)
        with patch('hermes_cli.config.load_config_readonly', return_value={'agent': {'max_turns': 1}}):
            first = Worker(self.s, self.model, clock=self.clock, adapters=self.registry).tick(NOW)
            Worker(Store(self.s.root), self.model, clock=self.clock, adapters=self.registry).tick(NOW)
        self.assertEqual(first['inference']['attempts'], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]['schema_name'], 'continuity_extract')
        self.assertEqual(next(j for j in self.s.jobs() if j['kind']=='workflow')['state'], 'blocked')
        with self.s.tx() as c:
            day = dict(c.execute('SELECT state,reason FROM checkin_days').fetchone())
            self.assertEqual(c.execute('SELECT count(*) FROM lifecycle_results').fetchone()[0], 0)
        self.assertEqual(day['state'], 'blocked')
        self.assertIn('inference_turn_limit_exhausted', day['reason'])

    def test_stale_closed_revoked_and_expired_lease_during_model_never_publish(self):
        actions = ['revision', 'closed', 'model', 'mode', 'adapter', 'workflow', 'pause', 'lease']
        for action in actions:
            with self.subTest(action=action):
                # Each race is independent, with a real selected file read first.
                runner = self.runner(); self.enqueue(runner)
                job = self.s.claim('test', self.clock_value, 120)
                def complete(**kw):
                    self.calls.append(kw)
                    if action == 'revision': self.s.transition(self.item['id'], self.item['revision'], 'open')
                    if action == 'closed': self.s.transition(self.item['id'], self.item['revision'], 'done')
                    if action == 'model': self.s.configure('allow_model', False)
                    if action == 'mode': self.s.configure('mode', 'shadow')
                    if action == 'adapter': self.s.configure('adapters', [])
                    if action == 'workflow': self.s.configure('workflows', dict(self.config, enabled=False))
                    if action == 'pause': self.s.pause(True)
                    if action == 'lease': self.clock_value += 121
                    return SimpleNamespace(parsed=copy.deepcopy(self.response))
                runner.model = SimpleNamespace(complete_structured=complete)
                with self.assertRaises(Stale): runner.run(job, now=100)
                with self.s.tx() as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM artifacts').fetchone()[0], 0)
                # Restore test-owned fixture through its ordinary configuration API.
                self.s.pause(False); self.s.configure('mode', 'prepare'); self.s.configure('allow_model', True)
                self.s.configure('adapters', [dict(type='markdown', role='notes', scope='project', root=str(self.notes))])
                with self.s.tx() as c:
                    c.execute('DELETE FROM jobs'); c.execute('DELETE FROM inference_blocks')
                    c.execute("UPDATE items SET state='open',revision=0")
                self.clock_value = 100

    def test_malformed_model_and_missing_citations_block_without_result(self):
        for value in ({'markdown': 'x'}, dict(self.response, context_sources=[]), dict(self.response, context_sources=['invented']), dict(self.response, tools=['terminal'])):
            with self.subTest(value=value):
                runner = self.runner(); self.enqueue(runner); self.response = value
                result = runner.run(self.s.claim('test', 100, 120), now=100)
                self.assertEqual(result['status'], 'blocked')
                with self.s.tx() as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM artifacts').fetchone()[0], 0)
                    c.execute('DELETE FROM jobs')

    def test_result_file_permission_or_content_tamper_removes_verification(self):
        runner = self.runner(); jid = self.enqueue(runner)
        runner.run(self.s.claim('test', 100, 120), now=100)
        path = self.s.root/'drafts'/(jid+'.md')
        original = path.read_text(); path.chmod(0o644)
        self.assertNotEqual(Lifecycle(self.s).snapshot(self.item['id'], 'person')['result_state'], 'verified')
        path.chmod(0o600)
        self.assertEqual(Lifecycle(self.s).snapshot(self.item['id'], 'person')['result_state'], 'verified')
        path.write_text('tampered')
        self.assertNotEqual(Lifecycle(self.s).snapshot(self.item['id'], 'person')['result_state'], 'verified')
        path.write_text(original)

    def test_output_collision_is_not_overwritten_or_deleted(self):
        runner = self.runner(); jid = self.enqueue(runner)
        directory = self.s.root/'drafts'; directory.mkdir(mode=0o700)
        path = directory/(jid+'.md'); path.write_text('preexisting private data')
        result = runner.run(self.s.claim('test', 100, 120), now=100)
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(), 'preexisting private data')

    def test_timeout_remains_blocked_and_dedupe_does_not_duplicate_read(self):
        import threading
        self.meeting()
        adapter = self.registry.select('calendar', 'review', 'read')
        started = threading.Event(); release = threading.Event(); finished = threading.Event()
        original = adapter.dispatch
        def slow(tool, args):
            started.set()
            try:
                release.wait(2)
                return original(tool, args)
            finally: finished.set()
        adapter.dispatch = slow; adapter.timeout = 0.02
        runner = self.runner(); jid = self.enqueue(runner, 'brief', 'meeting_brief')
        try:
            result = runner.run(self.s.claim('test', 100, 120), now=100)
            self.assertTrue(started.is_set()); self.assertEqual(result['status'], 'blocked')
            self.assertEqual(self.enqueue(runner, 'brief', 'meeting_brief'), jid)
            self.assertIsNone(self.s.claim('test', 100, 120)); self.assertFalse(self.calls)
        finally:
            release.set(); self.assertTrue(finished.wait(2))
        self.assertEqual(len(self.reads), 1)
        with self.s.tx() as c: self.assertEqual(c.execute('SELECT count(*) FROM artifacts').fetchone()[0], 0)

    def test_close_during_actual_host_read_blocks_next_read_and_synthesis(self):
        self.meeting(); adapter = self.registry.select('calendar', 'review', 'read')
        original = adapter.dispatch
        def close(tool, args):
            value = original(tool, args)
            self.s.transition(self.item['id'], 0, 'done')
            return value
        adapter.dispatch = close
        runner = self.runner(); self.enqueue(runner, 'brief', 'meeting_brief')
        with patch.object(MarkdownAdapter, 'read', side_effect=AssertionError('late notes read')):
            with self.assertRaises(Stale): runner.run(self.s.claim('test', 100, 120), now=100)
        self.assertEqual(len(self.reads), 1); self.assertFalse(self.calls)

    def test_scope_mismatch_and_symlink_paths_do_not_reach_synthesis(self):
        for attack in ('scope', 'symlink', 'empty'):
            with self.subTest(attack=attack):
                runner = self.runner(); self.enqueue(runner)
                if attack == 'scope': self.registry.add('notes', 'project', MarkdownAdapter(self.root))
                if attack == 'symlink':
                    (self.notes/'selected.md').unlink(); (self.notes/'selected.md').symlink_to(self.root/'event.json')
                if attack == 'empty':
                    (self.notes/'selected.md').unlink(missing_ok=True); (self.notes/'selected.md').write_text('   ')
                result = runner.run(self.s.claim('test', 100, 120), now=100)
                self.assertEqual(result['status'], 'blocked'); self.assertFalse(self.calls)
                self.registry.add('notes', 'project', MarkdownAdapter(self.notes))
                with self.s.tx() as c: c.execute('DELETE FROM jobs')

    def test_worker_no_model_authority_does_not_read(self):
        runner = self.runner(); self.enqueue(runner)
        with patch.object(MarkdownAdapter, 'read', side_effect=AssertionError('unauthorized read')):
            report = Worker(self.s, self.model, clock=self.clock, adapters=self.registry, allow_model=False).tick(100)
        self.assertEqual(report['deferred'], 1); self.assertFalse(self.calls)

    def test_claim_payload_cannot_be_retargeted_to_a_different_preset(self):
        self.config['presets']['alternate'] = copy.deepcopy(self.config['presets']['document'])
        runner = self.runner(); self.enqueue(runner)
        job = self.s.claim('test', 100, 120)
        payload = json.loads(job['payload']); payload['preset'] = 'alternate'; job['payload'] = json.dumps(payload)
        with self.assertRaises(Stale): runner.run(job, now=100)
        self.assertFalse(self.calls)

    def test_verified_meeting_brief_does_not_trigger_replacement_legacy_draft(self):
        self.meeting(); runner = self.runner(); self.enqueue(runner, 'brief', 'meeting_brief')
        runner.run(self.s.claim('test', 100, 120), now=100)
        with self.s.tx() as c: c.execute('UPDATE items SET review=101 WHERE id=?', (self.item['id'],))
        Worker(self.s, None, clock=lambda: 101).reviews(101)
        self.assertEqual(len(self.s.jobs()), 1)
        with self.s.tx() as c:
            self.assertEqual(c.execute('SELECT reason FROM item_events ORDER BY id DESC LIMIT 1').fetchone()[0], 'owner review due')

    def test_missing_required_source_reports_a_specific_private_block(self):
        (self.notes/'selected.md').unlink()
        runner = self.runner(); self.enqueue(runner)
        result = runner.run(self.s.claim('test', 100, 120), now=100)
        self.assertEqual(result['reason'], 'workflow_required_source_unavailable')
        self.assertEqual(result['next_action'], 'restore_selected_source')
        self.assertNotIn(str(self.notes), json.dumps(result))

    def test_worker_meeting_brief_executes_pinned_file_reader_subprocess(self):
        import sys
        from continuity.adapters import CommandReadAdapter
        self.meeting()
        # This fixed operator-reviewed helper reads an actual fixture file; no
        # mock subprocess or invented network response stands in for the read.
        executable = str(Path(sys.executable).resolve())
        mapping = dict(executable=executable, sha256=hashlib.sha256(Path(executable).read_bytes()).hexdigest(),
                       args=['-c', 'from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text())', str(self.calendar)],
                       timezone='UTC', approval='operator-reviewed-read-only', timeout=2,
                       schema=self.s.setting('adapters')[-1]['mapping']['schema'])
        self.s.configure('adapters', self.s.setting('adapters')[:-1]+[dict(type='command-read', role='calendar', scope='review', mapping=mapping)])
        self.registry.add('calendar', 'review', CommandReadAdapter(mapping, self.clock))
        runner = self.runner(); self.enqueue(runner, 'brief', 'meeting_brief')
        report = Worker(self.s, self.model, clock=self.clock, adapters=self.registry).tick(100)
        self.assertEqual(report['processed'], 1); self.assertEqual(report['inference']['attempts'], 1)
        self.assertFalse(self.reads, 'host-dispatch fixture must not substitute for actual helper execution')
        self.assertIn('2026-11-09T09:00:00Z', self.calls[0]['input'][0]['text'])
        self.assertEqual(Lifecycle(self.s).snapshot(self.item['id'], 'person')['result_state'], 'verified')

    def test_threaded_host_dispatch_rechecks_admission_after_describe(self):
        self.meeting(); adapter = self.registry.select('calendar', 'review', 'read')
        describe = adapter.describe
        def revoke(name):
            self.s.configure('workflows', dict(self.config, enabled=False))
            self.s.configure('workflows', self.config)
            return describe(name)
        adapter.describe = revoke
        runner = self.runner(); self.enqueue(runner, 'brief', 'meeting_brief')
        with self.assertRaises(Stale): runner.run(self.s.claim('test', 100, 120), now=100)
        self.assertFalse(self.reads); self.assertFalse(self.calls)

    def test_authority_revision_and_lease_changes_during_file_read_block_model(self):
        original = MarkdownAdapter.read
        for action in ('adapters', 'allow_model', 'mode', 'pause', 'revision', 'lease'):
            with self.subTest(action=action):
                runner = self.runner(); self.enqueue(runner)
                def race(adapter, path):
                    value = original(adapter, path)
                    if action == 'adapters': self.s.configure('adapters', [])
                    if action == 'allow_model': self.s.configure('allow_model', False)
                    if action == 'mode': self.s.configure('mode', 'shadow')
                    if action == 'pause': self.s.pause(True)
                    if action == 'revision': self.s.transition(self.item['id'], 0, 'open')
                    if action == 'lease': self.clock_value += 121
                    return value
                with patch.object(MarkdownAdapter, 'read', race), self.assertRaises(Stale):
                    runner.run(self.s.claim('test', 100, 120), now=100)
                self.assertFalse(self.calls)
                self.s.pause(False); self.s.configure('mode', 'prepare'); self.s.configure('allow_model', True)
                self.s.configure('adapters', [dict(type='markdown', role='notes', scope='project', root=str(self.notes))])
                with self.s.tx() as c:
                    c.execute('DELETE FROM jobs'); c.execute('UPDATE items SET revision=0')
                    self.assertEqual(c.execute('SELECT count(*) FROM artifacts').fetchone()[0], 0)
                self.clock_value = 100

    def test_local_draft_real_private_verified_result_and_dedupe(self):
        runner = self.runner()
        jid = self.enqueue(runner)
        self.assertEqual(self.enqueue(runner), jid)
        result = runner.run(self.s.claim('test', 100, 120), now=100)
        self.assertEqual(result['status'], 'verified')
        self.assertTrue(result['evidence_ids'])
        with self.s.tx() as c:
            registered = dict(c.execute('SELECT * FROM lifecycle_results WHERE id=?', (result['result_id'],)).fetchone())
            body = json.loads(c.execute('SELECT body FROM artifacts WHERE id=?', (registered['artifact'],)).fetchone()[0])
            for eid in body['evidence_ids']:
                self.assertTrue(c.execute('SELECT 1 FROM events WHERE id=? UNION SELECT 1 FROM artifacts WHERE id=?', (eid, eid)).fetchone())
        path = self.s.root/body['path']
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), body['content_digest'])
        self.assertEqual(body['content'], path.read_text())
        self.assertEqual(body['type'], 'local_draft')
        self.assertEqual(body['completion_condition'], 'private_file_verified')
        self.assertEqual(body['coverage'][0]['status'], 'available')
        self.assertEqual(Lifecycle(self.s).snapshot(self.item['id'], 'person')['result_state'], 'verified')
        self.assertEqual(self.s.jobs()[0]['state'], 'done')
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('tools', self.calls[0])
        self.assertIn('selected project', self.calls[0]['input'][0]['text'])
        self.assertEqual(self.s.items()[0]['state'], 'open')
