"""Synthetic admission proofs, not live automatic-capture acceptance."""
import importlib
import importlib.util
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from . import test_native_ordering as ordering


class AdmissionTurnTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ordering.NativeOrderingTests(methodName='runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('continuity.admission'),
                             'channel admission contract is missing')
        return importlib.import_module('continuity.admission')

    def test_pre_is_reference_only_post_reproves_exact_persisted_pair(self):
        api = self.api()
        from continuity.host import capture
        proof = api.verify_native_turn(self.fixture.home, self.fixture.payload, self.fixture.identities, phase='pre')
        self.assertEqual(proof['surface'], 'whatsapp')
        self.assertEqual(proof['identity'], 'stored@lid')
        self.assertFalse(proof['persisted'])
        self.assertEqual(set(proof['reference']), {'source', 'binding'})
        self.assertTrue(capture(self.fixture.controller, 'pre', self.fixture.payload))
        human_exists_before_post = self.fixture.controller.store.event(self.fixture.human) is not None
        self.assertFalse(human_exists_before_post)
        self.assertEqual(self.fixture.controller.store.jobs(), [])
        with self.assertRaises(ValueError):
            api.verify_native_turn(self.fixture.home, self.fixture.payload, self.fixture.identities, phase='post')
        self.fixture.persist()
        confirmed = api.verify_native_turn(self.fixture.home, self.fixture.payload, self.fixture.identities, phase='post')
        self.assertTrue(confirmed['persisted'])
        self.assertEqual(confirmed['reference'], proof['reference'])
        self.assertTrue(capture(self.fixture.controller, 'post', dict(self.fixture.payload, assistant_response='Synthetic response.')))
        human = self.fixture.controller.store.event(self.fixture.human)
        assistant = self.fixture.controller.store.event(self.fixture.assistant)
        assert human is not None and assistant is not None
        exact_pair_after_post = (human['text'] == self.fixture.payload['user_message']
            and assistant['text'] == 'Synthetic response.' and assistant['parent'] == self.fixture.human
            and human['principal'] == assistant['principal'] == 'person')
        self.assertTrue(exact_pair_after_post)
        self.assertEqual(len(self.fixture.controller.store.jobs()), 2)

    def test_coverage_is_metadata_only_and_never_treats_configuration_as_acceptance(self):
        api = self.api()
        self.assertTrue(hasattr(api, 'surface_coverage'), 'surface coverage contract is missing')
        import sqlite3
        connect = sqlite3.connect
        queries = []
        def traced(*args, **kwargs):
            c = connect(*args, **kwargs)
            c.set_trace_callback(queries.append)
            return c
        settings = {'identities': dict(self.fixture.identities, **{'gui:owner': 'person'}),
                    'automatic_verified': {'whatsapp': True, 'gui': True}, 'collect_references': True}
        with patch('continuity.source.sqlite3.connect', side_effect=traced):
            coverage = api.surface_coverage(self.fixture.home, settings)
        surfaces = {row['surface']: row for row in coverage}
        self.assertEqual(set(surfaces), {'cli','tui','gui','telegram','discord','slack','whatsapp','signal','matrix','sms','email'})
        for row in coverage:
            self.assertEqual(set(row), {'surface','configured','supported','automatic_verified','reason'})
            self.assertFalse(row['automatic_verified'])
        self.assertEqual(surfaces['gui'], dict(surface='gui', configured=True, supported=False,
            automatic_verified=False, reason='unsupported_native_turn_provenance'))
        self.assertEqual(surfaces['whatsapp'], dict(surface='whatsapp', configured=True, supported=True,
            automatic_verified=False, reason='automatic_capture_not_verified'))
        self.assertEqual(surfaces['telegram']['reason'], 'identity_not_configured')
        # Only schema_version and PRAGMA metadata, never producer/payload rows.
        selects = [q for q in queries if q.lstrip().upper().startswith('SELECT')]
        self.assertEqual(selects, ['SELECT version FROM schema_version LIMIT 1'])

    def test_owner_control_derives_from_registered_operator_transport_not_renderer(self):
        api = self.api()
        self.assertTrue(hasattr(api, 'OwnerControl'), 'authenticated owner-control context is missing')
        from gateway.session_controls import AuthorityConnection
        from gateway.owner_identity import principal_subject
        authority = SimpleNamespace(profile_id=str(self.fixture.home), instance_id='synthetic-instance', events={})
        identity = {'user_id': 'synthetic-operator', 'instance_id': authority.instance_id,
                    'profile_id': str(self.fixture.home)}
        # Real host connection class; the host's bootstrap migration is irrelevant
        # to this request-context unit fixture and must not write any real state.
        with patch('gateway.session_local_migration.bind_native_transport'):
            connection = AuthorityConnection(authority, object(), identity, operator=True)
        principal = principal_subject(identity)
        identities = {'host:' + principal: 'person'}
        control = api.OwnerControl.from_host(connection, self.fixture.home, identities, request_id='request-one')
        self.assertEqual((control.principal, control.profile_scope, control.request_id),
                         ('person', str(self.fixture.home), 'request-one'))
        self.assertEqual(api.require_owner_control(control, self.fixture.home), control)
        with self.assertRaises((ValueError, TypeError)):
            api.OwnerControl(principal='person', profile_scope=str(self.fixture.home), request_id='forged')
        with self.assertRaises(ValueError):
            api.require_owner_control({'principal': 'person', 'profile_scope': str(self.fixture.home), 'request_id': 'forged'}, self.fixture.home)
        with self.assertRaises(ValueError):
            api.require_owner_control(control, self.fixture.home / 'foreign')
        with self.assertRaises(ValueError):
            api.OwnerControl.from_host({'actor': connection.actor}, self.fixture.home, identities, request_id='forged')
        with self.assertRaises(ValueError):
            api.OwnerControl.from_host(connection.actor, self.fixture.home, identities, request_id='forged')
        with self.assertRaises(ValueError):
            api.OwnerControl.from_host(connection, self.fixture.home / 'foreign', identities, request_id='request-one')
        with self.assertRaises(ValueError):
            api.OwnerControl.from_host(connection, self.fixture.home, {}, request_id='request-one')
        for request_id in ('', None, 'x' * 257):
            with self.assertRaises(ValueError):
                api.OwnerControl.from_host(connection, self.fixture.home, identities, request_id=request_id)
        with self.assertRaises((AttributeError, TypeError)):
            control.principal = 'other'
        del authority.events[connection.actor.transport_id]
        with self.assertRaises(ValueError):
            api.OwnerControl.from_host(connection, self.fixture.home, identities, request_id='disconnected')

    def test_owner_control_rejects_unregistered_null_transport_and_unprivileged_actor(self):
        api = self.api()
        from gateway.session_controls import AuthorityConnection
        from gateway.owner_identity import principal_subject
        authority = SimpleNamespace(profile_id=str(self.fixture.home), instance_id='synthetic-instance', events={})
        identity = {'user_id': 'synthetic-operator', 'instance_id': authority.instance_id,
                    'profile_id': str(self.fixture.home), 'capabilities': ['session:operator']}
        with patch('gateway.session_local_migration.bind_native_transport'):
            unprivileged = AuthorityConnection(authority, object(), identity, operator=False)
            disconnected = AuthorityConnection(authority, None, identity, operator=True)
        identities = {'host:' + principal_subject(identity): 'person'}
        del authority.events[disconnected.actor.transport_id]
        for connection in (unprivileged, disconnected):
            with self.subTest(operator=connection._operator), self.assertRaises(ValueError):
                api.OwnerControl.from_host(connection, self.fixture.home, identities, request_id='synthetic-request')

    def test_native_wrapper_preserves_negative_proofs_and_alias_rules(self):
        api = self.api()
        from continuity.host import capture
        original_body = json.loads(json.dumps(self.fixture.body))
        original_identities = dict(self.fixture.identities)
        for mutation in ('bot', 'cron', 'delegate', 'ambiguous-alias', 'altered-payload', 'foreign-profile', 'duplicate-id'):
            with self.subTest(mutation=mutation):
                self.fixture.body = json.loads(json.dumps(original_body))
                identities = dict(original_identities)
                payload = dict(self.fixture.payload)
                if mutation == 'bot':
                    self.fixture.body['native_text_v1']['source']['is_bot'] = True
                elif mutation == 'cron':
                    self.fixture.body['native_text_v1']['automation'] = {'kind': 'cron'}
                elif mutation == 'delegate':
                    with self.fixture.db() as c:
                        c.execute('UPDATE sessions SET model_config=?', (json.dumps({'_delegate_from': 'synthetic-parent'}),))
                elif mutation == 'ambiguous-alias':
                    identities['whatsapp:sender@pn'] = 'other-person'
                elif mutation == 'altered-payload':
                    payload['user_message'] = 'Changed synthetic request.'
                elif mutation == 'foreign-profile':
                    self.fixture.body['native_text_v1']['provenance']['runtime_home'] = str(self.fixture.home / 'other')
                elif mutation == 'duplicate-id':
                    with self.fixture.db() as c:
                        values = ('synthetic-session', 'user', self.fixture.payload['user_message'], 'platform-one',
                                  json.dumps({'gateway_input_owner': 'reservation'}), 100.0)
                        for _ in range(2):
                            c.execute('INSERT INTO messages(session_id,role,content,platform_message_id,display_metadata,timestamp) VALUES(?,?,?,?,?,?)', values)
                with self.fixture.db() as c:
                    c.execute('UPDATE session_admissions SET payload_json=?,payload_digest=?',
                              (json.dumps(self.fixture.body), self.fixture.digest()))
                for phase in ('pre', 'post'):
                    with self.assertRaises(ValueError):
                        api.verify_native_turn(self.fixture.home, payload, identities, phase=phase)
                self.fixture.controller.store.configure('identities', identities)
                with self.assertRaises(ValueError):
                    capture(self.fixture.controller, 'pre', payload)
                self.assertIsNone(self.fixture.controller.store.native_pending(self.fixture.human))
                self.assertIsNone(self.fixture.controller.store.event(self.fixture.human))
                with self.fixture.db() as c:
                    c.execute("UPDATE sessions SET model_config='{}'")
                    c.execute('DELETE FROM messages')

    def test_native_wrapper_does_not_bypass_epoch_revocation_or_missing_pre(self):
        api = self.api()
        from continuity.host import capture
        api.verify_native_turn(self.fixture.home, self.fixture.payload, self.fixture.identities, phase='pre')
        capture(self.fixture.controller, 'pre', self.fixture.payload)
        self.fixture.persist()
        api.verify_native_turn(self.fixture.home, self.fixture.payload, self.fixture.identities, phase='post')
        self.fixture.controller.store.pause(True)
        self.fixture.controller.store.pause(False)
        with self.assertRaises(ValueError):
            capture(self.fixture.controller, 'post', dict(self.fixture.payload, assistant_response='Synthetic response.'))
        self.assertIsNone(self.fixture.controller.store.event(self.fixture.human))
        self.assertIsNone(self.fixture.controller.store.event(self.fixture.assistant))
        self.assertEqual(self.fixture.controller.store.jobs(), [])
        with self.assertRaisesRegex(ValueError, 'unknown native phase'):
            api.verify_native_turn(self.fixture.home, self.fixture.payload, self.fixture.identities, phase='anything')

    def test_coverage_fails_closed_on_schema_changes_absence_and_symlinks(self):
        api = self.api()
        for change in ('version', 'columns', 'symlink', 'missing'):
            with self.subTest(change=change):
                if change == 'version':
                    with self.fixture.db() as c:
                        c.execute('UPDATE schema_version SET version=31')
                elif change == 'columns':
                    with self.fixture.db() as c:
                        c.execute('UPDATE schema_version SET version=32')
                        c.execute('ALTER TABLE messages RENAME COLUMN platform_message_id TO missing_id')
                elif change == 'symlink':
                    self.fixture.native.close()
                    (self.fixture.home / 'state.db').rename(self.fixture.home / 'actual.db')
                    (self.fixture.home / 'state.db').symlink_to(self.fixture.home / 'actual.db')
                else:
                    (self.fixture.home / 'state.db').unlink()
                coverage = api.surface_coverage(self.fixture.home, {'identities': self.fixture.identities})
                self.assertFalse(any(row['supported'] or row['automatic_verified'] for row in coverage))
                expected = 'unsupported_native_schema' if change in ('version', 'columns') else 'unsafe_native_state'
                self.assertEqual(next(row for row in coverage if row['surface'] == 'whatsapp')['reason'], expected)

    def test_desktop_local_turn_protocol_is_explicitly_unsupported(self):
        api = self.api()
        self.fixture.body = {'text': self.fixture.payload['user_message'], 'local_operator_v1': {
            'profile_id': str(self.fixture.home), 'session_id': 'synthetic-session', 'principal_id': 'stored@lid'}}
        with self.fixture.db() as c:
            c.execute("UPDATE sessions SET source='local',created_source='gui'")
            receipt = dict(profile_id=str(self.fixture.home), session_id='synthetic-session',
                           entry={'session_id': 'synthetic-session'}, policy={'source': 'gui'},
                           principal_id='stored@lid')
            c.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                      ('gateway.local_policy.v1:synthetic-session', json.dumps(receipt)))
            c.execute('UPDATE session_admissions SET payload_json=?,payload_digest=?',
                      (json.dumps(self.fixture.body), self.fixture.digest()))
        payload = dict(self.fixture.payload, platform='desktop', sender_id='stored@lid')
        for phase in ('pre', 'post'):
            with self.subTest(phase=phase), self.assertRaisesRegex(ValueError, '^unsupported_native_turn_provenance$'):
                api.verify_native_turn(self.fixture.home, payload, {'gui:stored@lid': 'person'}, phase=phase)
        self.assertIsNone(self.fixture.controller.store.native_pending(self.fixture.human))
        self.assertIsNone(self.fixture.controller.store.event(self.fixture.human))
