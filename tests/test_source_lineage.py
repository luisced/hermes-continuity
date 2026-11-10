"""Synthetic v32 provenance fixtures; never reads the active profile."""
import contextlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from continuity.cli import Controller
from continuity.host import capture
from continuity.source import NativeSource


class SourceLineageTests(unittest.TestCase):
    def setUp(self):
        self.setup_source()
        from tests.native_fixtures import admit_message
        with self.db() as c:
            admit_message(c, self.home, "reset-two", self.payload["user_message"], "turn-one", sender="live@pn")

    def setup_source(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.controller = Controller(self.home, None)
        self.identities = {'whatsapp:stored@lid': 'person', 'whatsapp:live@pn': 'person'}
        self.controller.store.configure('identities', self.identities)
        with self.db() as c:
            c.executescript('''CREATE TABLE schema_version(version INTEGER);
                INSERT INTO schema_version VALUES(32);
                CREATE TABLE state_meta(key TEXT,value TEXT);
                CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,created_source TEXT,
                parent_session_id TEXT,user_id TEXT,model_config TEXT,session_key TEXT,
                chat_id TEXT,chat_type TEXT,thread_id TEXT,profile_name TEXT,origin_json TEXT,
                end_reason TEXT,transport_profile TEXT);
            ''')
            for sid, parent in [('root', None), ('reset-one', 'root'), ('reset-two', 'reset-one')]:
                c.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          (sid, 'whatsapp', 'whatsapp' if parent else None, parent, 'stored@lid',
                           json.dumps({'_reset_from': parent}) if parent else None,
                           'chat-route', 'chat-token', 'dm', None, 'fixture-profile',
                           json.dumps({'platform': 'whatsapp', 'chat_id': 'chat-token', 'user_id': 'stored@lid'}),
                           'session_reset' if sid == 'reset-one' else None, None))
        self.payload = dict(session_id='reset-two', turn_id='turn-one', user_message='Please prepare a brief.',
                            sender_id='live@pn', platform='whatsapp')

    @contextlib.contextmanager
    def db(self):
        with contextlib.closing(sqlite3.connect(self.home / 'state.db')) as c, c:
            yield c

    def test_canonical_reset_alias_admitted_by_native_hook(self):
        self.assertTrue(capture(self.controller, 'pre', dict(self.payload, parent_session_id='reset-one')))
        event = self.controller.store.event('native:reset-two:turn-one:human')
        self.assertEqual(event['principal'], 'person')
        self.assertTrue(capture(self.controller, 'post', dict(self.payload, assistant_response='I will prepare it.')))


    def test_registered_public_hooks_admit_reset_turn(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from continuity.host import register
        hooks = {}
        ctx = SimpleNamespace(register_hook=lambda name, callback: hooks.update({name: callback}),
                              register_cli_command=lambda *args: None,
                              get_config=lambda name, default: name == 'collect_references')
        with patch('hermes_constants.get_hermes_home', return_value=self.home):
            register(ctx)
        self.assertIsNone(hooks['pre_llm_call'](**self.payload))
        self.assertIsNotNone(self.controller.store.event('native:reset-two:turn-one:human'))
        self.assertIsNone(hooks['post_llm_call'](**dict(self.payload, assistant_response='I will prepare it.')))
        self.assertIsNotNone(self.controller.store.event('native:reset-two:turn-one:assistant'))

    def test_alias_revoked_during_resolution_is_fenced(self):
        from unittest.mock import patch
        original = NativeSource.resolve
        def revoke(resolver, payload, identities=None):
            result = original(resolver, payload, identities)
            self.controller.store.configure('identities', {'whatsapp:stored@lid': 'person'})
            return result
        with patch.object(NativeSource, 'resolve', revoke):
            self.assertFalse(capture(self.controller, 'pre', self.payload))
        self.assertFalse(self.controller.store.jobs())

    def test_identity_rebound_during_admission_is_fenced(self):
        from unittest.mock import patch
        from continuity.intake import admit_event
        def rebind(store, context, message):
            store.configure('identities', dict.fromkeys(self.identities, 'other-person'))
            return admit_event(store, context, message)
        with patch('continuity.host.admit_event', rebind):
            self.assertFalse(capture(self.controller, 'pre', self.payload))
        self.assertFalse(self.controller.store.jobs())

    def test_missing_cyclic_and_overlong_lineage_rejected(self):
        with self.db() as c:
            c.execute("DELETE FROM sessions WHERE id='root'")
        with self.assertRaises(ValueError):
            capture(self.controller, 'pre', self.payload)
        with self.db() as c:
            c.execute('UPDATE sessions SET parent_session_id=?,model_config=?,end_reason=? WHERE id=?',
                      ('reset-two', json.dumps({'_reset_from': 'reset-two'}), 'session_reset', 'reset-one'))
            c.execute("UPDATE sessions SET end_reason='session_reset' WHERE id='reset-two'")
        with self.assertRaises(ValueError):
            capture(self.controller, 'pre', self.payload)
        with self.db() as c:
            for n in range(65):
                sid = 'segment-' + str(n)
                parent = 'segment-' + str(n + 1) if n < 64 else None
                c.execute('INSERT INTO sessions SELECT ?,source,created_source,?,user_id,?,session_key,chat_id,chat_type,thread_id,profile_name,origin_json,?,transport_profile FROM sessions WHERE id=?',
                          (sid, parent, json.dumps({'_reset_from': parent}) if parent else '{}', 'session_reset', 'reset-two'))
        with self.assertRaises(ValueError):
            capture(self.controller, 'pre', dict(self.payload, session_id='segment-0'))
        self.assertFalse(self.controller.store.jobs())

    def test_foreign_origin_metadata_rejected(self):
        with self.db() as c:
            c.execute('UPDATE sessions SET origin_json=? WHERE id=?',
                      (json.dumps({'platform': 'whatsapp', 'chat_id': 'foreign-chat', 'user_id': 'stored@lid'}), 'reset-one'))
        with self.assertRaises(ValueError):
            capture(self.controller, 'pre', self.payload)

    def test_reset_chain_rejects_foreign_rows_and_noncanonical_markers(self):
        mutations = [
            ('model_config', json.dumps({'_delegate_from': 'reset-one', '_reset_from': 'reset-one'})),
            ('model_config', json.dumps({'_branched_from': 'reset-one', '_reset_from': 'reset-one'})),
            ('model_config', '{}'), ('model_config', '{bad'), ('model_config', '[]'),
            ('model_config', json.dumps({'_reset_from': 'other-parent'})),
            ('user_id', 'foreign-owner'), ('session_key', 'foreign-route'),
            ('chat_id', 'foreign-chat'), ('thread_id', 'foreign-thread'),
            ('profile_name', 'foreign-profile'), ('source', 'cron'), ('created_source', 'unknown'),
        ]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                with self.db() as c:
                    original = c.execute('SELECT '+key+' FROM sessions WHERE id=?', ('reset-two',)).fetchone()[0]
                    c.execute('UPDATE sessions SET '+key+'=? WHERE id=?', (value, 'reset-two'))
                with self.assertRaises(ValueError):
                    capture(self.controller, 'pre', self.payload)
                with self.db() as c:
                    c.execute('UPDATE sessions SET '+key+'=? WHERE id=?', (original, 'reset-two'))
        self.assertFalse(self.controller.store.jobs())

    def test_explicit_aliases_required_on_same_surface(self):
        for aliases in ({}, {'whatsapp:stored@lid': 'person'},
                        {'whatsapp:stored@lid': 'person', 'whatsapp:live@pn': 'other'},
                        {'whatsapp:stored@lid': 'person', 'telegram:live@pn': 'person'}):
            with self.subTest(aliases=aliases), self.assertRaises(ValueError):
                NativeSource(self.home).resolve(self.payload, identities=aliases)
        with self.assertRaises(ValueError):
            NativeSource(self.home).resolve(dict(self.payload, identities=self.identities))
        self.assertEqual(NativeSource(self.home).resolve(self.payload, self.identities), ('whatsapp', 'stored@lid'))

    def test_forged_hook_lineage_cannot_admit_unknown_delegate_or_cron(self):
        for sid in ('unknown', 'delegate', 'cron'):
            with self.db() as c:
                if sid != 'unknown':
                    c.execute('INSERT INTO sessions(id,source,created_source,parent_session_id,user_id,model_config) VALUES(?,?,?,?,?,?)',
                              (sid, 'cron' if sid == 'cron' else 'whatsapp', 'whatsapp', 'root', 'stored@lid', '{}'))
            forged = dict(self.payload, session_id=sid, producer='human', _reset_from='root', source='whatsapp')
            with self.assertRaises(ValueError):
                capture(self.controller, 'pre', forged)
        with self.assertRaises(ValueError):
            capture(self.controller, 'pre', dict(self.payload, parent_session_id='foreign-parent'))
        self.assertFalse(self.controller.store.jobs())


if __name__ == '__main__':
    unittest.main()
