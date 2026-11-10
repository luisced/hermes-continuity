"""Synthetic regressions for independent review; no live effects."""
import hashlib
import json
import unittest
from unittest.mock import patch
from tests import test_checkin as fixtures
from continuity.checkin import Checkins, validate_message
from continuity.outbox import Outbox

NOW = fixtures.NOW


class PreparationFixes(unittest.TestCase):
    # Reuse fixture setup without inheriting/discovering its entire test class.
    s: fixtures.Store
    model: object
    calls: list
    sent: list
    item: dict
    setUp = fixtures.CheckinTests.setUp
    transport = fixtures.CheckinTests.transport

    def engine(self, clock):
        return Checkins(self.s, self.model, clock=lambda: clock[0], transport=self.transport)

    def prepare_pending(self,engine,now):
        # Isolate a never-claimed preparation without the removed cooldown.
        with patch.object(Outbox,'dispatch',return_value=False): return engine.tick(now)

    def test_pending_reprepares_current_hash_without_extra_model_or_attempt(self):
        clock = [NOW]
        self.s.configure('last_contact', NOW - 60)
        engine = self.engine(clock)
        self.assertEqual(self.prepare_pending(engine,clock[0]), 0)
        with self.s.tx() as c:
            self.assertEqual(c.execute('SELECT state FROM outbox').fetchone()[0], 'planned')
            self.assertIsNone(c.execute("SELECT n FROM budgets WHERE kind='checkin_delivery'").fetchone())
        clock[0] += 10800
        self.assertEqual(engine.tick(clock[0]), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.calls), 1)
        with self.s.tx() as c:
            notice = json.loads(c.execute('SELECT notice FROM outbox_claims').fetchone()[0])
            encoded = c.execute('SELECT body FROM artifacts').fetchone()[0]
            self.assertEqual(notice['hash'], hashlib.sha256(encoded.encode()).hexdigest())
            self.assertEqual(c.execute("SELECT n FROM budgets WHERE kind='checkin_delivery'").fetchone()[0], 1)

    def test_consumed_preparations_never_refresh_notice_or_epoch(self):
        clock = [NOW]
        self.s.configure('last_contact', NOW - 60)
        self.prepare_pending(self.engine(clock),NOW)
        with self.s.tx() as c:
            item = dict(c.execute("SELECT * FROM items WHERE kind='checkin'").fetchone())
            oid = c.execute('SELECT id FROM outbox').fetchone()[0]
            original = c.execute('SELECT notice FROM outbox_claims').fetchone()[0]
        for state, token, until, reserved in [('sending', None, None, False),
                ('unknown', None, None, False), ('acknowledged', None, None, False),
                ('failed', None, None, False), ('planned', 'spent', None, False),
                ('planned', None, NOW + 60, False), ('planned', None, None, True)]:
            with self.subTest(state=state, token=token, until=until, reserved=reserved):
                with self.s.tx() as c:
                    c.execute('UPDATE outbox SET state=?,epoch=-1 WHERE id=?', (state, oid))
                    c.execute('UPDATE outbox_claims SET token=?,until=?,notice=? WHERE id=?', (token, until, original, oid))
                    c.execute("DELETE FROM budgets WHERE kind='checkin_delivery'")
                    if reserved: c.execute("INSERT INTO budgets VALUES('checkin_delivery','2026-11-02',1)")
                Outbox(self.s).prepare(item, dict(json.loads(original), created=NOW + 1))
                with self.s.tx() as c:
                    self.assertEqual(c.execute('SELECT epoch FROM outbox').fetchone()[0], -1)
                    self.assertEqual(c.execute('SELECT notice FROM outbox_claims').fetchone()[0], original)

    def dispatch_at_boundary(self, start, elapsed, mutate=None, runner=None):
        from contextlib import contextmanager
        from types import SimpleNamespace
        original = Outbox.dispatch
        def wrapped(outbox, oid, transport, now):
            tx = self.s.tx
            entries = [0]
            @contextmanager
            def between_transactions():
                entries[0] += 1
                if entries[0] == 2 and mutate: mutate()
                with tx() as c: yield c
            with patch.object(self.s, 'tx', between_transactions), patch(
                    'continuity.outbox.time', SimpleNamespace(monotonic=iter([100.0, 100.0 + elapsed, 100.0 + elapsed]).__next__)):
                return original(outbox, oid, transport, now)
        with patch.object(Outbox, 'dispatch', wrapped):
            return runner(start) if runner else self.engine([start]).tick(start)

    def test_final_admission_rejects_crossing_quiet_hours_and_keeps_one_reservation(self):
        from datetime import datetime, timezone
        start = datetime(2026, 11, 2, 20, 59, 59, tzinfo=timezone.utc).timestamp()
        self.assertEqual(self.dispatch_at_boundary(start, 2), 0)
        self.assertFalse(self.sent)
        self.engine([start + 3]).tick(start + 3)
        with self.s.tx() as c:
            self.assertEqual(c.execute("SELECT n FROM budgets WHERE kind='checkin_delivery'").fetchone()[0], 1)

    def test_final_admission_inside_window_preserves_notice_review_exception(self):
        def reviewed():
            with self.s.tx() as c:
                c.execute("UPDATE items SET review=? WHERE kind='checkin'", (NOW + 3600,))
        self.assertEqual(self.dispatch_at_boundary(NOW, 2, reviewed), 1)
        self.assertEqual(len(self.sent), 1)

    def test_final_admission_has_no_global_contact_cooldown(self):
        self.assertEqual(self.dispatch_at_boundary(NOW, 2,
            lambda: self.s.configure('last_contact', NOW + 1)), 1)
        self.assertEqual(len(self.sent),1)

    def test_final_admission_ignores_retired_shared_delivery_cap(self):
        def exhausted():
            with self.s.tx() as c:
                c.execute("INSERT OR REPLACE INTO budgets VALUES('delivery','2026-11-02',999)")
        self.assertEqual(self.dispatch_at_boundary(NOW, 2, exhausted), 1)
        self.assertEqual(len(self.sent),1)

    def test_consumed_checkin_without_claim_row_does_not_adopt_notice(self):
        self.s.configure('last_contact', NOW - 60)
        self.prepare_pending(self.engine([NOW]),NOW)
        with self.s.tx() as c:
            item = dict(c.execute("SELECT * FROM items WHERE kind='checkin'").fetchone())
            notice = json.loads(c.execute('SELECT notice FROM outbox_claims').fetchone()[0])
            c.execute("UPDATE outbox SET state='unknown'")
            c.execute('DELETE FROM outbox_claims')
        Outbox(self.s).prepare(item, notice)
        with self.s.tx() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM outbox_claims').fetchone()[0], 0)

    def test_checkin_without_revalidated_notice_cannot_dispatch(self):
        item = self.s.create_item('checkin', 'fixture', 'No prepared context', 'fixture:bare', 'bare')
        outbox = Outbox(self.s)
        oid = outbox.prepare(item)
        effects = []
        self.assertFalse(outbox.dispatch(oid, lambda key: effects.append(key) or {'receipt': 'fixture'}, NOW))
        self.assertFalse(effects)

    def test_planned_row_with_consumed_claim_cannot_dispatch_again(self):
        self.s.configure('last_contact', NOW - 60)
        self.prepare_pending(self.engine([NOW]),NOW)
        with self.s.tx() as c:
            oid = c.execute('SELECT id FROM outbox').fetchone()[0]
            c.execute("UPDATE outbox_claims SET token='consumed',until=NULL")
        self.s.configure('last_contact', None)
        self.assertFalse(Outbox(self.s).dispatch(oid, self.transport, NOW))
        self.assertFalse(self.sent)

    def test_stale_epoch_artifact_cannot_refresh_existing_preparation(self):
        self.s.configure('last_contact', NOW - 60)
        self.prepare_pending(self.engine([NOW]),NOW)
        with self.s.tx() as c:
            item = dict(c.execute("SELECT * FROM items WHERE kind='checkin'").fetchone())
            before = dict(c.execute('SELECT * FROM outbox').fetchone())
            notice = json.loads(c.execute('SELECT notice FROM outbox_claims').fetchone()[0])
        self.s.configure('allow_delivery', False)
        self.s.configure('allow_delivery', True)
        Outbox(self.s).prepare(item, notice)
        self.assertEqual(Outbox(self.s).get(before['id']), before)
        self.assertFalse(Outbox(self.s).dispatch(before['id'], self.transport, NOW))
        self.assertFalse(self.sent)

    def test_final_admission_rechecks_checkin_cap(self):
        def exhausted():
            with self.s.tx() as c:
                c.execute("UPDATE budgets SET n=2 WHERE kind='checkin_delivery'")
        self.assertEqual(self.dispatch_at_boundary(NOW, 2, exhausted), 0)
        self.assertFalse(self.sent)

    def test_general_outbox_without_notice_rechecks_future_review(self):
        outbox = Outbox(self.s)
        oid = outbox.prepare(self.item)
        effects = []
        def snoozed():
            with self.s.tx() as c:
                c.execute('UPDATE items SET review=? WHERE id=?', (NOW + 3600, self.item['id']))
        runner = lambda start: outbox.dispatch(oid, lambda key: effects.append(key) or {'receipt': 'fixture'}, start)
        self.assertFalse(self.dispatch_at_boundary(NOW, 2, snoozed, runner))
        self.assertFalse(effects)

    def test_general_outbox_without_notice_rechecks_quiet_hours(self):
        from datetime import datetime, timezone
        start = datetime(2026, 11, 2, 20, 59, 59, tzinfo=timezone.utc).timestamp()
        outbox = Outbox(self.s)
        oid = outbox.prepare(self.item)
        effects = []
        runner = lambda now: outbox.dispatch(oid, lambda key: effects.append(key) or {'receipt': 'fixture'}, now)
        self.assertFalse(self.dispatch_at_boundary(start, 2, runner=runner))
        self.assertFalse(effects)

    def test_path_spellings_are_rejected_in_grounded_message(self):
        evidence = {'ledger': {'status': 'OK', 'content': ['el documento pendiente']}}
        for path in ['/home/owner/private.md', '/private.md', '~/private.md', '~owner/private.md',
                     './private.md', '../private.md', 'notes/private.md',
                     r'C:\Users\owner\private.md', 'C:/Users/owner/private.md', 'C:private.md',
                     r'\\server\share\private.md', r'\private.md']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_message({'message': 'Sobre el documento pendiente en ' + path + ', ¿cómo vas?',
                    'citations': [{'source': 'ledger', 'quote': 'el documento pendiente'}]}, evidence)

    def test_grounded_prose_without_path_syntax_remains_valid(self):
        message = 'Sobre el documento pendiente de las 09:00, ¿cómo vas?'
        self.assertEqual(validate_message({'message': message,
            'citations': [{'source': 'ledger', 'quote': 'el documento pendiente'}]},
            {'ledger': {'status': 'OK', 'content': ['el documento pendiente']}}), message)

    def test_late_manual_response_belongs_only_to_original_checkin(self):
        from continuity.intake import admit_event, OwnerContext
        self.s.configure('identities', {'cli:fixture': 'fixture'})
        clock = [NOW]
        engine = self.engine(clock)
        engine.tick(NOW)
        clock[0] += 86400
        engine.tick(clock[0])
        with self.s.tx() as c:
            days = [dict(r) for r in c.execute('SELECT * FROM checkin_days ORDER BY day')]
        clock[0] += 60
        with patch('continuity.intake.time.time', return_value=clock[0]):
            event = admit_event(self.s, OwnerContext.local('cli', 'fixture'),
                {'id': 'late-human', 'text': 'Respuesta explícita al check-in anterior'})
            self.s.ingest(event)
        self.assertTrue(engine.record_response(days[0]['id'], event['id'], 'wellbeing', []))
        with self.s.tx() as c:
            states = dict(c.execute('SELECT id,state FROM checkin_days'))
            self.assertEqual(states[days[0]['id']], 'acknowledged')
            self.assertEqual(states[days[1]['id']], 'blocked')
            self.assertEqual(c.execute('SELECT day_id FROM checkin_responses').fetchone()[0], days[0]['id'])
        with self.assertRaises(ValueError):
            engine.record_response(days[1]['id'], event['id'], 'wellbeing', [])

    def test_epoch_change_requires_new_draft_then_prepares_new_authority(self):
        clock = [NOW]
        self.s.configure('last_contact', NOW - 60)
        engine = self.engine(clock)
        self.prepare_pending(engine,NOW)
        self.s.configure('allow_delivery', False)
        self.s.configure('allow_delivery', True)
        clock[0] += 10800
        self.assertEqual(engine.tick(clock[0]), 0)  # discard, never adopt
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(engine.tick(clock[0]), 1)
        self.assertEqual(len(self.calls), 2)
        with self.s.tx() as c:
            self.assertEqual(c.execute('SELECT epoch FROM outbox').fetchone()[0], self.s._epoch(c))
            body = json.loads(c.execute('SELECT body FROM artifacts').fetchone()[0])
            self.assertEqual(body['epoch'], self.s._epoch(c))
