"""Synthetic admitted-owner cases, not proof of live Desktop capture."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from continuity.store import Store, Stale
from continuity.intake import OwnerContext, admit_event
from continuity.inference import Execution


class LinkingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = Store(Path(self.tmp.name)/'data')
        self.s.configure('identities', {'cli:a': 'person', 'cli:b': 'person'})
        self.now = time.time()

    def event(self, eid, text, *, parent=None, role='human', identity='b'):
        msg = dict(id=eid, text=text, role=role)
        if parent: msg['parent'] = parent
        event = admit_event(self.s, OwnerContext.local('cli', identity), msg)
        self.s.ingest(event)
        return event

    def item(self, source='request', title='Reembolso de vuelo', **kw):
        if not self.s.event(source):
            self.event(source, 'Avísame cuando llegue el reembolso del vuelo.', identity='a')
        return self.s.create_item('commitment', 'person', title, source,
                                  self.s.event(source)['text'], **kw)

    def linker(self, proposal=None):
        self.assertIsNotNone(importlib.util.find_spec('continuity.linking'), 'semantic Linker is missing')
        from continuity.linking import Linker
        class Model:
            def complete_structured(me, **kw):
                return SimpleNamespace(parsed=proposal)
        return Linker(self.s, Model(), Execution(self.s))

    def apply_proposed(self, event_id, proposal, *, now):
        # Exercise both public boundaries with an explicit synthetic model.
        linker = self.linker(proposal)
        bound = linker.propose(event_id, now=now)
        return linker.apply(event_id, bound, now=now)

    def proposal(self, event, item, **kw):
        assertion = dict(quote=event['text'], polarity='affirmed', certainty='certain',
                         speaker='self', temporality='current', meaning='completion')
        return dict(disposition='associate', target=item['id'], target_revision=item['revision'],
                    assertion=assertion, evidence_ids=[event['id']], **kw)

    def test_standalone_propose_rejects_target_or_revision_not_in_snapshot(self):
        from continuity.linking import Linker
        for change in ('target', 'revision'):
            with self.subTest(change=change):
                item = self.item('snapshot-'+change)
                event = self.event('snapshot-confirm-'+change, 'Everything is sorted out now.',
                                   parent='snapshot-new-source' if change == 'target' else item['source'])
                class Model:
                    def complete_structured(me, **kwargs):
                        if change == 'target':
                            selected = self.s.create_item('commitment', 'person', 'Inserted in flight', event['parent'], 'q')
                        else:
                            self.s.transition(item['id'], 0, 'waiting')
                            selected = dict(item, revision=1)
                        return SimpleNamespace(parsed=self.proposal(event, selected))
                linker = Linker(self.s, Model(), Execution(self.s))
                with self.assertRaisesRegex(ValueError, 'not supplied'):
                    linker.propose(event['id'], now=time.time())
                self.assertFalse(any(i['state']=='done' for i in self.s.items()))
                with self.s.tx() as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM artifacts').fetchone()[0], 0)

    def test_delayed_apply_rejects_raw_or_tampered_proposal_without_trusted_binding(self):
        from continuity.linking import Linker
        item = self.item()
        event = self.event('delayed-binding', 'Everything is sorted out now.', parent=item['source'])
        raw = self.proposal(event, item)
        with self.assertRaisesRegex(ValueError, 'snapshot'):
            Linker(self.s, None, Execution(self.s)).apply(event['id'], raw, now=time.time())
        linker = self.linker(raw)
        bound = linker.propose(event['id'], now=time.time())
        # Reconstructed JSON and Linker retain the code-owned boundary, not an
        # in-memory capability or model-authored snapshot field.
        bound = json.loads(json.dumps(bound))
        self.s.transition(item['id'], 0, 'waiting')
        tampered = json.loads(json.dumps(bound))
        tampered['target_revision'] = 1
        with self.assertRaisesRegex(ValueError, 'snapshot'):
            Linker(self.s, None, Execution(self.s)).apply(event['id'], tampered, now=time.time())
        with self.assertRaises(Stale):
            Linker(self.s, None, Execution(self.s)).apply(event['id'], bound, now=time.time())
        fresh_event = self.event('bound-fresh', 'Everything is sorted out now.', parent=item['source'])
        fresh = dict(item, revision=1)
        bound = self.linker(self.proposal(fresh_event, fresh)).propose(fresh_event['id'], now=time.time())
        self.assertEqual(Linker(self.s, None, Execution(self.s)).apply(
            fresh_event['id'], json.loads(json.dumps(bound)), now=time.time())['disposition'], 'closed')

    def test_targeted_clarification_cas_and_foreign_principal_are_validated(self):
        item = self.item()
        event = self.event('targeted-clarify', '¿Ya quedó?', parent=item['source'])
        proposal = self.proposal(event, item)
        proposal['disposition'] = 'clarify'
        self.s.transition(item['id'], 0, 'waiting')
        with self.assertRaises(Stale): self.linker().apply(event['id'], proposal, now=time.time())
        foreign = self.s.create_item('commitment', 'other', 'Private', 'foreign', 'q')
        proposal['target'] = foreign['id']
        proposal['target_revision'] = 0
        with self.assertRaises(PermissionError): self.linker().apply(event['id'], proposal, now=time.time())

    def test_failed_provider_attempt_is_shared_and_not_reset_by_new_execution(self):
        from continuity.linking import Linker
        from continuity.inference import InferenceBlocked
        item = self.item()
        event = self.event('provider-failure', 'Ya quedó, gracias', parent=item['source'])
        execution = Execution(self.s)
        with self.s.tx() as c:
            c.execute('UPDATE inference_executions SET max_turns=1 WHERE id=?', (execution.id,))
        execution.limit = 1
        class Fails:
            def complete_structured(me, **kwargs): raise RuntimeError('synthetic provider unavailable')
        with self.assertRaises(RuntimeError): Linker(self.s, Fails(), execution).propose(event['id'], now=time.time())
        self.assertEqual(execution.summary()['attempts'], 1)
        new = Execution(self.s)
        with self.assertRaises(InferenceBlocked): Linker(self.s, Fails(), new).propose(event['id'], now=time.time())
        self.assertEqual(new.summary()['attempts'], 0)

    def test_real_native_messaging_fixture_links_whatsapp_to_telegram_by_exact_reference(self):
        import sqlite3
        from contextlib import closing
        from hermes_state import SessionDB
        from continuity.cli import Controller
        from continuity.host import capture
        from tests.native_fixtures import admit_message
        home = Path(self.tmp.name)/'native-home'
        home.mkdir()
        native = SessionDB(home/'state.db')
        self.addCleanup(native.close)
        native.create_session('channel-a', 'whatsapp', user_id='synthetic-a')
        native.create_session('channel-b', 'telegram', user_id='synthetic-b')
        controller = Controller(home, None)
        controller.store.configure('identities', {'whatsapp:synthetic-a': 'person', 'telegram:synthetic-b': 'person'})
        source = 'native:channel-a:request:human'
        texts = [('channel-a', 'whatsapp', 'synthetic-a', 'request', 'Please track my synthetic refund.'),
                 ('channel-b', 'telegram', 'synthetic-b', 'confirm', 'Sobre '+source+': ya quedó, gracias')]
        for session, surface, sender, turn, text in texts:
            with closing(sqlite3.connect(home/'state.db')) as c, c:
                c.execute("UPDATE sessions SET session_key=?,chat_id=?,chat_type='dm' WHERE id=?", (session, session, session))
                admit_message(c, home, session, text, turn)
            payload = dict(session_id=session, turn_id=turn, user_message=text, platform=surface, sender_id=sender)
            self.assertTrue(capture(controller, 'pre', payload))
        s = controller.store
        item = s.create_item('commitment', 'person', 'Synthetic refund', source, texts[0][-1])
        event_id = 'native:channel-b:confirm:human'
        event = s.event(event_id)
        from continuity.linking import Linker
        class Model:
            def complete_structured(me, **kwargs):
                return SimpleNamespace(parsed=self.proposal(event, item))
        linker = Linker(s, Model(), Execution(s))
        proposal = linker.propose(event_id, now=time.time())
        result = linker.apply(event_id, proposal, now=time.time())
        self.assertEqual(result['disposition'], 'closed')
        self.assertEqual(s.event(source)['surface'], 'whatsapp')
        self.assertEqual(event['surface'], 'telegram')
        self.assertEqual(s.items()[0]['state'], 'done')

    def test_assistant_receipt_and_child_delivery_never_close_parent_goal(self):
        parent = self.item()
        assistant = self.event('assistant-claim', 'I delivered the refund letter.', role='assistant', parent=parent['source'])
        with self.s.tx() as c:
            child = self.s._create_item(c, 'commitment', 'person', 'Refund letter', assistant['id'], assistant['text'],
                                       actor='assistant', parent=parent['id'])
        with self.assertRaisesRegex(ValueError, 'not authorized'):
            self.linker().apply(assistant['id'], self.proposal(assistant, parent), now=time.time())
        with self.s.tx() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM artifacts').fetchone()[0], 0)
        event = self.event('child-confirm', 'Todo resuelto, gracias', parent=child['source'])
        result = self.apply_proposed(event['id'], self.proposal(event, parent), now=time.time())
        self.assertEqual(result['disposition'], 'needs_confirmation')
        self.assertEqual(next(i for i in self.s.items() if i['id']==parent['id'])['state'], 'open')
        result = self.apply_proposed(event['id'], self.proposal(event, child), now=time.time())
        self.assertEqual(result['disposition'], 'closed')
        self.assertEqual(next(i for i in self.s.items() if i['id']==parent['id'])['state'], 'open')

    def test_replay_and_late_candidate_insertion_never_resurrect_or_close_another_goal(self):
        item = self.item()
        event = self.event('replay', 'Ya quedó, gracias', parent=item['source'])
        linker = self.linker(self.proposal(event, item))
        proposal = linker.propose(event['id'], now=time.time())
        # Freshly inserted same-source obligation invalidates uniqueness.
        other = self.s.create_item('commitment', 'person', 'Another goal', item['source'], 'another condition')
        self.assertEqual(linker.apply(event['id'], proposal, now=time.time())['disposition'], 'needs_confirmation')
        self.s.transition(other['id'], 0, 'dismissed')
        self.assertEqual(linker.apply(event['id'], proposal, now=time.time())['disposition'], 'closed')
        with self.assertRaises(Stale): linker.apply(event['id'], proposal, now=time.time())
        third = self.s.create_item('commitment', 'person', 'Later goal', item['source'], 'later condition')
        with self.assertRaisesRegex(ValueError, 'reused confirmation'):
            self.apply_proposed(event['id'], self.proposal(event, third), now=time.time())
        self.assertEqual(next(i for i in self.s.items() if i['id']==third['id'])['state'], 'open')

    def test_mixed_item_title_source_and_reply_references_preserve_conflicts(self):
        for n, (reference, parent) in enumerate([
                ('id', None), ('title', None), ('id', 'selected'),
                ('title', 'selected'), ('id', 'other'), ('title', 'other')]):
            with self.subTest(reference=reference, parent=parent):
                item = self.item('source-alpha-'+str(n), title='Flight refund '+str(n))
                other = self.item('source-beta-'+str(n), title='Hotel refund '+str(n))
                text = 'Regarding '+item['id' if reference == 'id' else 'title']+' and '+other['source']+': everything is sorted out now.'
                event = self.event('mixed-'+str(n), text,
                                   parent=(item if parent == 'selected' else other)['source'] if parent else None)
                proposal = self.proposal(event, item)
                linker = self.linker(proposal)
                proposal = linker.propose(event['id'], now=time.time())
                before = self.s.items()
                result = linker.apply(event['id'], proposal, now=time.time())
                self.assertEqual(result['disposition'], 'needs_confirmation')
                self.assertEqual(self.s.items(), before)
                with self.s.tx() as c:
                    body = json.loads(c.execute('SELECT body FROM artifacts WHERE id=?', (result['clarification_id'],)).fetchone()[0])
                self.assertIn([other['id'], 0], body['candidate_revisions'])

    def test_mixed_own_shared_source_can_be_narrowed_by_exact_item(self):
        item = self.item()
        other = self.s.create_item('commitment', 'person', 'Other condition', item['source'], 'other quote')
        event = self.event('coherent-mixed', 'Regarding '+item['id']+' and '+item['source']+': everything is sorted out now.', parent=item['source'])
        linker = self.linker(self.proposal(event, item))
        proposal = linker.propose(event['id'], now=time.time())
        self.assertEqual(linker.apply(event['id'], proposal, now=time.time())['disposition'], 'closed')
        self.assertEqual(next(i for i in self.s.items() if i['id']==other['id']), other)

    def test_mismatched_reply_source_and_explicit_reference_cannot_authorize(self):
        item = self.item()
        event = self.event('wrong-parent', 'Ya quedó, gracias', parent='foreign-source')
        result = self.apply_proposed(event['id'], self.proposal(event, item), now=time.time())
        self.assertEqual(result['disposition'], 'needs_confirmation')
        other = self.item('different-request', title='Different obligation')
        event = self.event('conflicting-reference', 'Sobre different-request: ya quedó, gracias', parent=item['source'])
        result = self.apply_proposed(event['id'], self.proposal(event, other), now=time.time())
        self.assertEqual(result['disposition'], 'needs_confirmation')

    def test_literal_item_reference_disambiguates_shared_source(self):
        item = self.item()
        self.s.create_item('commitment', 'person', 'Other condition', item['source'], 'other quote')
        event = self.event('literal-item', 'Sobre '+item['id']+': todo resuelto, gracias', parent=item['source'])
        result = self.apply_proposed(event['id'], self.proposal(event, item), now=time.time())
        self.assertEqual(result['disposition'], 'closed')
        self.assertEqual(sum(i['state']=='done' for i in self.s.items()), 1)

    def test_observed_real_model_future_other_assertion_is_response_not_closure(self):
        item = self.item()
        event = self.event('future-regression', 'I will receive the refund tomorrow.', parent=item['source'])
        proposal = self.proposal(event, item)
        proposal['assertion'].update(temporality='future', meaning='other')
        result = self.apply_proposed(event['id'], proposal, now=time.time())
        self.assertEqual(result['disposition'], 'associated')
        self.assertEqual(self.s.items()[0]['state'], 'open')

    def test_propose_bounds_complete_context_and_rechecks_model_consent(self):
        item = self.item()
        event = self.event('bounded', 'Ya quedó, gracias', parent=item['source'])
        calls = []
        class Model:
            def complete_structured(me, **kw):
                calls.append(kw)
                return SimpleNamespace(parsed=self.proposal(event, item))
        from continuity.linking import Linker
        linker = Linker(self.s, Model(), Execution(self.s))
        self.s.configure('allow_model', False)
        with self.assertRaises(ValueError): linker.propose(event['id'], now=time.time())
        self.assertEqual(calls, [])
        self.s.configure('allow_model', True)
        # New admitted event after consent epoch change, huge complete candidate packet.
        event = self.event('oversize', 'Ya quedó, gracias', parent=item['source'])
        for n in range(10): self.item('large'+str(n), title='x'*3000)
        result = linker.propose(event['id'], now=time.time())
        self.assertEqual(result['disposition'], 'clarify')
        self.assertEqual(calls, [])
        self.assertEqual(linker.execution.summary()['attempts'], 0)

    def test_revocation_during_standalone_inference_cannot_retain_snapshot_authority(self):
        from continuity.linking import Linker
        item = self.item()
        event = self.event('revoked-inflight-propose', 'Everything is sorted out now.', parent=item['source'])
        before = self.s.items()
        class Model:
            def complete_structured(me, **kwargs):
                self.s.configure('identities', {'cli:a': 'person'})
                return SimpleNamespace(parsed=self.proposal(event, item))
        linker = Linker(self.s, Model(), Execution(self.s))
        with self.assertRaisesRegex(ValueError, 'not authorized'):
            linker.propose(event['id'], now=time.time())
        self.assertEqual(self.s.items(), before)
        with self.s.tx() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM inference_blocks WHERE reason='link_snapshot_validated'").fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT count(*) FROM artifacts').fetchone()[0], 0)

    def test_publication_authority_failure_never_creates_actionable_artifacts(self):
        for fault in ('revoked_mapping', 'old_epoch', 'expired_receipt', 'paused'):
            for decision in ('completion', 'association', 'targeted_clarify', 'null_clarify', 'negative'):
                with self.subTest(fault=fault, decision=decision):
                    self.s.configure('identities', {'cli:a': 'person', 'cli:b': 'person'})
                    self.s.pause(False)
                    suffix = fault+'-'+decision
                    item = self.item('authority-request-'+suffix)
                    event = self.event('authority-confirm-'+suffix, 'Everything is sorted out now.', parent=item['source'])
                    proposal = self.proposal(event, item)
                    if decision == 'association': proposal['assertion']['meaning'] = 'other'
                    if decision == 'negative': proposal['assertion']['polarity'] = 'negated'
                    if 'clarify' in decision: proposal['disposition'] = 'clarify'
                    if decision == 'null_clarify': proposal.update(target=None, target_revision=None)
                    linker = self.linker(proposal)
                    bound = linker.propose(event['id'], now=time.time())
                    self.assertEqual(bound, proposal)
                    if fault == 'revoked_mapping': self.s.configure('identities', {'cli:a': 'person'})
                    if fault == 'old_epoch':
                        self.s.pause(True)
                        self.s.pause(False)  # current mapping/enabled, obsolete causal epoch
                    if fault == 'paused': self.s.pause(True)
                    now = time.time()+86401 if fault == 'expired_receipt' else time.time()
                    before = self.s.items()
                    with self.s.tx() as c:
                        artifacts = [tuple(r) for r in c.execute('SELECT * FROM artifacts ORDER BY id')]
                    with self.assertRaisesRegex(ValueError, 'not authorized'):
                        linker.apply(event['id'], bound, now=now)
                    self.assertEqual(self.s.items(), before)
                    with self.s.tx() as c:
                        self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM artifacts ORDER BY id')], artifacts)

    def test_worker_scheduling_clock_never_substitutes_receipt_wall_clock(self):
        from continuity.extraction import Worker
        for expired in (False, True):
            with self.subTest(expired=expired):
                item = self.item('separate-clock-request-'+str(expired))
                with self.s.tx() as c: c.execute("UPDATE jobs SET state='done'")
                event = self.event('separate-clock-confirm-'+str(expired), 'Everything is sorted out now.', parent=item['source'])
                if expired:
                    with self.s.tx() as c:
                        c.execute('UPDATE event_auth SET received=0 WHERE id=?', (event['id'],))
                before = self.s.items()
                with self.s.tx() as c:
                    artifacts = [tuple(r) for r in c.execute('SELECT * FROM artifacts ORDER BY id')]
                class Model:
                    def complete_structured(me, **kwargs):
                        return SimpleNamespace(parsed=dict(proposals=[dict(kind='pending_close', owner='human',
                            title=item['title'], source=event['id'], quote=event['text'], target=item['id'],
                            target_revision=0, closure='explicit', assertion=self.proposal(event, item)['assertion'])]))
                report = Worker(self.s, Model(), clock=lambda:100).tick(100)
                if expired:
                    self.assertEqual(self.s.items(), before)
                    self.assertEqual(report['processed'], 0)
                    with self.s.tx() as c:
                        self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM artifacts ORDER BY id')], artifacts)
                else:
                    self.assertEqual(report['processed'], 1)
                    self.assertEqual(next(i for i in self.s.items() if i['id']==item['id'])['state'], 'done')

    def test_worker_obsolete_causal_authority_cannot_publish_clarification_or_waiting(self):
        from continuity.extraction import Worker
        for fault in ('revoked_mapping', 'old_epoch', 'expired_receipt', 'paused_inflight'):
            with self.subTest(fault=fault):
                self.s.configure('identities', {'cli:a': 'person', 'cli:b': 'person'})
                self.s.pause(False)
                item = self.item('worker-authority-request-'+fault)
                with self.s.tx() as c: c.execute("UPDATE jobs SET state='done'")
                event = self.event('worker-authority-confirm-'+fault, 'Everything is sorted out now.', parent=item['source'])
                if fault == 'revoked_mapping': self.s.configure('identities', {'cli:a': 'person'})
                if fault == 'old_epoch':
                    self.s.pause(True)
                    self.s.pause(False)
                if fault == 'expired_receipt':
                    with self.s.tx() as c:
                        c.execute('UPDATE event_auth SET received=? WHERE id=?', (time.time()-86401, event['id']))
                before = self.s.items()
                with self.s.tx() as c:
                    artifacts = [tuple(r) for r in c.execute('SELECT * FROM artifacts ORDER BY id')]
                class Model:
                    def complete_structured(me, **kwargs):
                        if fault == 'paused_inflight': self.s.pause(True)
                        return SimpleNamespace(parsed=dict(proposals=[dict(kind='pending_close', owner='human',
                            title=item['title'], source=event['id'], quote=event['text'], target=item['id'],
                            target_revision=0, closure='explicit', assertion=self.proposal(event, item)['assertion'])]))
                report = Worker(self.s, Model(), clock=time.time).tick(time.time())
                self.assertEqual(report['processed'], 0)
                self.assertGreater(report['blocked']+report['stale'], 0)
                self.assertEqual(self.s.items(), before)
                with self.s.tx() as c:
                    self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM artifacts ORDER BY id')], artifacts)

    def test_worker_ambiguous_label_cannot_be_promoted_by_positive_assertion(self):
        from continuity.extraction import Worker
        item = self.item()
        with self.s.tx() as c: c.execute("UPDATE jobs SET state='done'")
        event = self.event('uncertain-worker', 'Ya quedó, gracias', parent=item['source'])
        class Model:
            def complete_structured(me, **kw):
                return SimpleNamespace(parsed=dict(proposals=[dict(kind='pending_close', owner='human',
                    title=item['title'], source=event['id'], quote=event['text'], target=item['id'],
                    target_revision=0, closure='ambiguous', assertion=self.proposal(event, item)['assertion'])]))
        Worker(self.s, Model(), clock=time.time).tick(time.time())
        self.assertEqual(self.s.items()[0]['state'], 'waiting')

    def test_worker_rejects_target_not_supplied_in_inference_candidate_snapshot(self):
        from continuity.extraction import Worker
        self.item()
        with self.s.tx() as c: c.execute("UPDATE jobs SET state='done'")
        event = self.event('invented-worker-target', 'Ya quedó, gracias', parent='unsupplied-source')
        created = []
        class Model:
            def complete_structured(me, **kw):
                item = self.s.create_item('commitment', 'person', 'New during inference', 'unsupplied-source', 'q')
                created.append(item)
                return SimpleNamespace(parsed=dict(proposals=[dict(kind='pending_close', owner='human',
                    title=item['title'], source=event['id'], quote=event['text'], target=item['id'],
                    target_revision=0, closure='explicit', assertion=self.proposal(event, item)['assertion'])]))
        report = Worker(self.s, Model(), clock=time.time).tick(time.time())
        self.assertEqual(report['processed'], 0)
        self.assertEqual(report['blocked'], 1)
        self.assertEqual(next(i for i in self.s.items() if i['id']==created[0]['id'])['state'], 'open')

    def test_worker_uses_linker_and_full_live_candidate_set_without_extra_model_turn(self):
        from continuity.extraction import Worker
        item = self.item()
        for n in range(22): self.item('worker-other'+str(n), title='Other task '+str(n))
        with self.s.tx() as c: c.execute("UPDATE jobs SET state='done'")
        event = self.event('worker-confirm', 'Todo resuelto, muchas gracias', parent=item['source'])
        calls = []
        class Model:
            def complete_structured(me, **kw):
                packet = json.loads(kw['input'][0]['text'])
                calls.append(packet)
                return SimpleNamespace(parsed=dict(proposals=[dict(kind='pending_close', owner='human',
                    title=item['title'], source=event['id'], quote=event['text'], target=item['id'],
                    target_revision=0, closure='explicit', assertion=self.proposal(event, item)['assertion'])]))
        report = Worker(self.s, Model(), clock=time.time).tick(time.time())
        self.assertEqual(report['processed'], 1)
        self.assertEqual(report['inference']['attempts'], 1)
        self.assertEqual(len(calls[0]['related']), 23)
        from continuity.lifecycle import Lifecycle
        self.assertEqual(Lifecycle(self.s).snapshot(item['id'], 'person')['result_state'], 'confirmed')
        self.assertEqual(sum(i['state']=='done' for i in self.s.items()), 1)

    def test_exact_literal_reference_selects_target_beyond_first_page(self):
        self.item('reference-source')
        for n in range(52): self.item('other-source'+str(n))
        target = max(self.s.items(), key=lambda i: i['id'])
        event = self.event('exact-reference', 'Sobre '+target['source']+': ya quedó, gracias')
        linker = self.linker()
        first = linker.candidates(event['id'], after=None, limit=50)
        self.assertNotIn(target['id'], [i['id'] for i in first['items']])
        linker = self.linker(self.proposal(event, target))
        proposal = linker.propose(event['id'], now=time.time())
        result = linker.apply(event['id'], proposal, now=time.time())
        self.assertEqual(result['disposition'], 'closed')
        self.assertEqual(sum(i['state']=='done' for i in self.s.items()), 1)

    def test_unrelated_and_noncompletion_association_have_no_closure(self):
        item = self.item()
        event = self.event('unrelated', 'El jardín está precioso.', parent=item['source'])
        p = self.proposal(event, item)
        p['disposition'] = 'unrelated'
        result = self.apply_proposed(event['id'], p, now=time.time())
        self.assertEqual(result['disposition'], 'unrelated')
        event = self.event('association', 'Quiero revisar este objetivo contigo.', parent=item['source'])
        p = self.proposal(event, item)
        p['assertion']['meaning'] = 'other'
        result = self.apply_proposed(event['id'], p, now=time.time())
        self.assertEqual(result['disposition'], 'associated')
        self.assertEqual(self.s.items()[0]['state'], 'open')

    def test_forged_evidence_cas_revocation_and_tombstones(self):
        item = self.item()
        event = self.event('scope', 'Ya quedó, gracias', parent=item['source'])
        p = self.proposal(event, item)
        p['evidence_ids'] = ['request']
        with self.assertRaises(ValueError): self.linker().apply(event['id'], p, now=time.time())
        p = self.proposal(event, item)
        self.s.transition(item['id'], 0, 'waiting')
        with self.assertRaises(Stale): self.linker().apply(event['id'], p, now=time.time())
        p['target_revision'] = 1
        self.s.configure('identities', {'cli:a': 'person'})
        with self.assertRaisesRegex(ValueError, 'not authorized'):
            self.linker().apply(event['id'], p, now=time.time())
        with self.s.tx() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM artifacts').fetchone()[0], 0)
        self.s.transition(item['id'], 1, 'dismissed')
        self.s.configure('identities', {'cli:a': 'person', 'cli:b': 'person'})
        fresh = self.event('tombstone-confirm', 'Ya quedó, gracias', parent=item['source'])
        p = self.proposal(fresh, dict(item, revision=2))
        with self.assertRaisesRegex(ValueError, 'tombstone'):
            self.linker().apply(fresh['id'], p, now=time.time())
        self.assertEqual(self.s.items()[0]['state'], 'dismissed')

    def test_complete_candidates_and_ambiguity_dedupe_include_off_page_revisions(self):
        item = self.item()
        for n in range(55): self.item('similar'+str(n))
        self.s.create_item('opportunity', 'person', 'Not an obligation', 'casual', 'q')
        self.s.create_item('commitment', 'other', 'Private', 'foreign', 'q')
        event = self.event('ambiguous', 'Ya quedó, gracias')
        linker = self.linker()
        first = linker.candidates(event['id'], after=None, limit=50)
        self.assertEqual(len(first['items']), 50)
        self.assertTrue(first['has_more'])
        last = linker.candidates(event['id'], after=first['next_cursor'])
        self.assertEqual(len(last['items']), 6)
        self.assertFalse(last['has_more'])
        ids = [i['id'] for i in first['items']+last['items']]
        self.assertEqual(len(set(ids)), 56)
        p = self.proposal(event, item)
        linker = self.linker(p)
        p = linker.propose(event['id'], now=time.time())
        a = linker.apply(event['id'], p, now=time.time())
        b = self.linker().apply(event['id'], p, now=time.time())
        self.assertEqual(a['disposition'], 'needs_confirmation')
        self.assertTrue(a['clarification_created'])
        self.assertFalse(b['clarification_created'])
        self.assertEqual(a['clarification_id'], b['clarification_id'])
        with self.s.tx() as c:
            body = json.loads(c.execute('SELECT body FROM artifacts WHERE id=?', (a['clarification_id'],)).fetchone()[0])
            self.assertEqual(body['v'], 1)
        offpage = last['items'][-1]
        self.s.transition(offpage['id'], offpage['revision'], 'waiting')
        again = linker.apply(event['id'], p, now=time.time())
        self.assertTrue(again['clarification_created'])
        self.assertNotEqual(again['clarification_id'], a['clarification_id'])
        self.assertFalse(any(i['state']=='done' for i in self.s.items()))
        with self.assertRaises(ValueError):
            linker.candidates('request', after=first['next_cursor'])

    def test_direct_reply_to_shared_source_does_not_select_one_of_two_goals(self):
        item = self.item()
        self.s.create_item('commitment', 'person', 'Another condition', item['source'], 'different goal')
        event = self.event('shared', 'Ya quedó, gracias', parent=item['source'])
        got = self.apply_proposed(event['id'], self.proposal(event, item), now=time.time())
        self.assertEqual(got['disposition'], 'needs_confirmation')
        self.assertFalse(any(i['state']=='done' for i in self.s.items()))

    def test_raw_positive_flags_never_override_negative_assertion_scope(self):
        cases = ['No quedó.', 'Me dijeron “ya quedó”.', 'Mañana quedará listo.',
                 'Quizá ya quedó, no sé.', 'I will finish it tomorrow.',
                 'Someone said it was done.', 'I think it is done.',
                 'The transport accepted the message.', 'I remember finishing it last year.',
                 'The draft was delivered, not the refund.', 'Ya quedó. Pero no lo he comprobado.',
                 "I didn't finish it.", "I haven't received it.", 'I haven’t received it.',
                 'Nunca quedó.', 'Jamás recibí el reembolso.', 'Lo terminaré.',
                 'It should be done.', 'Me informaron que ya quedó.']
        for n, text in enumerate(cases):
            with self.subTest(text=text):
                item = self.item('negative-request'+str(n))
                event = self.event('negative'+str(n), text, parent=item['source'])
                result = self.apply_proposed(event['id'], self.proposal(event, item), now=time.time())
                self.assertEqual(result['disposition'], 'needs_confirmation')
                self.assertEqual(next(i for i in self.s.items() if i['id']==item['id'])['state'], 'open')

    def test_cross_surface_exact_reply_semantic_confirmation_closes_only_target(self):
        item = self.item()
        other = self.item('second', 'Reembolso de hotel')
        event = self.event('confirmation', 'Ya quedó, gracias', parent=item['source'])
        proposal = self.proposal(event, item)
        linker = self.linker(proposal)
        before = self.s.items()
        got = linker.propose(event['id'], now=time.time())
        self.assertEqual(got['target'], item['id'])
        self.assertEqual(self.s.items(), before, 'propose has no lifecycle effects')
        result = linker.apply(event['id'], got, now=time.time())
        self.assertEqual(result['disposition'], 'closed')
        from continuity.lifecycle import Lifecycle
        self.assertEqual(Lifecycle(self.s).snapshot(item['id'], 'person')['result_state'], 'confirmed')
        self.assertEqual(Lifecycle(self.s).snapshot(other['id'], 'person')['stage'], 'open')
        self.assertEqual(linker.execution.summary()['attempts'], 1)


@unittest.skipUnless(os.environ.get('CONTINUITY_LINKING_REAL_MODEL') == '1', 'real provider opt-in; fixtures are not semantic acceptance')
class RealModelLinkingTests(unittest.TestCase):
    setUp = LinkingTests.setUp
    event = LinkingTests.event
    item = LinkingTests.item

    def test_real_model_worker_synthetic_cases(self):
        from hermes_cli.plugins import PluginContext, PluginManifest
        from continuity.extraction import Worker
        model = PluginContext(PluginManifest(name='hermes-continuity'), None).llm
        rows = []
        for n, (text, parent, expected) in enumerate([
            ('Ya quedó, gracias', True, 'done'),
            ('Regarding worker-model-request-1: everything is sorted out now, thank you.', False, 'done'),
            ('No quedó. Todavía no recibo el reembolso.', True, 'waiting')]):
            for item in self.s.items():
                if item['state'] not in ('done', 'dismissed'): self.s.transition(item['id'], item['revision'], 'dismissed')
            item = self.item('worker-model-request-'+str(n), title='Synthetic refund '+str(n))
            self.item('worker-model-other-'+str(n), title='Similar synthetic refund '+str(n))
            with self.s.tx() as c: c.execute("UPDATE jobs SET state='done'")
            event = self.event('worker-model-confirm-'+str(n), text, parent=item['source'] if parent else None)
            report = Worker(self.s, model, clock=time.time).tick(time.time())
            fresh = next(i for i in self.s.items() if i['id']==item['id'])
            row = dict(case=n, text=text, expected=expected, state=fresh['state'], report=report)
            rows.append(row)
            print('REAL_MODEL_WORKER '+json.dumps(row, ensure_ascii=False), flush=True)
        output = os.environ.get('CONTINUITY_LINKING_MODEL_REPORT')
        if output: Path(output+'.worker.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2)+'\n')
        self.assertEqual([r['state'] for r in rows], [r['expected'] for r in rows])

    def test_real_model_synthetic_cases(self):
        from hermes_cli.plugins import PluginContext, PluginManifest
        from continuity.linking import Linker
        # Host-owned facade, actual configured provider/auth; no policy override,
        # injected caller, plugin loading, production DB, sends or profile mutation.
        host_model = PluginContext(PluginManifest(name='hermes-continuity'), None).llm
        routes = []
        class RecordingModel:
            def complete_structured(me, **kwargs):
                result = host_model.complete_structured(**kwargs)
                routes.append(dict(provider=result.provider, model=result.model))
                return result
        model = RecordingModel()
        execution = Execution(self.s)
        cases = json.loads((Path(__file__).parent/'model_cases'/'linking.json').read_text())['cases']
        outcomes = []
        failures = []
        for case in cases:
            for item in self.s.items():
                if item['state'] not in ('done', 'dismissed'):
                    self.s.transition(item['id'], item['revision'], 'dismissed')
            target = self.item('model-request-'+case['id'], title='Synthetic flight refund '+case['id'])
            if case.get('similar'):
                self.item('model-second-'+case['id'], title=target['title'])
            event = self.event('model-confirm-'+case['id'], case['text'],
                               parent=target['source'] if case.get('reply') else None)
            row = dict(case=case['id'], expected_proposal=case['proposal'], expected_apply=case['apply'])
            try:
                linker = Linker(self.s, model, execution)
                proposal = linker.propose(event['id'], now=time.time())
                result = linker.apply(event['id'], proposal, now=time.time())
                row.update(route=routes[-1], proposal=proposal, result=result,
                           closed=[i['id'] for i in self.s.items() if i['state']=='done' and i['source'].endswith(case['id'])])
                if proposal['disposition'] != case['proposal'] or result['disposition'] != case['apply']:
                    failures.append(case['id'])
                if case['apply'] == 'closed' and row['closed'] != [target['id']]: failures.append(case['id']+':target')
                if case['apply'] != 'closed' and row['closed']: failures.append(case['id']+':unsafe-close')
            except Exception as exc:
                row['error'] = type(exc).__name__+': '+str(exc)
                failures.append(case['id'])
            outcomes.append(row)
            print('REAL_MODEL_CASE '+json.dumps(row, ensure_ascii=False), flush=True)
            if row.get('error'): break  # bounded fail-fast on provider failure
        print('REAL_MODEL_EXECUTION '+json.dumps(execution.summary()), flush=True)
        output = os.environ.get('CONTINUITY_LINKING_MODEL_REPORT')
        if output:
            Path(output).write_text(json.dumps(dict(outcomes=outcomes, failures=failures,
                                                   execution=execution.summary()), ensure_ascii=False, indent=2)+'\n')
        self.assertEqual(len(outcomes), len(cases), 'provider blocked remaining cases; no simulated results')
        self.assertEqual(failures, [])
