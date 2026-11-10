"""Version-matched native compatibility, with synthetic host state only."""
import unittest

from continuity.host import capture
from continuity.source import NativeSource, NativeSourceError
from . import test_native_ordering as ordering


class HostCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ordering.NativeOrderingTests(methodName='runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_installed_v33_pre_persistence_post_keeps_exact_pair_protocol(self):
        f = self.fixture
        with f.db() as c:
            self.assertEqual(c.execute('SELECT version FROM schema_version').fetchone()[0], 33,
                             'Review the installed migration before supporting another SDK version')
        # Exercise the installed admission writer/fingerprint/claim contract,
        # not just an SQL row shaped like an admission. The producer envelope
        # remains synthetic: this is not live transport-authentication evidence.
        from hermes_state_runtime import begin_runtime_epoch, admit_session_input, claim_session_input
        with f.db() as c:
            principal = c.execute('SELECT principal_id FROM session_admissions').fetchone()[0]
            c.execute('DELETE FROM session_admissions')
        epoch = begin_runtime_epoch(f.native, instance_id='synthetic-compat-owner')
        receipt = admit_session_input(f.native, epoch=epoch, principal_id=principal,
                                     session_id=f.payload['session_id'], request_id='platform-one',
                                     payload=f.body, intent='queue')
        started = claim_session_input(f.native, epoch=epoch, session_id=f.payload['session_id'])
        self.assertIsInstance(started, dict)
        assert isinstance(started, dict)
        self.assertEqual(started['admission_id'], receipt['admission_id'])
        self.assertEqual(started['status'], 'started')
        f.pre()
        self.assertIsNotNone(f.controller.store.native_pending(f.human))
        self.assertIsNone(f.controller.store.event(f.human))
        self.assertEqual(f.controller.store.jobs(), [])
        f.persist()
        f.post()
        human = f.controller.store.event(f.human)
        assistant = f.controller.store.event(f.assistant)
        self.assertIsNotNone(human)
        self.assertIsNotNone(assistant)
        assert human is not None and assistant is not None
        self.assertEqual(human['text'], f.payload['user_message'])
        self.assertEqual(assistant['parent'], human['id'])
        self.assertEqual(human['principal'], 'person')
        self.assertEqual(assistant['principal'], 'person')
        self.assertIsNone(f.controller.store.native_pending(f.human))
        self.assertEqual(len(f.controller.store.jobs()), 2)

    def test_v32_preserves_same_pending_and_persisted_binding(self):
        f = self.fixture
        # v33 changed data migration only: retain the previous protocol too.
        with f.db() as c:
            c.execute('UPDATE schema_version SET version=32')
        source = NativeSource(f.home)
        pending = source.reference(f.payload, f.identities, pending=True)
        f.pre()
        f.persist()
        self.assertEqual(source.reference(f.payload, f.identities), pending)
        f.post()
        self.assertEqual(len(f.controller.store.jobs()), 2)

    def test_unknown_schema_cannot_create_a_witness_despite_matching_columns(self):
        f = self.fixture
        for version in (31, 34, 1000):
            with self.subTest(version=version):
                with f.db() as c:
                    c.execute('UPDATE schema_version SET version=?', (version,))
                # Identical columns/producer evidence cannot authorize an unknown SDK.
                with self.assertRaisesRegex(NativeSourceError, 'unsupported native schema'):
                    capture(f.controller, 'pre', f.payload)
                self.assertIsNone(f.controller.store.native_pending(f.human))
                self.assertEqual(f.controller.store.jobs(), [])

    def test_actual_v33_delegate_retag_migration_stays_nonhuman(self):
        from hermes_state import SessionDB
        f = self.fixture
        f.native.create_session('synthetic-parent', 'whatsapp', user_id='stored@lid')
        f.native.close()
        with f.db() as c:
            c.execute('UPDATE schema_version SET version=32')
            c.execute("UPDATE sessions SET source='subagent', parent_session_id='synthetic-parent', model_config='{}' WHERE id='synthetic-session'")
        migrated = SessionDB(f.home / 'state.db')
        self.addCleanup(migrated.close)
        with f.db() as c:
            self.assertEqual(c.execute('SELECT version FROM schema_version').fetchone()[0], 33)
            self.assertEqual(c.execute("SELECT json_extract(model_config,'$._delegate_from') FROM sessions WHERE id='synthetic-session'").fetchone()[0], 'synthetic-parent')
        with self.assertRaisesRegex(NativeSourceError, 'branched or delegated session'):
            capture(f.controller, 'pre', f.payload)
        self.assertIsNone(f.controller.store.native_pending(f.human))
        self.assertEqual(f.controller.store.jobs(), [])
