import hashlib
from contextlib import closing
import shutil
import sqlite3
import time
import importlib.util
import json,tempfile,unittest
from pathlib import Path
from continuity.store import Store, Stale
from continuity.extraction import Worker

class ReviewTests(unittest.TestCase):
    def test_due_snooze_reopens_by_revision_without_model(self):
        with tempfile.TemporaryDirectory() as d:
            s=Store(Path(d)/'data'); item=s.create_item('commitment','person','Document expected','e','quote')
            s.transition(item['id'],0,'snoozed',review=100)
            class Never:
                def complete_structured(me,**kw): raise AssertionError('no event requires a model')
            Worker(s,Never(),clock=lambda:99).tick(99)
            self.assertEqual(s.items()[0]['state'],'snoozed')
            Worker(s,Never(),clock=lambda:100).tick(100)
            self.assertEqual(s.items()[0]['state'],'open'); self.assertEqual(s.items()[0]['revision'],2)
            self.assertEqual(s.items()[0]['review'],86500)
            with s.tx() as c:
                body=json.loads(c.execute('SELECT body FROM artifacts').fetchone()[0])
                self.assertEqual(body['completion'],'unverified')
    def test_pause_prevents_due_review(self):
        with tempfile.TemporaryDirectory() as d:
            s=Store(Path(d)/'data'); item=s.create_item('commitment','person','Title','e','quote')
            s.transition(item['id'],0,'snoozed',review=1); s.pause(True)
            Worker(s,None,clock=lambda:2).tick(2)
            self.assertEqual(s.items()[0]['state'],'snoozed')


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.s=Store(Path(self.tmp.name)/'data')
        self.item=self.s.create_item('commitment','person','Draft','request','quote')
        self.assertIsNotNone(importlib.util.find_spec('continuity.lifecycle'), 'Lifecycle service is missing')
        from continuity.lifecycle import Lifecycle
        self.life=Lifecycle(self.s)

    def draft(self, item=None, aid='draft'):
        item=item or self.item
        text='# Draft\nA useful local result, not evidence of delivery or completion.'
        directory=self.s.root/'drafts'; directory.mkdir(exist_ok=True)
        (directory/(aid+'.md')).write_text(text)
        body=dict(type='local_draft',item=item['id'],revision=item['revision'],source=item['source'],
                  path='drafts/'+aid+'.md',sha256=hashlib.sha256(text.encode()).hexdigest(),
                  markdown=text,created=100,provenance=[])
        with self.s.tx() as c:
            c.execute('INSERT INTO artifacts VALUES(?,?,?)',(aid,item['id'],json.dumps(body)))
            c.execute('UPDATE items SET artifact=? WHERE id=?',(aid,item['id']))
        return body

    def test_copied_v2_fixture_migrates_additively_and_idempotently(self):
        fixture=Path(self.tmp.name)/'v2.sqlite3'
        with closing(sqlite3.connect(fixture)) as c, c:
            c.executescript('''
                CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT INTO settings VALUES('paused','true'),('epoch','7'),('ticker_token','"keep-token"');
                CREATE TABLE items(id TEXT PRIMARY KEY,kind TEXT,owner TEXT,title TEXT,source TEXT,quote TEXT,
                    state TEXT DEFAULT 'open',revision INTEGER DEFAULT 0,review REAL,artifact TEXT,
                    principal TEXT,actor TEXT DEFAULT 'unknown',deadline REAL,expectation TEXT,task TEXT,parent TEXT);
                INSERT INTO items VALUES('closed','commitment','person','Private title','source','Private quote',
                    'done',9,NULL,NULL,'person','human',NULL,NULL,NULL,NULL);
                CREATE TABLE item_events(id INTEGER PRIMARY KEY,item TEXT,revision INTEGER,state TEXT,reason TEXT);
                INSERT INTO item_events VALUES(1,'closed',9,'done','historical');
                CREATE TABLE outbox(id TEXT PRIMARY KEY,item TEXT,revision INTEGER,state TEXT,receipt TEXT,epoch INTEGER);
                INSERT INTO outbox VALUES('uncertain','closed',8,'unknown',NULL,7);
                CREATE TABLE budgets(kind TEXT,day TEXT,n INTEGER,PRIMARY KEY(kind,day));
                INSERT INTO budgets VALUES('checkin_delivery','2026-11-07',1);
                CREATE TABLE inference_executions(id TEXT PRIMARY KEY,max_turns INTEGER,attempts INTEGER,state TEXT);
                INSERT INTO inference_executions VALUES('execution',12,12,'exhausted');
                PRAGMA user_version=2;
            ''')
            expected={t:c.execute('SELECT * FROM '+t).fetchall() for t in
                      ('settings','items','item_events','outbox','budgets','inference_executions')}
        root=Path(self.tmp.name)/'copied'; root.mkdir()
        shutil.copyfile(fixture,root/'continuity.sqlite3')
        for _ in range(2):
            migrated=Store(root)
            with migrated.tx() as c:
                self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],3)
                self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                for table,rows in expected.items():
                    self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM '+table)],rows)
                for table in ('lifecycle_states','lifecycle_events','lifecycle_results'):
                    self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
            from continuity.lifecycle import Lifecycle
            self.assertEqual(Lifecycle(migrated).snapshot('closed','person')['stage'],'done')

    def test_stage_transition_is_scoped_cas_and_preserves_compatibility(self):
        args=dict(stage='running',causal_event=None,evidence_ids=[],reason='execution_started',now=101)
        with self.assertRaises(PermissionError):
            self.life.transition(self.item['id'],0,'other',**args)
        snapshot=self.life.transition(self.item['id'],0,'person',**args)
        self.assertEqual((snapshot['stage'],snapshot['item']['state'],snapshot['revision']),('running','open',1))
        with self.assertRaises(Stale):
            self.life.transition(self.item['id'],0,'person',**args)
        with self.s.tx() as c:
            history=[dict(r) for r in c.execute('SELECT * FROM lifecycle_events')]
            self.assertEqual(len(history),1)
            self.assertEqual(history[0]['actor'],'unknown')
            self.assertEqual(history[0]['reason'],'execution_started')
            self.assertEqual(history[0]['changed_at'],101)
            self.assertNotIn('quote',str(history))
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("UPDATE lifecycle_events SET reason='rewritten'")
        self.s.transition(self.item['id'],1,'done')
        snapshot=self.life.snapshot(self.item['id'],'person')
        self.assertEqual((snapshot['stage'],snapshot['result_state']),('done','absent'))
        with self.assertRaises(ValueError):
            self.life.transition(self.item['id'],2,'person',**args)

    def test_invalid_or_foreign_evidence_cannot_mutate_or_leak_into_history(self):
        other=self.s.create_item('commitment','other','Other','other_source','q')
        self.draft(other,aid='foreign')
        args=dict(stage='blocked',causal_event=None,evidence_ids=[],reason='dependency_unavailable',now=101)
        for change in (dict(evidence_ids=['missing']),dict(evidence_ids=['foreign']),
                       dict(reason='Private prose about the user'),dict(stage='sending'),
                       dict(now=float('nan')),dict(evidence_ids='draft'),dict(causal_event='missing')):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.life.transition(self.item['id'],0,'person',**dict(args,**change))
        self.assertEqual(self.life.snapshot(self.item['id'],'person')['revision'],0)
        with self.s.tx() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM lifecycle_events').fetchone()[0],0)

    def test_done_requires_fresh_admitted_linked_human_confirmation(self):
        from continuity.intake import OwnerContext, admit_event
        self.s.configure('identities',{'cli:owner':'person'})
        self.s.ingest(admit_event(self.s,OwnerContext.local('cli','owner'),
                                 dict(id='request',text='Please finish the task')))
        args=dict(stage='done',causal_event=None,evidence_ids=[],reason='owner_confirmed',now=time.time())
        with self.assertRaises(ValueError): self.life.transition(self.item['id'],0,'person',**args)
        for eid,role,text,parent in (
                ('assistant','assistant','I finished it.','request'),
                ('negative','human','I did not finish it.','request'),
                ('unlinked','human','I finished it.',None),
                ('human','human','I finished it.','request')):
            msg=dict(id=eid,text=text,role=role)
            if parent: msg['parent']=parent
            self.s.ingest(admit_event(self.s,OwnerContext.local('cli','owner'),msg))
            kwargs=dict(args,causal_event=eid,evidence_ids=[eid],now=time.time())
            if eid!='human':
                with self.assertRaises(ValueError): self.life.transition(self.item['id'],0,'person',**kwargs)
            else:
                snapshot=self.life.transition(self.item['id'],0,'person',**kwargs)
                self.assertEqual((snapshot['stage'],snapshot['result_state']),('done','confirmed'))
                self.assertIn(eid,snapshot['evidence'])
                self.assertNotEqual(snapshot['transport_state'],'acknowledged')
        with self.s.tx() as c:
            history=dict(c.execute('SELECT * FROM lifecycle_events').fetchone())
            self.assertEqual((history['actor'],history['causal_event']),('human','human'))
            self.assertNotIn('I finished',str(history))

    def test_record_result_is_stable_scoped_and_verified_only_by_file_check(self):
        body=self.draft()
        with self.assertRaises(PermissionError):
            self.life.record_result(self.item['id'],0,'other',{'artifact_id':'draft'},now=101)
        rid=self.life.record_result(self.item['id'],0,'person',{'artifact_id':'draft'},now=101)
        self.assertIsInstance(rid,str)
        self.assertEqual(self.life.snapshot(self.item['id'],'person')['result_state'],'prepared')
        self.assertEqual(self.life.record_result(self.item['id'],1,'person',{'artifact_id':'draft'},now=102),rid)
        self.assertEqual(self.life.snapshot(self.item['id'],'person')['revision'],1)
        with self.assertRaises(Stale):
            self.life.record_result(self.item['id'],0,'person',{'artifact_id':'draft'},now=102)
        self.assertEqual(self.life.record_result(self.item['id'],1,'person',{'artifact_id':'draft','verify':True},now=103),rid)
        snap=self.life.snapshot(self.item['id'],'person')
        self.assertEqual((snap['revision'],snap['stage'],snap['result_state']),(2,'open','verified'))
        with self.s.tx() as c:
            result=dict(c.execute('SELECT * FROM lifecycle_results WHERE id=?',(rid,)).fetchone())
            self.assertEqual((result['digest'],result['observed_at'],result['verified_at']), (body['sha256'],100,103))
            self.assertNotIn(body['markdown'],str(result))
            self.assertEqual(c.execute('SELECT count(*) FROM lifecycle_events').fetchone()[0],2)
        (self.s.root/'drafts'/'draft.md').write_text('changed after verification')
        self.assertEqual(self.life.snapshot(self.item['id'],'person')['result_state'],'prepared')
        # A previous successful verification is not permission to trust a changed file.
        with self.assertRaises(ValueError):
            self.life.record_result(self.item['id'],2,'person',{'artifact_id':'draft','verify':True},now=104)

    def test_result_labels_and_foreign_artifacts_are_not_evidence(self):
        self.draft()
        other=self.s.create_item('commitment','other','Other','other','q'); self.draft(other,'foreign')
        for result in ({'artifact_id':'missing'},{'artifact_id':'foreign'},
                       {'artifact_id':'draft','result_state':'delivered'},
                       {'artifact_id':'draft','confirmed':True},{'artifact_id':'draft','verify':'yes'}):
            with self.subTest(result=result), self.assertRaises(ValueError):
                self.life.record_result(self.item['id'],0,'person',result,now=101)
        self.assertEqual(self.life.snapshot(self.item['id'],'person')['revision'],0)

    def test_artifact_bound_readiness_receipt_is_still_transport_only(self):
        from continuity.outbox import Outbox
        self.draft()
        self.s.configure('mode','active'); self.s.configure('timezone','UTC')
        self.s.configure('allow_delivery',True); self.s.configure('delivery_target','test:owner')
        outbox=Outbox(self.s); oid=outbox.prepare(self.item)
        self.assertTrue(outbox.dispatch(oid,lambda key:{'receipt':'fixture-review-receipt'},43200))
        snap=self.life.snapshot(self.item['id'],'person')
        self.assertEqual((snap['transport_state'],snap['result_state']),('acknowledged','prepared'))
        # Metadata binding identifies what a notice concerns, not what was sent.
        second=self.s.create_item('commitment','person','Another draft','second','q')
        self.draft(second,aid='second-draft')
        with self.s.tx() as c:
            encoded=c.execute("SELECT body FROM artifacts WHERE id='second-draft'").fetchone()[0]
        notice=dict(artifact='second-draft',hash=hashlib.sha256(encoded.encode()).hexdigest(),target='test:owner',created=43200)
        oid=outbox.prepare(second,notice)
        self.assertTrue(outbox.dispatch(oid,lambda key:{'receipt':'fixture-ready-receipt'},43200))
        snap=self.life.snapshot(second['id'],'person')
        self.assertEqual((snap['stage'],snap['result_state'],snap['transport_state']),('open','prepared','acknowledged'))
        self.assertNotIn(oid,snap['evidence'])

    def test_forget_purges_lifecycle_metadata_and_history(self):
        self.draft()
        self.life.record_result(self.item['id'],0,'person',{'artifact_id':'draft'},now=101)
        self.s.configure('ticker_token','keep')
        self.assertFalse(self.s.forget()['cleanup_required'])
        with self.s.tx() as c:
            for table in ('lifecycle_states','lifecycle_results','lifecycle_events'):
                self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        self.assertEqual(self.s.setting('ticker_token'),'keep')
        self.assertTrue(self.s.setting('paused'))

    def test_page_is_complete_bounded_and_cursor_scoped(self):
        with self.s.tx() as c:
            for n in range(110):
                self.s._create_item(c,'commitment','person','Task',str(n),'q')
            self.s._create_item(c,'commitment','other','Private','other','q')
        first=self.life.page('person',stage=None,after=None,limit=1000)
        self.assertEqual(len(first['items']),100)
        self.assertTrue(first['has_more'])
        self.assertIsNotNone(first['next_cursor'])
        with self.assertRaises(ValueError):
            self.life.page('other',stage=None,after=first['next_cursor'])
        with self.assertRaises(ValueError):
            self.life.page('person',stage='open',after=first['next_cursor'])
        with self.assertRaises(ValueError):
            self.life.page('person',stage=None,after=first['next_cursor']+'x')
        from continuity.lifecycle import Lifecycle
        last=Lifecycle(Store(self.s.root)).page('person',stage=None,after=first['next_cursor'])
        self.assertFalse(last['has_more']); self.assertIsNone(last['next_cursor'])
        ids=[i['item']['id'] for i in first['items']+last['items']]
        self.assertEqual(len(ids),111); self.assertEqual(len(set(ids)),111)
        self.assertEqual(ids,sorted(ids))
        self.assertTrue(all(i['item']['principal']=='person' for i in first['items']+last['items']))
        self.life.transition(self.item['id'],0,'person',stage='running',causal_event=None,
                             evidence_ids=[],reason='execution_started',now=101)
        running=self.life.page('person',stage='running',after=None)
        self.assertEqual([i['item']['id'] for i in running['items']],[self.item['id']])
        self.s.transition(self.item['id'],1,'done')
        self.assertEqual(self.life.page('person',stage='running',after=None)['items'],[])
        self.assertEqual(len(self.life.page('person',stage='done',after=None)['items']),1)
        for limit in (0,-1,True,'5'):
            with self.assertRaises(ValueError): self.life.page('person',stage=None,after=None,limit=limit)

    def test_local_owner_cli_exposes_scoped_lifecycle_pages(self):
        import argparse
        import io
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from continuity.cli import Controller, setup_parser
        parser=argparse.ArgumentParser(); setup_parser(parser)
        controller=Controller(self.s.root.parent,SimpleNamespace()); controller._store=self.s
        with redirect_stdout(io.StringIO()) as output:
            page=controller.handle(parser.parse_args(['lifecycle-list','--principal','person','--stage','open','--limit','1']))
        self.assertEqual(json.loads(output.getvalue()),page)
        self.assertEqual(page['items'][0]['item']['id'],self.item['id'])
        self.assertEqual(page['items'][0]['result_state'],'absent')
        self.assertFalse(page['has_more'])
        # Keep the established local diagnostics command compatible.
        with redirect_stdout(io.StringIO()):
            legacy=controller.handle(parser.parse_args(['list']))
        self.assertIn('inference',legacy)
        self.assertEqual(legacy['items'][0]['id'],self.item['id'])

    def test_results_cannot_publish_after_pause_or_on_closed_tombstones(self):
        self.draft()
        self.s.pause(True)
        with self.assertRaises(ValueError):
            self.life.record_result(self.item['id'],0,'person',{'artifact_id':'draft'},now=101)
        self.s.pause(False)
        self.s.transition(self.item['id'],0,'dismissed')
        with self.assertRaises(ValueError):
            self.life.record_result(self.item['id'],1,'person',{'artifact_id':'draft'},now=102)
        snap=self.life.snapshot(self.item['id'],'person')
        self.assertEqual((snap['stage'],snap['revision']),('dismissed',1))

    def test_done_rejects_ambiguous_source_and_replayed_confirmation(self):
        from continuity.intake import OwnerContext, admit_event
        self.s.configure('identities',{'cli:owner':'person'})
        second=self.s.create_item('commitment','person','Second task','request','another quote')
        self.s.ingest(admit_event(self.s,OwnerContext.local('cli','owner'),
                                 dict(id='confirmation',text='I finished it.',parent='request')))
        args=dict(stage='done',causal_event='confirmation',evidence_ids=['confirmation'],reason='owner_confirmed',now=time.time())
        with self.assertRaises(ValueError): self.life.transition(self.item['id'],0,'person',**args)
        self.s.transition(second['id'],0,'dismissed')
        self.life.transition(self.item['id'],0,'person',**args)
        third=self.s.create_item('commitment','person','New task','request','third quote')
        with self.assertRaises(ValueError): self.life.transition(third['id'],0,'person',**args)

    def test_stale_artifact_cannot_replace_the_selected_current_result(self):
        self.draft(aid='old')
        self.draft(aid='current')
        with self.assertRaises(ValueError):
            self.life.record_result(self.item['id'],0,'person',{'artifact_id':'old'},now=101)
        self.assertEqual(self.life.snapshot(self.item['id'],'person')['evidence'],['current'])

    def test_transition_rejects_boolean_revisions_and_invalid_stage_types(self):
        args=dict(stage='running',causal_event=None,evidence_ids=[],reason='execution_started',now=101)
        with self.assertRaises(Stale):
            self.life.transition(self.item['id'],False,'person',**args)
        with self.assertRaises(ValueError):
            self.life.transition(self.item['id'],0,'person',**dict(args,stage=[]))

    def test_stage_projection_preserves_unknown_outbox_and_history(self):
        from continuity.outbox import Outbox
        outbox=Outbox(self.s); oid=outbox.prepare(self.item)
        with self.s.tx() as c:
            c.execute("UPDATE outbox SET state='unknown' WHERE id=?",(oid,))
        before=outbox.get(oid)
        for revision,stage in enumerate(('running','waiting_owner','waiting_third_party','blocked','open','dismissed')):
            snap=self.life.transition(self.item['id'],revision,'person',stage=stage,causal_event=None,
                                      evidence_ids=[oid],reason='stage_changed',now=101+revision)
            self.assertEqual(snap['stage'],stage)
            self.assertEqual(snap['transport_state'],'unknown')
            self.assertEqual(outbox.get(oid),before)
            self.assertEqual(len(self.life.page('person',stage=stage,after=None)['items']),1)
        with self.s.tx() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM lifecycle_events').fetchone()[0],6)
        self.s.configure('ticker_token','keep')
        self.s.forget()
        self.assertIsNone(self.s.setting('lifecycle_cursor_key'))

    def test_concurrent_transitions_have_one_cas_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        def change(_):
            try:
                self.life.transition(self.item['id'],0,'person',stage='running',causal_event=None,
                                     evidence_ids=[],reason='execution_started',now=101)
                return True
            except Stale:
                return False
        with ThreadPoolExecutor(4) as pool:
            self.assertEqual(sum(pool.map(change,range(4))),1)
        with self.s.tx() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM lifecycle_events').fetchone()[0],1)
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_stale_or_revoked_confirmation_never_confirms(self):
        from continuity.intake import OwnerContext, admit_event
        self.s.configure('identities',{'cli:owner':'person'})
        self.s.ingest(admit_event(self.s,OwnerContext.local('cli','owner'),
                                 dict(id='confirmation',text='I finished it.',parent='request')))
        args=dict(stage='done',causal_event='confirmation',evidence_ids=['confirmation'],reason='owner_confirmed',now=time.time()+86401)
        with self.assertRaises(ValueError): self.life.transition(self.item['id'],0,'person',**args)
        self.s.configure('identities',{})
        with self.assertRaises(ValueError): self.life.transition(self.item['id'],0,'person',**dict(args,now=time.time()))
        self.assertEqual(self.life.snapshot(self.item['id'],'person')['result_state'],'absent')

    def test_local_file_is_prepared_not_delivered(self):
        self.draft()
        snapshot=self.life.snapshot(self.item['id'],'person')
        self.assertEqual(snapshot['result_state'],'prepared')
        self.assertNotEqual(snapshot['transport_state'],'acknowledged')
        self.assertEqual(snapshot['stage'],'open')
        self.assertEqual(snapshot['revision'],0)
        self.assertEqual(set(snapshot), {'followthrough','item','revision','stage','result_state','transport_state',
                                        'next_action','reason','evidence','changed_at'})
        with self.assertRaises(PermissionError): self.life.snapshot(self.item['id'],'other')
