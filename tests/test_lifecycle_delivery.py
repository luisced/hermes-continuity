"""Current draft/readiness transport composition; all effects are fixtures."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from continuity.delivery import deliver_pending
from continuity.extraction import Worker
from continuity.lifecycle import Lifecycle
from continuity.outbox import Outbox
from continuity.store import Store


class LifecycleDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = Store(Path(self.tmp.name)/'data')
        for key, value in [('mode', 'active'), ('timezone', 'UTC'),
                           ('allow_delivery', True), ('delivery_target', 'test:owner')]:
            self.s.configure(key, value)
        self.life = Lifecycle(self.s)
        self.outbox = Outbox(self.s)
        self.messages = []
        self.sender = patch('continuity.delivery.HermesSend', return_value=self.send)
        self.sender.start()
        self.addCleanup(self.sender.stop)

    def send(self, message):
        self.messages.append(message)
        return {'receipt': 'synthetic-readiness-receipt'}

    def prepare_draft(self):
        # Exercise the current real local preparation worker, not hand-built result metadata.
        with self.s.tx() as c:
            item = self.s._create_item(c, 'commitment', 'person', 'Brief', 'request', 'quote',
                                       actor='assistant', task='draft', expectation='local_draft')
        self.s.configure('mode', 'prepare')
        self.s.enqueue('draft', {'item': item['id'], 'revision': 0}, 'fixture-draft', 43200)
        class Model:
            def complete_structured(me, **kw):
                return SimpleNamespace(parsed=dict(title='Brief', context_sources=[],
                    markdown='# Brief\nConfirm the meeting goal and assign a decision owner before the meeting.'))
        Worker(self.s, Model(), clock=lambda: 43200).tick(43200)
        self.s.configure('mode', 'active')
        self.item = self.s.items()[0]
        with self.s.tx() as c:
            self.artifact = dict(c.execute('SELECT * FROM artifacts WHERE id=?',
                                          (self.item['artifact'],)).fetchone())
        self.body = json.loads(self.artifact['body'])
        self.notice = dict(artifact=self.artifact['id'],
                           hash=hashlib.sha256(self.artifact['body'].encode()).hexdigest(),
                           target='test:owner', created=43200)
        return self.item

    def register_verify(self):
        self.life.record_result(self.item['id'], 0, 'person',
                                {'artifact_id': self.artifact['id']}, now=43201)
        self.life.record_result(self.item['id'], 1, 'person',
                                {'artifact_id': self.artifact['id'], 'verify': True}, now=43202)

    def test_real_ready_notice_receipt_is_transport_only(self):
        self.prepare_draft()
        self.assertEqual(deliver_pending(self.s, 43203), 1)
        self.assertEqual(len(self.messages), 1)
        self.assertIn('local draft is ready', self.messages[0])
        self.assertNotIn(self.body['markdown'], self.messages[0])
        snap = self.life.snapshot(self.item['id'], 'person')
        self.assertEqual(snap['transport_state'], 'acknowledged')
        self.assertEqual(snap['result_state'], 'prepared')

    def test_real_prepare_register_verify_preserves_pending_notice_authority(self):
        self.prepare_draft()
        oid = self.outbox.prepare(self.item, self.notice)
        original = self.outbox.get(oid)
        self.register_verify()
        self.assertEqual(self.outbox.get(oid), original)
        self.assertEqual(deliver_pending(self.s, 43203), 1)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.outbox.get(oid)['state'], 'acknowledged')
        snap = self.life.snapshot(self.item['id'], 'person')
        self.assertEqual((snap['revision'], snap['result_state']), (2, 'verified'))
        with self.s.tx() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM outbox').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT body FROM artifacts WHERE id=?',
                                      (self.artifact['id'],)).fetchone()[0], self.artifact['body'])
        self.assertEqual(deliver_pending(self.s, 43204), 0)
        self.assertEqual(len(self.messages), 1)

    def test_running_and_open_preserve_scheduled_review_eligibility(self):
        for stage in ('running', 'open'):
            with self.subTest(stage=stage):
                item = self.s.create_item('commitment', 'person', 'Review', stage, 'quote', review=43200)
                for revision, target in enumerate(('running',) if stage == 'running' else ('running', 'open')):
                    snap = self.life.transition(item['id'], revision, 'person', stage=target,
                        causal_event=None, evidence_ids=[], reason='stage_changed', now=43199)
                    self.assertEqual(snap['item']['review'], 43200)
                worker = Worker(self.s, None, clock=lambda: 43200)
                self.assertEqual(worker.reviews(43199), 0)
                self.assertEqual(worker.reviews(43200), 1)
                fresh = next(i for i in self.s.items() if i['id'] == item['id'])
                self.assertEqual(fresh['review'], 129600)

    def test_registered_result_stays_authorized_across_running_and_open(self):
        self.prepare_draft()
        self.register_verify()
        for revision, stage in ((2, 'running'), (3, 'open')):
            self.life.transition(self.item['id'], revision, 'person', stage=stage,
                causal_event=None, evidence_ids=[], reason='stage_changed', now=43202)
        self.assertEqual(deliver_pending(self.s, 43203), 1)
        self.assertEqual(self.life.snapshot(self.item['id'], 'person')['result_state'], 'verified')

    def test_registered_content_change_or_unrelated_revision_blocks_delivery(self):
        self.prepare_draft()
        self.register_verify()
        with self.s.tx() as c:
            c.execute('UPDATE artifacts SET body=? WHERE id=?',
                      (json.dumps(dict(self.body, title='Changed content metadata')), self.artifact['id']))
        self.assertEqual(deliver_pending(self.s, 43203), 0)
        with self.s.tx() as c:
            c.execute('UPDATE artifacts SET body=? WHERE id=?', (self.artifact['body'], self.artifact['id']))
        self.s.transition(self.item['id'], 2, 'open', reason='unrelated change')
        self.assertEqual(deliver_pending(self.s, 43204), 0)
        self.assertEqual(self.messages, [])
        with self.assertRaises(ValueError):
            self.life.record_result(self.item['id'], 3, 'person',
                                    {'artifact_id': self.artifact['id'], 'verify': True}, now=43205)

    def test_registered_changed_file_blocks_delivery(self):
        self.prepare_draft()
        self.register_verify()
        (self.s.root/self.body['path']).write_text('Changed file')
        self.assertEqual(deliver_pending(self.s, 43203), 0)
        self.assertEqual(self.messages, [])

    def test_legacy_unknown_without_claim_is_not_refreshed_by_registration(self):
        self.prepare_draft()
        oid = self.outbox.prepare(self.item)
        # A historical UNKNOWN fixture may have no claim metadata at all.
        with self.s.tx() as c:
            c.execute("UPDATE outbox SET state='unknown' WHERE id=?", (oid,))
        before = self.outbox.get(oid)
        self.register_verify()
        self.assertEqual(deliver_pending(self.s, 43203), 0)
        self.assertEqual(self.outbox.get(oid), before)
        with self.s.tx() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM outbox_claims').fetchone()[0], 0)
        self.assertEqual(self.messages, [])

    def test_authorized_old_content_revision_does_not_bypass_effect_time_cas(self):
        self.prepare_draft()
        self.register_verify()
        item = self.life.snapshot(self.item['id'], 'person')['item']
        oid = self.outbox.prepare(item, self.notice)
        from contextlib import contextmanager
        original_tx = self.s.tx
        mutated = []
        @contextmanager
        def race():
            with original_tx() as c:
                yield c
            if not mutated:
                mutated.append(True)
                # Commit a genuine metadata-only transition between admission
                # and effect. Content authorization remains valid at revision 3,
                # but the effect must still reject its stale revision-2 capture.
                self.life.transition(item['id'], 2, 'person', stage='running',
                    causal_event=None, evidence_ids=[], reason='stage_changed', now=43203)
        with patch.object(self.s, 'tx', side_effect=race):
            self.assertFalse(self.outbox.dispatch(oid, lambda key: self.send('must not send'), 43203))
        self.assertEqual(self.outbox.get(oid)['state'], 'failed')
        self.assertEqual(self.messages, [])

    def test_revoked_epoch_and_unknown_cannot_gain_new_attempt_from_bookkeeping(self):
        for state in ('revoked', 'unknown', 'acknowledged'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                # Independent test-owned stores, never rewrite historical receipt rows.
                self.s = Store(Path(directory)/'data')
                for key, value in [('mode', 'active'), ('timezone', 'UTC'),
                                   ('allow_delivery', True), ('delivery_target', 'test:owner')]:
                    self.s.configure(key, value)
                self.life = Lifecycle(self.s)
                self.outbox = Outbox(self.s)
                self.prepare_draft()
                oid = self.outbox.prepare(self.item, self.notice)
                if state == 'revoked':
                    self.s.pause(True)
                    self.s.pause(False)
                elif state == 'unknown':
                    with self.assertLogs('continuity.outbox', level='WARNING'):
                        self.assertFalse(self.outbox.dispatch(oid, lambda key: (_ for _ in ()).throw(TimeoutError()), 43200))
                else:
                    self.assertTrue(self.outbox.dispatch(oid, lambda key: {'receipt': 'prior-receipt'}, 43200))
                before = self.outbox.get(oid)
                with self.s.tx() as c:
                    claim = dict(c.execute('SELECT * FROM outbox_claims WHERE id=?', (oid,)).fetchone())
                self.register_verify()
                self.assertEqual(deliver_pending(self.s, 43203), 0)
                self.assertEqual(self.outbox.get(oid), before)
                with self.s.tx() as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM outbox').fetchone()[0], 1)
                    self.assertEqual(dict(c.execute('SELECT * FROM outbox_claims WHERE id=?', (oid,)).fetchone()), claim)
        self.assertEqual(self.messages, [])
