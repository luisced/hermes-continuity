import json, sqlite3, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from continuity.store import Store
from continuity.extraction import Worker, validate

class RuntimeFixes(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.s=Store(Path(self.tmp.name)/'data')
    def test_assertion_subspan_preserves_response(self):
        text='Ya recibí el reembolso; no me recuerdes más.'
        p=dict(kind='pending_close',source='e',quote=text,title='Refund',owner='human',target='x',target_revision=0,closure='explicit',assertion=dict(quote='Ya recibí el reembolso',polarity='affirmed',certainty='certain',speaker='self',temporality='current',meaning='completion'))
        data={'proposals':[p]}; original=json.dumps(data)
        self.assertIs(validate(data,dict(id='e',text=text,role='human'))[0],p)
        self.assertEqual(json.dumps(data),original)
    def test_duplicate_does_not_mutate_metadata(self):
        with self.s.tx() as c:
            first=self.s._create_item(c,'commitment','p','Task','e','q',actor='assistant',task='draft',expectation='local_draft')
            second=self.s._create_item(c,'commitment','p','Changed','e','q',actor='assistant')
        self.assertEqual(first,second)
    def test_permission_aba_fences_claim(self):
        self.s.configure('adapters',[]); self.s.enqueue('read',{},'r',0); job=self.s.claim('w',0)
        self.s.configure('adapters',[{'role':'x'}]); self.s.configure('adapters',[])
        from continuity.store import Stale
        with self.s.tx() as c:
            with self.assertRaises(Stale): self.s.guard(c,job,1)
    def test_forget_commits_before_unexpected_directory(self):
        self.s.ingest(dict(id='e',text='private',role='human',principal='p')); self.s.configure('ticker_token','technical')
        (self.s.root/'drafts'/'unexpected').mkdir(parents=True)
        result=self.s.forget()
        self.assertTrue(self.s.setting('paused')); self.assertIsNone(self.s.event('e'))
        self.assertEqual(self.s.setting('ticker_token'),'technical'); self.assertTrue(result['cleanup_required'])
    def test_waiting_obligation_reviewed(self):
        i=self.s.create_item('commitment','p','Task','e','q',review=1)
        self.s.transition(i['id'],0,'waiting',review=1)
        self.assertEqual(Worker(self.s,None,clock=lambda:2).reviews(2),1)
    def test_pending_close_without_target_is_proposal_only(self):
        e=dict(id='e',text='Maybe done',role='human',principal='p'); self.s.ingest(e)
        class M:
            def complete_structured(me,**kw): return SimpleNamespace(parsed={'proposals':[dict(kind='pending_close',source='e',quote=e['text'],title='Maybe',owner='human')]})
        report=Worker(self.s,M(),clock=lambda:0).tick(0)
        # An unadmitted raw event is discarded, not actionable human ambiguity.
        self.assertEqual(report['processed'],0); self.assertEqual(report['blocked'],1)
        self.assertEqual(self.s.items(),[])
        with self.s.tx() as c: self.assertEqual(c.execute('SELECT count(*) FROM artifacts').fetchone()[0],0)
    def test_execution_rejects_wrong_draft_actor(self):
        self.s.configure('mode','prepare')
        i=self.s.create_item('opportunity','p','Draft','e','q'); self.s.enqueue('draft',dict(item=i['id'],revision=0),'d',0)
        calls=[]
        class M:
            def complete_structured(me,**kw): calls.append(kw)
        Worker(self.s,M(),clock=lambda:0).tick(0); self.assertFalse(calls)
    def test_synthetic_recording_through_actual_plugin_llm(self):
        from agent.plugin_llm import make_plugin_llm_for_test, _TrustPolicy
        self.s.configure('identities',{'cli:owner':'p'})
        from continuity.intake import admit_event,OwnerContext
        item=self.s.create_item('commitment','p','Refund','request','request')
        text='Ya recibí el reembolso; no me recuerdes más.'
        e=admit_event(self.s,OwnerContext.local('cli','owner'),dict(id='close',text=text)); self.s.ingest(e)
        response={'proposals':[dict(kind='pending_close',source='close',quote=text,title='Refund',owner='human',deadline=None,next_review=None,expectation=None,task=None,target=item['id'],target_revision=0,closure='explicit',assertion=dict(quote='Ya recibí el reembolso',polarity='affirmed',certainty='certain',speaker='self',temporality='current',meaning='completion'))]}
        original=json.dumps(response,ensure_ascii=False); calls=[]
        def caller(**kw):
            calls.append(kw)
            return ('synthetic','synthetic',json.loads(json.dumps({'choices':[{'message':{'content':original}}]}),object_hook=lambda d:SimpleNamespace(**d)))
        llm=make_plugin_llm_for_test(plugin_id='hermes-continuity',policy=_TrustPolicy(plugin_id='hermes-continuity'),sync_caller=caller)
        self.assertEqual(Worker(self.s,llm,clock=lambda:100).tick(100)['processed'],1)
        self.assertEqual(self.s.items()[0]['state'],'done'); self.assertEqual(len(calls),1)
        with self.s.tx() as c:
            reason=json.loads(c.execute('SELECT reason FROM item_events').fetchone()[0]); self.assertEqual(reason['quote'],text)
        self.assertEqual(json.dumps(response,ensure_ascii=False),original)
    def test_host_read_hint_alone_does_not_authorize(self):
        from continuity.adapters import HostReadAdapter
        calls=[]; mapping=dict(tool='anything',args={},schema=dict(type='object',additionalProperties=False,properties={}))
        with self.assertRaises(ValueError): HostReadAdapter(mapping,lambda *a:calls.append(a),lambda n:{'annotations':{'readOnlyHint':True}}).read()
        self.assertFalse(calls)
    def test_stale_registry_and_aba_during_select_denied(self):
        from continuity.adapters import AdapterRegistry,MarkdownAdapter
        (self.s.root/'x.md').write_text('synthetic')
        mapping=dict(role='notes',scope='selected',type='markdown',root=str(self.s.root))
        for aba in (False,True):
            self.s.configure('mode','prepare'); self.s.configure('adapters',[mapping] if aba else [])
            i=self.s.create_item('commitment','p','Task',str(aba),'q')
            self.s.enqueue('read',dict(item=i['id'],revision=0,role='notes',scope='selected',path='x.md'),str(aba),0)
            adapter=MarkdownAdapter(self.s.root)
            class R:
                def select(me,*a):
                    if aba: self.s.configure('adapters',[]); self.s.configure('adapters',[mapping])
                    return adapter
            from unittest.mock import patch
            with patch.object(adapter,'read',side_effect=AssertionError('revoked read')) as read:
                Worker(self.s,None,clock=lambda:0,adapters=R()).tick(0)
                read.assert_not_called()
    def test_legacy_conflicting_actor_migration_is_atomic_and_idempotent(self):
        path=Path(self.tmp.name)/'legacy'; path.mkdir()
        with __import__('contextlib').closing(sqlite3.connect(path/'continuity.sqlite3')) as c, c:
            c.executescript('CREATE TABLE items(id TEXT PRIMARY KEY,kind TEXT,owner TEXT,title TEXT,source TEXT,quote TEXT,state TEXT,revision INTEGER,review REAL,artifact TEXT); CREATE TABLE artifacts(id TEXT PRIMARY KEY,item TEXT,body TEXT); PRAGMA user_version=1;')
            c.execute("INSERT INTO items VALUES('keep','commitment','p','Task','e','q','open',0,NULL,NULL)")
            for n,actor in enumerate(('human','assistant')): c.execute('INSERT INTO artifacts VALUES(?,?,?)',(str(n),'keep',json.dumps({'proposal':{'owner':actor}})))
        migrated=Store(path); first=migrated.items(); self.assertEqual(first[0]['actor'],'unknown')
        self.assertEqual(Store(path).items(),first)
        with migrated.tx() as c: self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],3)
    def test_subspan_cannot_authorize_negated_quoted_or_uncertain_event(self):
        from continuity.intake import admit_event,OwnerContext
        self.s.configure('identities',{'cli:o':'p'})
        for n,text in enumerate(('I did not finish it.','Previous transcript:\n> I finished it.','Quizás ya lo terminé.')):
            i=self.s.create_item('commitment','p','Task',str(n),'q',review=10)
            e=admit_event(self.s,OwnerContext.local('cli','o'),dict(id='e'+str(n),text=text)); self.s.ingest(e)
            p=dict(kind='pending_close',source=e['id'],quote=text,title='Task',owner='human',target=i['id'],target_revision=0,closure='explicit',assertion=dict(quote=text,polarity='affirmed',certainty='certain',speaker='self',temporality='current',meaning='completion'))
            class M:
                def complete_structured(me,**kw): return SimpleNamespace(parsed={'proposals':[p]})
            Worker(self.s,M(),clock=lambda:0).tick(0)
            current=next(v for v in self.s.items() if v['id']==i['id']); self.assertEqual(current['state'],'waiting'); self.assertEqual(current['review'],10)
    def test_relative_time_packet_anchors_source_observation(self):
        self.s.ingest(dict(id='old',text='Tomorrow check refund',role='human',principal='p',observed_at=1))
        class M:
            def complete_structured(me,**kw):
                packet=json.loads(kw['input'][0]['text']); self.assertEqual(packet['event']['observed_at'],1)
                return SimpleNamespace(parsed={'proposals':[dict(kind='commitment',source='old',quote='Tomorrow check refund',title='Refund',owner='assistant')]})
        Worker(self.s,M(),clock=lambda:200000).tick(200000); self.assertEqual(self.s.items()[0]['review'],86401)
    def test_safe_diagnostics_never_store_exception_payload(self):
        from continuity.extraction import safe_error
        self.assertEqual(safe_error(ValueError('assertion evidence scope')),'ValueError: assertion evidence scope')
        self.assertNotIn('private',safe_error(ValueError('private prompt and credentials')))
    def test_queue_full_review_commits_and_existing_work_runs(self):
        i=self.s.create_item('commitment','p','Task','e','q',review=1)
        with self.s.tx() as c:
            c.execute("UPDATE items SET actor='assistant',task='draft',expectation='local_draft' WHERE id=?",(i['id'],))
            for n in range(256): self.s._enqueue(c,'unsupported',{},str(n),0)
        self.s.configure('mode','prepare')
        report=Worker(self.s,None,clock=lambda:2).tick(2)
        self.assertEqual(report['reviewed'],1); self.assertEqual(report['blocked'],4); self.assertEqual(self.s.items()[0]['review'],86402)
    def test_each_claim_uses_current_clock(self):
        for n in range(4): self.s.enqueue('unsupported',{},str(n),0)
        now=[0]; original=self.s.claim; times=[]
        def claim(owner,instant,lease):
            times.append(instant); job=original(owner,instant,lease); now[0]+=45; return job
        self.s.claim=claim
        Worker(self.s,None,clock=lambda:now[0]).tick(0)
        self.assertEqual(times,[0,45,90,135]); self.assertEqual([j['until'] for j in self.s.jobs()],[120,165,210,255])
    def test_ticker_reconciles_drift_and_orphan_identity(self):
        from continuity.ticker import Ticker
        class Fake(Ticker):
            jobs=[]; serial=0
            def api(me,action,**kw):
                if action=='list': return {'jobs':list(me.jobs)}
                if action=='remove': me.jobs[:]=[j for j in me.jobs if j['job_id']!=kw['job_id']]; return {}
                me.serial+=1; job=dict(kw,job_id=str(me.serial),enabled=True); me.jobs.append(job); return job
        ticker=Fake(Path(self.tmp.name),self.s,None)
        first=ticker.setup(); self.assertEqual(ticker.setup(),first)
        for mutation in ({'deliver':'external'},{'enabled':False},{'schedule':'1d'},{'no_agent':False},{'prompt':'do work'}):
            ticker.jobs[0].update(mutation); ticker.setup(); self.assertEqual(len(ticker.jobs),1); self.assertTrue(ticker._valid(ticker.jobs[0]))
        token=self.s.setting('ticker_token'); self.s.forget(); self.assertEqual(self.s.setting('ticker_token'),token)
        self.assertEqual(ticker.setup(),ticker.jobs[0]['job_id']); self.assertEqual(len(ticker.jobs),1)
        script=(Path(self.tmp.name)/'scripts'/('continuity-'+token+'.py')).read_text()
        self.assertLess(script.index('HERMES_DISABLE_LAZY_INSTALLS'),script.index('from hermes_cli'))
        self.assertIn('os.execv',script)
    def test_lifecycle_lock_is_bounded(self):
        from continuity.ticker import bounded_lock
        with bounded_lock(self.s.root/'lock'):
            with self.assertRaises(TimeoutError):
                with bounded_lock(self.s.root/'lock',timeout=.03): self.fail('lock bypass')
    def test_target_unique_and_no_extra_task_on_closure(self):
        p=dict(kind='pending_close',source='e',quote='done',title='Task',owner='human',target='x',target_revision=0)
        event=dict(id='e',text='done',role='human')
        with self.assertRaises(ValueError): validate({'proposals':[p,dict(p)]},event)
        with self.assertRaises(ValueError): validate({'proposals':[p,dict(kind='commitment',source='e',quote='done',title='extra',owner='human')]},event)
    def test_no_parent_cascade_even_with_fresh_natural_confirmation(self):
        from continuity.intake import admit_event,OwnerContext
        self.s.configure('identities',{'cli:o':'p'})
        parent=self.s.create_item('commitment','p','Parent','parent','q')
        with self.s.tx() as c: child=self.s._create_item(c,'commitment','p','Child','child','q',actor='assistant',parent=parent['id'])
        e=admit_event(self.s,OwnerContext.local('cli','o'),dict(id='close',text='I finished it.')); self.s.ingest(e)
        p=dict(kind='pending_close',source=e['id'],quote=e['text'],title='Child',owner='human',target=child['id'],target_revision=0,closure='explicit',assertion=dict(quote=e['text'],polarity='affirmed',certainty='certain',speaker='self',temporality='current',meaning='completion'))
        class M:
            def complete_structured(me,**kw): return SimpleNamespace(parsed={'proposals':[p]})
        Worker(self.s,M(),clock=lambda:0).tick(0)
        selected=next(i for i in self.s.items() if i['id']==child['id'])
        self.assertEqual(selected['state'],'waiting')
        self.s.transition(child['id'],selected['revision'],'done')
        items={i['id']:i for i in self.s.items()}; self.assertEqual(items[parent['id']]['revision'],0); self.assertEqual(items[parent['id']]['state'],'open'); self.assertEqual(items[child['id']]['state'],'done')
    def test_source_schema_error_does_not_escape_hook(self):
        from continuity.host import register
        from unittest.mock import patch
        hooks={}
        class C:
            def register_cli_command(me,*a): pass
            def register_hook(me,name,fn): hooks[name]=fn
            def get_config(me,*a): return True
        with patch('hermes_constants.get_hermes_home',return_value=Path(self.tmp.name)),patch('continuity.host.capture',side_effect=sqlite3.OperationalError('private table info')):
            register(C())
            with self.assertLogs('continuity.host',level='WARNING') as logs: hooks['pre_llm_call']()
            self.assertNotIn('private',str(logs.output)); self.assertIn('OperationalError',str(logs.output))
    def test_final_executor_recheck_and_no_database_lock_over_dispatch(self):
        from continuity.adapters import HostReadAdapter,AdapterRegistry
        mapping=dict(tool='synthetic_read',args={},schema=dict(type='object',additionalProperties=False,properties={}))
        config=dict(type='host-read',role='notes',scope='s',mapping=mapping)
        self.s.configure('mode','prepare'); self.s.configure('adapters',[config])
        i=self.s.create_item('commitment','p','Task','task','q'); calls=[]
        def describe(name):
            self.s.configure('adapters',[])
            return {'annotations':{'readOnlyHint':True,'continuityScopedRead':{'tool':name,'args':{}}}}
        adapter=HostReadAdapter(mapping,lambda *a:calls.append(a),describe)
        r=AdapterRegistry(); r.add('notes','s',adapter)
        self.s.enqueue('read',dict(item=i['id'],revision=0,role='notes',scope='s'),'revoked',0)
        Worker(self.s,None,clock=lambda:0,adapters=r).tick(0); self.assertFalse(calls)
        self.s.configure('adapters',[config]); self.s.enqueue('read',dict(item=i['id'],revision=0,role='notes',scope='s'),'current',0)
        adapter.describe=lambda name:{'annotations':{'readOnlyHint':True,'continuityScopedRead':{'tool':name,'args':{}}}}
        def dispatch(*args):
            # A second connection can write while external code runs.
            with __import__('contextlib').closing(sqlite3.connect(self.s.path,timeout=.1)) as c, c: c.execute("INSERT OR REPLACE INTO settings VALUES('probe','true')")
            calls.append(args); return {}
        adapter.dispatch=dispatch
        Worker(self.s,None,clock=lambda:0,adapters=r).tick(0); self.assertEqual(len(calls),1)
    def test_eight_process_setup_and_lost_create_reply(self):
        import multiprocessing
        scheduler=Path(self.tmp.name)/'scheduler.json'; scheduler.write_text('[]')
        ctx=multiprocessing.get_context('fork')
        children=[ctx.Process(target=synthetic_ticker_process,args=(str(self.s.root),str(scheduler))) for _ in range(8)]
        for child in children: child.start()
        for child in children: child.join(15); self.assertEqual(child.exitcode,0)
        jobs=json.loads(scheduler.read_text()); self.assertEqual(len(jobs),1)
        # A create committed by the scheduler but its reply lost must be adopted.
        scheduler.write_text('[]')
        synthetic_ticker_process(str(self.s.root),str(scheduler),lose_reply=True)
        synthetic_ticker_process(str(self.s.root),str(scheduler))
        self.assertEqual(len(json.loads(scheduler.read_text())),1)

    def test_queued_capability_work_cannot_survive_permission_aba(self):
        self.s.configure('mode','prepare'); self.s.configure('adapters',[])
        self.s.enqueue('read',{},'old',0)
        self.s.configure('adapters',[{'role':'revoked'}]); self.s.configure('adapters',[])
        self.assertIsNone(self.s.claim('w',0))
        self.assertEqual(self.s.jobs()[0]['state'],'blocked')
    def test_legacy_duplicate_ids_and_references_preserved_blocked(self):
        root=Path(self.tmp.name)/'duplicates'; root.mkdir()
        import contextlib
        with contextlib.closing(sqlite3.connect(root/'continuity.sqlite3')) as c, c:
            c.executescript("CREATE TABLE items(id TEXT PRIMARY KEY,kind TEXT,owner TEXT,title TEXT,source TEXT,quote TEXT,state TEXT,revision INTEGER,review REAL,artifact TEXT); PRAGMA user_version=1;")
            for id in ('a','b'): c.execute("INSERT INTO items VALUES(?,'commitment','p','Task','e','q','open',0,NULL,NULL)",(id,))
        migrated=Store(root); self.assertEqual({i['id'] for i in migrated.items()},{'a','b'})
        self.assertTrue(all(i['actor']=='unknown' and i['state']=='blocked' for i in migrated.items()))
        self.assertEqual(Store(root).items(),migrated.items())

    def test_migration_never_reopens_closed_duplicate_tombstones(self):
        root=Path(self.tmp.name)/'closed'; root.mkdir()
        import contextlib
        with contextlib.closing(sqlite3.connect(root/'continuity.sqlite3')) as c, c:
            c.executescript("CREATE TABLE items(id TEXT PRIMARY KEY,kind TEXT,owner TEXT,title TEXT,source TEXT,quote TEXT,state TEXT,revision INTEGER,review REAL,artifact TEXT); PRAGMA user_version=1;")
            for id,state in (('a','done'),('b','dismissed')): c.execute("INSERT INTO items VALUES(?,'commitment','p','Task','e','q',?,4,NULL,NULL)",(id,state))
        records=Store(root).items(); self.assertEqual({i['state'] for i in records},{'done','dismissed'}); self.assertTrue(all(i['revision']==4 for i in records))

    def test_manual_pending_states_preserve_existing_review(self):
        for state in ('waiting','blocked'):
            item=self.s.create_item('commitment','p','Task',state,'q',review=10)
            self.s.transition(item['id'],0,state)
            self.assertEqual(next(i for i in self.s.items() if i['id']==item['id'])['review'],10)

def synthetic_ticker_process(root,scheduler,lose_reply=False):
    from continuity.ticker import Ticker
    class Fake(Ticker):
        def api(me,action,**kw):
            path=Path(scheduler); jobs=json.loads(path.read_text())
            if action=='list': return {'jobs':jobs}
            if action=='remove': jobs=[j for j in jobs if j['job_id']!=kw['job_id']]; path.write_text(json.dumps(jobs)); return {}
            job=dict(kw,job_id='canonical',enabled=True); jobs.append(job); path.write_text(json.dumps(jobs))
            if lose_reply: raise TimeoutError('lost scheduler reply')
            return job
    try: Fake(Path(root).parent,Store(root),None).setup()
    except TimeoutError:
        if not lose_reply: raise
