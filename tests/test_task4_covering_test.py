"""Task 4 fixround1: real CLI/Worker authority and readiness regressions."""
import argparse
import json
import unittest
from unittest.mock import patch

from tests import test_workflows
from continuity.adapters import MarkdownAdapter
from continuity.cli import Controller, setup_parser
from continuity.extraction import Worker
from continuity.store import Stale


class Task4AuthorityTests(unittest.TestCase):
    setUp = test_workflows.WorkflowTests.setUp
    runner = test_workflows.WorkflowTests.runner
    enqueue = test_workflows.WorkflowTests.enqueue

    def cli_enqueue(self):
        parser = argparse.ArgumentParser()
        setup_parser(parser)
        ctl = Controller(self.root, None)
        ctl._store = self.s
        with patch('continuity.cli.time.time', return_value=100):
            return ctl.handle(parser.parse_args(['workflow-enqueue', self.item['id'], '--revision', '0',
                                                '--workflow', 'local_draft', '--preset', 'document']))

    def test_stale_preset_last_identity_removal_or_rebinding_denies_cli_enqueue(self):
        self.runner()
        for identities in ({}, {'cli:owner': 'other'}):
            with self.subTest(identities=identities):
                self.s.configure('identities', identities)
                with patch.object(MarkdownAdapter, 'read') as read:
                    with self.assertRaises(Stale):
                        self.cli_enqueue()
                    Worker(self.s, self.model, clock=self.clock, adapters=self.registry).tick(100)
                    read.assert_not_called()
                self.assertFalse(self.calls)
                self.assertFalse(self.s.jobs())

    def test_identity_revoked_at_actual_read_admission_has_zero_reads_or_model(self):
        self.runner()
        self.cli_enqueue()
        original = self.registry.select
        def select(*args):
            adapter = original(*args)
            self.s.configure('identities', {})
            return adapter
        with patch.object(self.registry, 'select', select), patch.object(MarkdownAdapter, 'read') as read:
            Worker(self.s, self.model, clock=self.clock, adapters=self.registry).tick(100)
            read.assert_not_called()
        self.assertFalse(self.calls)
        self.assertEqual(self.s.jobs()[0]['state'], 'blocked')

    def test_remaining_explicit_alias_preserves_cli_to_worker_authority(self):
        self.runner()
        self.s.configure('identities', {'cli:owner': 'other', 'telegram:alias': 'person'})
        self.cli_enqueue()
        report = Worker(self.s, self.model, clock=self.clock, adapters=self.registry).tick(100)
        self.assertEqual(report['processed'], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.s.jobs()[0]['state'], 'done')


class Task4ReadinessTests(unittest.TestCase):
    setUp = test_workflows.WorkflowTests.setUp
    runner = test_workflows.WorkflowTests.runner
    enqueue = test_workflows.WorkflowTests.enqueue

    def ready(self):
        self.clock_value = 43200
        self.s.configure('mode', 'active')
        self.s.configure('allow_delivery', True)
        self.s.configure('delivery_target', 'telegram:fixture')
        runner = self.runner()
        self.aid = self.enqueue(runner)
        result = runner.run(self.s.claim('test', self.clock_value, 120), now=self.clock_value)
        self.sid = result['evidence_ids'][1]
        with self.s.tx() as c:
            self.source_body = c.execute('SELECT body FROM artifacts WHERE id=?', (self.sid,)).fetchone()[0]
        self.path = self.s.root/'drafts'/(self.aid+'.md')
        self.original_text = self.path.read_text()
        self.sent = []

    def transport(self, key):
        self.sent.append(key)
        return {'receipt': 'fixture-receipt'}

    def invalidate(self, c, change):
        if change == 'delete_source':
            c.execute('DELETE FROM artifacts WHERE id=?', (self.sid,))
        elif change == 'tamper_source':
            row = c.execute('SELECT body FROM artifacts WHERE id=?', (self.sid,)).fetchone()
            body = json.loads(row[0]); body['content'] = {'text': 'substituted'}
            c.execute('UPDATE artifacts SET body=? WHERE id=?', (json.dumps(body), self.sid))
        elif change == 'mode':
            self.path.chmod(0o644)
        elif change == 'file':
            self.path.write_text('substituted document')

    def check_invalid(self, boundary):
        from continuity.delivery import deliver_pending
        from continuity.outbox import Outbox
        for change in ('delete_source', 'tamper_source', 'mode', 'file'):
            with self.subTest(boundary=boundary, change=change):
                self.setUp()
                self.ready()
                if boundary == 'selection':
                    with self.s.tx() as c: self.invalidate(c, change)
                    self.assertEqual(deliver_pending(self.s, self.clock_value, self.transport), 0)
                    with self.s.tx() as c:
                        self.assertEqual(c.execute('SELECT count(*) FROM outbox').fetchone()[0], 0)
                elif boundary == 'prepare':
                    original = Outbox.prepare
                    def race(outbox, *args, **kwargs):
                        key = original(outbox, *args, **kwargs)
                        with self.s.tx() as c: self.invalidate(c, change)
                        return key
                    with patch.object(Outbox, 'prepare', race):
                        self.assertEqual(deliver_pending(self.s, self.clock_value, self.transport), 0)
                else:
                    original = Outbox.notice_valid
                    checks = []
                    def race(outbox, c, *args, **kwargs):
                        valid = original(outbox, c, *args, **kwargs)
                        checks.append(valid)
                        if len(checks) == 1:
                            self.assertTrue(valid)
                            self.invalidate(c, change)
                        return valid
                    with patch.object(Outbox, 'notice_valid', race):
                        self.assertEqual(deliver_pending(self.s, self.clock_value, self.transport), 0)
                    self.assertEqual(checks, [True, False])
                    with self.s.tx() as c:
                        self.assertEqual(c.execute('SELECT state FROM outbox').fetchone()[0], 'failed')
                    # Repairing evidence cannot grant a consumed claim a retry.
                    with self.s.tx() as c:
                        c.execute('INSERT OR REPLACE INTO artifacts VALUES(?,?,?)',
                                  (self.sid, self.item['id'], self.source_body))
                    self.path.write_text(self.original_text)
                    self.path.chmod(0o600)
                    self.assertEqual(deliver_pending(self.s, self.clock_value, self.transport), 0)
                self.assertFalse(self.sent)


    def test_invalid_typed_evidence_excluded_at_readiness_selection(self):
        self.check_invalid('selection')

    def test_typed_evidence_rechecked_after_preparation_before_dispatch(self):
        self.check_invalid('prepare')

    def test_typed_evidence_rechecked_after_claim_before_actual_effect(self):
        self.check_invalid('effect')

    def test_valid_readiness_retains_original_attempt_identity_and_transport_only_receipt(self):
        import uuid
        from continuity.delivery import deliver_pending
        from continuity.lifecycle import Lifecycle
        self.ready()
        self.assertEqual(deliver_pending(self.s, self.clock_value, self.transport), 1)
        expected = uuid.uuid5(uuid.NAMESPACE_URL, self.item['id']+':0').hex
        self.assertEqual(self.sent, [expected])
        self.assertEqual(deliver_pending(self.s, self.clock_value, self.transport), 0)
        snapshot = Lifecycle(self.s).snapshot(self.item['id'], 'person')
        self.assertEqual(snapshot['result_state'], 'verified')
        self.assertEqual(self.s.items()[0]['state'], 'open')

    def test_untagged_legacy_draft_keeps_content_only_readiness_contract(self):
        from tests.test_lifecycle import LifecycleTests
        from continuity.delivery import deliver_pending
        self.s.configure('mode', 'active')
        self.s.configure('allow_delivery', True)
        self.s.configure('delivery_target', 'telegram:fixture')
        LifecycleTests.draft(self)
        self.sent = []
        self.assertEqual(deliver_pending(self.s, 43200, self.transport), 1)
        self.assertEqual(len(self.sent), 1)

    def test_typed_unknown_attempt_is_never_retried(self):
        from continuity.delivery import deliver_pending
        self.ready()
        def uncertain(key):
            self.sent.append(key)
            raise TimeoutError('fixture')
        self.assertEqual(deliver_pending(self.s, self.clock_value, uncertain), 0)
        self.assertEqual(deliver_pending(self.s, self.clock_value, self.transport), 0)
        self.assertEqual(len(self.sent), 1)
        with self.s.tx() as c:
            self.assertEqual(c.execute('SELECT state FROM outbox').fetchone()[0], 'unknown')
