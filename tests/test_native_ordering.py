"""Actual SDK PRE -> persistence -> POST, isolated synthetic state only."""
import contextlib
import hashlib
import json
import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from continuity.cli import Controller
from continuity.host import capture, register


class NativeOrderingTests(unittest.TestCase):
    def setUp(self):
        from hermes_state import SessionDB
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.native = SessionDB(self.home / 'state.db')
        self.addCleanup(self.native.close)
        self.native.create_session('synthetic-session', 'whatsapp', user_id='stored@lid')
        self.identities = {'whatsapp:stored@lid': 'person', 'whatsapp:sender@pn': 'person'}
        self.controller = Controller(self.home, None)
        self.controller.store.configure('identities', self.identities)
        self.payload = dict(session_id='synthetic-session', turn_id='synthetic-turn',
                            user_message='Please prepare a synthetic brief.',
                            platform='whatsapp', sender_id='sender@pn')
        self.human = 'native:synthetic-session:synthetic-turn:human'
        self.assistant = 'native:synthetic-session:synthetic-turn:assistant'
        self.body = dict(text=self.payload['user_message'], native_text_v1=dict(
            event=dict(message_id='platform-one'),
            source=dict(platform='whatsapp', chat_id='synthetic-chat', chat_type='dm', thread_id=None,
                        user_id='sender@pn', message_id='platform-one', is_bot=False),
            provenance=dict(platform='whatsapp', connector='adapter:whatsapp:', transport_profile=None,
                            runtime_home=str(self.home), transport_home=str(self.home)), route='synthetic-route'))
        with self.db() as c:
            c.execute("UPDATE sessions SET created_source='whatsapp',session_key='synthetic-route',chat_id='synthetic-chat',chat_type='dm'")
            c.execute('''INSERT INTO session_admissions(admission_id,request_id,principal_id,target_session_id,
                payload_json,payload_digest,intent,status,owner_epoch,generation,lineage_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                ('native-admission', 'platform-one', 'messaging:' + json.dumps([None,'whatsapp','synthetic-chat',None,'sender@pn'], separators=(',',':')),
                 'synthetic-session',json.dumps(self.body), self.digest(), 'queue', 'started', 1, 1, '["synthetic-session"]'))
        self.agent = SimpleNamespace(session_id='synthetic-session', _user_id='sender@pn', platform='whatsapp', model='synthetic', _session_db=self.native)
        self.messages = [dict(role='user', content=self.payload['user_message'], platform_message_id='platform-one',
                              display_metadata=dict(gateway_input_owner='reservation-not-admission'))]
        self.hooks = {}
        ctx = SimpleNamespace(register_hook=lambda name, cb: self.hooks.update({name: cb}),
                              register_cli_command=lambda *args: None,
                              get_config=lambda key, default: key == 'collect_references')
        with patch('hermes_constants.get_hermes_home', return_value=self.home):
            register(ctx)

    def digest(self):
        return hashlib.sha256(json.dumps(dict(target='synthetic-session',payload=dict(input=self.body,intent='queue')),
            ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

    @contextlib.contextmanager
    def db(self):
        with contextlib.closing(sqlite3.connect(self.home / 'state.db')) as c, c:
            yield c

    def dispatch(self, name, **payload):
        if name=='post_llm_call': self.assertNotIn('sender_id',payload)
        if name in self.hooks:
            self.hooks[name](**payload)
        return []

    def pre(self):
        from agent.turn_context import _collect_pre_llm_call_context
        with patch('hermes_cli.lifecycle.invoke_hook', side_effect=self.dispatch):
            _collect_pre_llm_call_context(self.agent, effective_task_id='synthetic-session', turn_id='synthetic-turn',
                original_user_message=self.payload['user_message'], messages=self.messages, conversation_history=[])

    def persist(self):
        from agent.turn_context import _persist_turn_start
        from agent.session_persistence import _db_flush_row, _db_flush_write
        def flush(messages, history):
            _db_flush_write(self.agent, [_db_flush_row(self.agent, m, i == 0) for i,m in enumerate(messages)], messages, messages)
        self.agent._ensure_db_session = lambda: None
        self.agent._persist_session = flush
        _persist_turn_start(self.agent, self.messages, [], None)
        with self.db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM messages WHERE role='user'").fetchone()[0], 1)

    def post(self):
        from agent.turn_finalizer import _apply_output_hooks
        with patch('hermes_cli.lifecycle.invoke_hook', side_effect=self.dispatch), \
             patch('agent.turn_finalizer.apply_llm_output_transform', return_value=('Synthetic response.', False, None)), \
             patch('agent.side_question_lease.issue_side_question_token', return_value=None):
            _apply_output_hooks(self.agent, 'Synthetic response.', logging.getLogger(__name__), platform='whatsapp',
                effective_task_id='synthetic-session', turn_id='synthetic-turn',
                original_user_message=self.payload['user_message'], messages=self.messages)

    def test_actual_sdk_pre_then_persist_then_post_preserves_pre_time(self):
        with self.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM messages').fetchone()[0], 0)
        with patch('time.time', return_value=1000.0):
            self.pre()
        self.assertIsNone(self.controller.store.event(self.human))
        self.assertEqual(self.controller.store.jobs(), [])
        self.persist()
        with patch('time.time', return_value=1600.0):
            self.post()
        event = self.controller.store.event(self.human)
        self.assertIsNotNone(event, 'SDK pre precedes persistence: POST must confirm the PRE witness')
        assert event is not None
        self.assertEqual(event['observed_at'], 1000.0)
        self.assertIsNotNone(self.controller.store.event(self.assistant))
        self.assertEqual(len(self.controller.store.jobs()), 2)

    def test_pending_inspection_is_reference_only_and_no_work_is_admitted(self):
        self.pre()
        pending = self.controller.store.native_pending(self.human)
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(set(pending), {'id','source','binding','epoch','observed'})
        serialized = json.dumps(pending)
        for secret in (self.payload['user_message'], 'sender@pn', 'stored@lid', 'person'):
            self.assertNotIn(secret, serialized)
        self.assertIsNone(self.controller.store.event(self.human))
        self.assertEqual(self.controller.store.jobs(), [])
        self.assertIsNone(self.controller.store.claim('synthetic-worker', 1e12))
        self.assertEqual(self.controller.store.items(), [])

    def test_cli_and_tui_without_native_producer_protocol_stay_rejected(self):
        body=self.body
        self.body={'text':self.payload['user_message']}
        digest=self.digest()
        with self.db() as c: c.execute('UPDATE session_admissions SET payload_json=?,payload_digest=?',(json.dumps(self.body),digest))
        self.body=body
        for surface in ('cli','tui'):
            with self.subTest(surface=surface):
                with self.db() as c: c.execute('UPDATE sessions SET source=?,created_source=?',(surface,surface))
                with self.assertRaises(ValueError): capture(self.controller,'pre',dict(self.payload,platform=surface))
                self.assertIsNone(self.controller.store.native_pending(self.human))
                self.assertIsNone(self.controller.store.event(self.human))

    def test_post_without_pre_reference_cannot_backfill(self):
        self.persist()
        with self.assertRaises(ValueError):
            capture(self.controller, 'post', dict(self.payload, assistant_response='Synthetic response.'))
        self.assertIsNone(self.controller.store.event(self.human))
        self.assertEqual(self.controller.store.jobs(), [])

    def test_post_without_persisted_message_cannot_promote_reference(self):
        self.pre()
        with self.assertRaises(ValueError):
            capture(self.controller, 'post', dict(self.payload, assistant_response='Synthetic response.'))
        self.assertIsNotNone(self.controller.store.native_pending(self.human))
        self.assertIsNone(self.controller.store.event(self.human))
        self.persist()
        self.post()
        self.assertIsNotNone(self.controller.store.event(self.assistant))
        self.assertIsNone(self.controller.store.native_pending(self.human))

    def test_persisted_reference_survives_real_process_restart_but_does_not_self_promote(self):
        import subprocess
        import sys
        import agent.turn_context
        self.pre()
        self.persist()
        sdk = str(Path(agent.turn_context.__file__).parent.parent)
        repo = str(Path(__file__).parent.parent)
        code = '''import sys,json
sys.path[:0]=json.loads(sys.argv[1])
from pathlib import Path
from continuity.cli import Controller
from continuity.host import capture
c=Controller(Path(sys.argv[2]),None)
p=json.loads(sys.argv[3])
assert c.store.event(sys.argv[4]) is None
assert c.store.jobs()==[]
assert c.store.native_pending(sys.argv[4]) is not None
assert capture(c,'post',dict(p,assistant_response='Synthetic response.'))
assert c.store.event(sys.argv[4]) is not None
assert c.store.native_pending(sys.argv[4]) is None
print('synthetic process restart confirmed')
'''
        result = subprocess.run([sys.executable,'-I','-c',code,json.dumps([repo,sdk]),str(self.home),json.dumps(self.payload),self.human],
                                text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'synthetic process restart confirmed')
        self.assertEqual(len(self.controller.store.jobs()),2)

    def test_pause_resume_and_forget_purge_pending_and_cannot_resurrect_it(self):
        for action in ('pause','forget','identities'):
            with self.subTest(action=action):
                self.pre()
                self.assertIsNotNone(self.controller.store.native_pending(self.human))
                self.persist()
                if action=='pause':
                    self.controller.store.pause(True)
                elif action=='forget':
                    self.controller.store.forget()
                else:
                    self.controller.store.configure('identities',dict.fromkeys(self.identities,'other'))
                self.controller.store.configure('identities',self.identities)
                self.controller.store.pause(False)
                with self.controller.store.tx() as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM native_pending').fetchone()[0],0)
                with self.assertRaises(ValueError):
                    capture(self.controller,'post',dict(self.payload,assistant_response='Synthetic response.'))
                self.assertIsNone(self.controller.store.event(self.human))
                # Next synthetic PRE again occurs before persistence.
                with self.db() as c: c.execute('DELETE FROM messages')

    def test_pre_rejects_bot_automation_forged_sender_foreign_parent_and_bad_digest(self):
        for mutation in ('bot','automation','sender','parent','digest'):
            with self.subTest(mutation=mutation):
                body=json.loads(json.dumps(self.body))
                payload=dict(self.payload)
                if mutation=='bot': body['native_text_v1']['source']['is_bot']=True
                if mutation=='automation': body['native_text_v1']['automation']={}
                if mutation=='sender': payload['sender_id']='forged'
                if mutation=='parent': payload['parent_session_id']='foreign'
                original=self.body
                self.body=body
                digest='invalid' if mutation=='digest' else self.digest()
                self.body=original
                with self.db() as c: c.execute('UPDATE session_admissions SET payload_json=?,payload_digest=?',(json.dumps(body),digest))
                with self.assertRaises(ValueError): capture(self.controller,'pre',payload)
                self.assertIsNone(self.controller.store.native_pending(self.human))
                self.assertIsNone(self.controller.store.event(self.human))

    def test_pre_never_defers_duplicate_or_contradictory_platform_id(self):
        for mutation in ('duplicate','text','stamp','false-metadata','wrong-role'):
            with self.subTest(mutation=mutation):
                with self.db() as c: c.execute('DELETE FROM messages')
                row: dict=dict(self.messages[0])
                if mutation=='text': row['content']='contradictory synthetic text'
                if mutation=='stamp': row['display_metadata']={'gateway_input_owner':False}
                if mutation=='false-metadata': row['display_metadata']=False
                if mutation=='wrong-role': row['role']='assistant'
                with self.db() as c:
                    values=('synthetic-session',row['role'],row['content'],'platform-one',json.dumps(row['display_metadata']),100.0)
                    c.execute('INSERT INTO messages(session_id,role,content,platform_message_id,display_metadata,timestamp) VALUES(?,?,?,?,?,?)',values)
                    if mutation=='duplicate':
                        # Corrupt the persisted store deliberately: the SDK batch
                        # writer correctly deduplicates exact message identities.
                        c.execute('INSERT INTO messages(session_id,role,content,platform_message_id,display_metadata,timestamp) VALUES(?,?,?,?,?,?)',values)
                        self.assertEqual(c.execute('SELECT count(*) FROM messages').fetchone()[0],2)
                with self.assertRaises(ValueError): capture(self.controller,'pre',self.payload)
                self.assertIsNone(self.controller.store.native_pending(self.human))
                self.assertIsNone(self.controller.store.event(self.human))

    def test_post_never_promotes_duplicate_or_contradictory_message(self):
        self.pre()
        self.persist()
        self.native.append_messages_batch('synthetic-session',[dict(self.messages[0],content='contradictory')])
        with self.assertRaises(ValueError): capture(self.controller,'post',dict(self.payload,assistant_response='Synthetic response.'))
        self.assertIsNone(self.controller.store.event(self.human))
        self.assertEqual(self.controller.store.jobs(),[])

    def test_post_duplicate_exact_rows_are_ambiguous(self):
        self.pre()
        self.persist()
        with self.db() as c:
            c.execute('''INSERT INTO messages(session_id,role,content,platform_message_id,display_metadata,timestamp)
                SELECT session_id,role,content,platform_message_id,display_metadata,timestamp FROM messages WHERE role='user' ''')
            self.assertEqual(c.execute('SELECT count(*) FROM messages').fetchone()[0],2)
        with self.assertRaises(ValueError): capture(self.controller,'post',dict(self.payload,assistant_response='Synthetic response.'))
        self.assertIsNone(self.controller.store.event(self.human))
        self.assertEqual(self.controller.store.jobs(),[])

    def test_ambiguous_started_admissions_never_make_a_witness(self):
        with self.db() as c:
            c.execute('''INSERT INTO session_admissions(admission_id,request_id,principal_id,target_session_id,
                payload_json,payload_digest,intent,status,owner_epoch,generation,lineage_json)
                SELECT 'other-admission','other-request',principal_id,target_session_id,
                payload_json,payload_digest,intent,status,owner_epoch,generation,lineage_json FROM session_admissions''')
        with self.assertRaises(ValueError): capture(self.controller,'pre',self.payload)
        self.assertIsNone(self.controller.store.native_pending(self.human))
        self.assertIsNone(self.controller.store.event(self.human))

    def test_epoch_race_during_pending_reference_cannot_write_a_witness(self):
        from continuity.source import NativeSource
        original=NativeSource.reference
        def revoke(source,*args,**kwargs):
            result=original(source,*args,**kwargs)
            if kwargs.get('pending'):
                self.controller.store.forget()
                self.controller.store.configure('identities',self.identities)
                self.controller.store.pause(False)
            return result
        with patch.object(NativeSource,'reference',revoke):
            self.assertFalse(capture(self.controller,'pre',self.payload))
        self.assertIsNone(self.controller.store.native_pending(self.human))
        self.assertIsNone(self.controller.store.event(self.human))

    def test_epoch_race_after_post_proof_cannot_publish_a_pair(self):
        from continuity.source import NativeSource
        self.pre()
        self.persist()
        original=NativeSource.reference
        calls=[]
        def revoke(source,*args,**kwargs):
            result=original(source,*args,**kwargs)
            calls.append(True)
            if len(calls)==2:
                self.controller.store.pause(True)
                self.controller.store.pause(False)
            return result
        with patch.object(NativeSource,'reference',revoke):
            self.assertFalse(capture(self.controller,'post',dict(self.payload,assistant_response='Synthetic response.')))
        self.assertEqual(len(calls),2)
        self.assertIsNone(self.controller.store.event(self.human))
        self.assertIsNone(self.controller.store.event(self.assistant))

    def test_pair_admission_is_atomic_when_second_job_exceeds_capacity(self):
        self.pre()
        self.persist()
        for i in range(255): self.controller.store.enqueue('synthetic',{},str(i),0)
        with self.assertRaises(ValueError): capture(self.controller,'post',dict(self.payload,assistant_response='Synthetic response.'))
        self.assertIsNone(self.controller.store.event(self.human))
        self.assertIsNone(self.controller.store.event(self.assistant))
        self.assertIsNotNone(self.controller.store.native_pending(self.human))
        self.assertEqual(len(self.controller.store.jobs()),255)

    def test_pending_post_requires_still_started_admission(self):
        self.pre()
        self.persist()
        with self.db() as c: c.execute("UPDATE session_admissions SET status='terminal'")
        with self.assertRaises(ValueError): capture(self.controller,'post',dict(self.payload,assistant_response='Synthetic response.'))
        self.assertIsNone(self.controller.store.event(self.human))

    def test_post_changed_admission_generation_cannot_consume_witness(self):
        self.pre()
        self.persist()
        with self.db() as c: c.execute('UPDATE session_admissions SET generation=2')
        with self.assertRaises(ValueError): capture(self.controller,'post',dict(self.payload,assistant_response='Synthetic response.'))
        self.assertIsNone(self.controller.store.event(self.human))

    def test_repeated_pre_after_persistence_keeps_witness_unpublished_until_post(self):
        with patch('time.time',return_value=1000): self.pre()
        self.persist()
        with patch('time.time',return_value=1300):
            self.pre()
            self.assertIsNone(self.controller.store.event(self.human))
            pending=self.controller.store.native_pending(self.human)
            assert pending is not None
            self.assertEqual(pending['observed'],1000)
        with patch('time.time',return_value=1600): self.post()
        event=self.controller.store.event(self.human)
        assert event is not None
        self.assertEqual(event['observed_at'],1000)

    def test_duplicate_post_is_idempotent(self):
        self.pre()
        self.persist()
        self.post()
        self.post()
        self.assertEqual(len(self.controller.store.jobs()),2)

    def test_different_turn_cannot_reuse_pre_witness(self):
        capture(self.controller,'pre',self.payload)
        with self.assertRaises(ValueError): capture(self.controller,'pre',dict(self.payload,turn_id='other'))
        self.persist()
        with self.assertRaises(ValueError): capture(self.controller,'post',dict(self.payload,turn_id='other',assistant_response='Synthetic response.'))
        self.assertIsNone(self.controller.store.event(self.human))

    def test_restart_purges_expired_references_without_admitting_events(self):
        with patch('time.time',return_value=1000): self.pre()
        with patch('time.time',return_value=87401):
            restarted=Controller(self.home,None)
        with restarted.store.tx() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM native_pending').fetchone()[0],0)
        self.assertIsNone(restarted.store.event(self.human))
        self.assertEqual(restarted.store.jobs(),[])

    def test_pending_expiry_is_purged_not_recovered(self):
        with patch('time.time',return_value=1000): self.pre()
        self.persist()
        with patch('time.time',return_value=87401):
            with self.assertRaises(ValueError): capture(self.controller,'post',dict(self.payload,assistant_response='Synthetic response.'))
            self.assertIsNone(self.controller.store.native_pending(self.human))
        self.assertIsNone(self.controller.store.event(self.human))
