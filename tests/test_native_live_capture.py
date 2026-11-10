"""Native host-contract regressions; all identities and transcripts are synthetic."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from tests import test_native_messaging as messaging
from continuity.source import NativeSource
from continuity.host import capture, register


class NativeLiveCaptureTests(unittest.TestCase):
    setUp = messaging.NativeMessagingTests.setUp
    db = messaging.NativeMessagingTests.db
    admission = messaging.NativeMessagingTests.admission

    def test_real_turn_context_reset_parent_is_not_delegation(self):
        from agent.turn_context import _collect_pre_llm_call_context
        self.admission()
        hooks = {}
        ctx = SimpleNamespace(register_hook=lambda name, cb: hooks.update({name: cb}),
                              register_cli_command=lambda *a: None,
                              get_config=lambda key, default: key == 'collect_references')
        with patch('hermes_constants.get_hermes_home', return_value=self.home):
            register(ctx)
        agent = SimpleNamespace(session_id='reset-two', _parent_session_id='reset-one',
                                _user_id='live@pn', platform='whatsapp', model='fixture')
        def dispatch(name, **payload):
            hooks[name](**payload)
            return []
        with patch('hermes_cli.lifecycle.invoke_hook', side_effect=dispatch):
            self.assertEqual(_collect_pre_llm_call_context(
                agent, effective_task_id='reset-two', turn_id='native-turn',
                original_user_message=self.payload['user_message'],
                messages=[{'role': 'user', 'content': self.payload['user_message'],
                           'platform_message_id': 'platform-one'}], conversation_history=[]), '')
        self.assertIsNotNone(self.controller.store.event('native:reset-two:native-turn:human'))

    def test_real_post_hook_without_sender_still_uses_configured_aliases(self):
        import logging
        from agent.turn_finalizer import _apply_output_hooks
        self.admission()
        self.assertTrue(capture(self.controller, 'pre', self.payload))
        hooks = {}
        ctx = SimpleNamespace(register_hook=lambda name, cb: hooks.update({name: cb}),
                              register_cli_command=lambda *a: None,
                              get_config=lambda key, default: key == 'collect_references')
        with patch('hermes_constants.get_hermes_home', return_value=self.home):
            register(ctx)
        def dispatch(name, **payload):
            hooks[name](**payload)
            return []
        with patch('hermes_cli.lifecycle.invoke_hook', side_effect=dispatch), \
             patch('agent.turn_finalizer.apply_llm_output_transform', return_value=('I will prepare it.', False, None)), \
             patch('agent.side_question_lease.issue_side_question_token', return_value=None):
            _apply_output_hooks(SimpleNamespace(session_id='reset-two', model='fixture'), 'I will prepare it.',
                                logging.getLogger(__name__), platform='whatsapp', effective_task_id='reset-two',
                                turn_id='turn-one', original_user_message=self.payload['user_message'], messages=[])
        self.assertIsNotNone(self.controller.store.event('native:reset-two:turn-one:assistant'))

    def test_recent_history_uses_terminal_digest_bound_admissions_only(self):
        self.admission()
        with self.db() as c:
            c.execute("UPDATE session_admissions SET status='terminal'")
            c.execute("INSERT INTO messages VALUES(?,?,?,?,?)", ('reset-two', 'user', 'unadmitted text', 'foreign', '{}'))
        rows = NativeSource(self.home).recent('reset-two', self.identities, principal='person', limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['text'], self.payload['user_message'])
        self.assertEqual(rows[0]['platform_message_id'], 'platform-one')
        self.assertEqual(rows[0]['admission_id'], 'admission-one')
        self.assertEqual(rows[0]['status'], 'terminal')
        self.assertEqual(rows[0]['principal'], 'person')
        with self.assertRaises(ValueError):
            NativeSource(self.home).recent('reset-two', self.identities, principal='foreign')
        with self.db() as c:
            c.execute("UPDATE session_admissions SET payload_digest='broken'")
        self.assertEqual(NativeSource(self.home).recent('reset-two', self.identities, principal='person'), [])

    def test_recent_history_cannot_reuse_other_session_admission(self):
        self.admission()
        with self.db() as c:
            c.execute("UPDATE session_admissions SET target_session_id='reset-one'")
        self.assertEqual(NativeSource(self.home).recent('reset-two', self.identities, principal='person'), [])

    def test_history_facade_is_read_only_and_epoch_fenced(self):
        from continuity.host import recent_history
        self.admission()
        self.assertEqual(len(recent_history(self.controller, 'reset-two', principal='person')), 1)
        self.assertIsNone(self.controller.store.event('native:reset-two:turn-one:human'))
        original = NativeSource.recent
        def revoke(source, *args, **kwargs):
            rows = original(source, *args, **kwargs)
            self.controller.store.pause(True)
            return rows
        with patch.object(NativeSource, 'recent', revoke):
            self.assertEqual(recent_history(self.controller, 'reset-two', principal='person'), [])

    def test_same_sender_still_requires_explicit_identity_binding(self):
        self.admission()
        with self.db() as c:
            c.execute("UPDATE sessions SET user_id='live@pn'")
            for sid in ('root', 'reset-one', 'reset-two'):
                c.execute('UPDATE sessions SET origin_json=? WHERE id=?',
                          (json.dumps({'platform': 'whatsapp', 'chat_id': 'chat-token', 'user_id': 'live@pn'}), sid))
        with self.assertRaises(ValueError):
            NativeSource(self.home).resolve(self.payload, {})

    def test_registered_rejection_logs_safe_specific_cause(self):
        hooks = {}
        ctx = SimpleNamespace(register_hook=lambda name, cb: hooks.update({name: cb}),
                              register_cli_command=lambda *a: None,
                              get_config=lambda key, default: key == 'collect_references')
        with patch('hermes_constants.get_hermes_home', return_value=self.home):
            register(ctx)
        with self.assertLogs('continuity.host', level='WARNING') as records:
            hooks['pre_llm_call'](**self.payload)
        self.assertIn('native_turn_admission_missing', records.output[0])
        self.assertNotIn(self.payload['user_message'], records.output[0])
        self.assertNotIn('reset-two', records.output[0])

    def test_malformed_history_envelope_is_omitted_not_promoted(self):
        self.admission()
        with self.db() as c:
            c.execute('UPDATE session_admissions SET principal_id=NULL')
        self.assertEqual(NativeSource(self.home).recent('reset-two', self.identities, principal='person'), [])
        with self.db() as c:
            c.execute('UPDATE session_admissions SET principal_id=?',
                      ('messaging:'+json.dumps([None,'whatsapp','chat-token',None,'live@pn'],separators=(',',':')),))
            c.execute("UPDATE messages SET display_metadata='[]'")
        self.assertEqual(NativeSource(self.home).recent('reset-two', self.identities, principal='person'), [])

    def test_capture_epoch_is_fenced_even_after_forget_and_resume(self):
        self.admission()
        original = NativeSource.resolve
        def rotate(source, payload, identities=None):
            resolved = original(source, payload, identities)
            self.controller.store.forget()
            self.controller.store.configure('identities', self.identities)
            self.controller.store.pause(False)
            return resolved
        with patch.object(NativeSource, 'resolve', rotate):
            self.assertFalse(capture(self.controller, 'pre', self.payload))
        self.assertIsNone(self.controller.store.event('native:reset-two:turn-one:human'))

    def test_post_cannot_create_missing_or_different_pre_turn(self):
        self.admission()
        with self.assertRaises(ValueError):
            capture(self.controller, 'post', dict(self.payload, assistant_response='Prepared.'))
        self.assertTrue(capture(self.controller, 'pre', self.payload))
        with self.assertRaises(ValueError):
            capture(self.controller, 'post', dict(self.payload, turn_id='other', assistant_response='Prepared.'))

    def test_history_cursor_is_bounded_and_admission_scoped(self):
        self.admission()
        source = NativeSource(self.home)
        rows = source.recent('reset-two', self.identities, principal='person', limit=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(source.recent('reset-two', self.identities, principal='person', before_seq=rows[0]['seq']), [])
        for limit in (0, 101, True, '10'):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                source.recent('reset-two', self.identities, principal='person', limit=limit)

    def test_admission_identifier_must_be_nonempty(self):
        self.admission()
        with self.db() as c:
            c.execute("UPDATE session_admissions SET admission_id=''")
        with self.assertRaises(ValueError):
            NativeSource(self.home).resolve(self.payload, self.identities)

    def test_missing_admission_is_not_human_session_authority(self):
        with self.assertRaises(ValueError):
            NativeSource(self.home).resolve(self.payload, self.identities)

    def test_terminal_admission_is_history_not_live_authority(self):
        self.admission()
        with self.db() as c:
            c.execute("UPDATE session_admissions SET status='terminal'")
        with self.assertRaises(ValueError):
            NativeSource(self.home).resolve(self.payload, self.identities)

    def test_automation_with_human_envelope_is_rejected(self):
        import hashlib
        self.admission()
        with self.db() as c:
            body = json.loads(c.execute('SELECT payload_json FROM session_admissions').fetchone()[0])
            body['native_text_v1']['automation'] = {'owner': 'reset-two'}
            raw = json.dumps({'target': 'reset-two', 'payload': {'input': body, 'intent': 'queue'}},
                             ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
            c.execute('UPDATE session_admissions SET payload_json=?,payload_digest=?',
                      (json.dumps(body), hashlib.sha256(raw).hexdigest()))
        with self.assertRaises(ValueError):
            NativeSource(self.home).resolve(self.payload, self.identities)

    def test_foreign_hook_parent_is_rejected(self):
        self.admission()
        with self.assertRaises(ValueError):
            NativeSource(self.home).resolve(dict(self.payload, parent_session_id='foreign'), self.identities)

    def test_wrapped_unpersisted_hook_text_is_not_stripped_as_authority(self):
        self.admission()
        with self.assertRaises(ValueError):
            NativeSource(self.home).resolve(dict(self.payload, user_message='[system note]\n' + self.payload['user_message']), self.identities)
