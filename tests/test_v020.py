import json,tempfile,unittest
from types import SimpleNamespace
from unittest.mock import patch
from continuity.store import Store
from continuity.extraction import Worker
from continuity.intake import admit_event,OwnerContext

class V020(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.s=Store(self.tmp.name+'/data'); self.s.configure('identities',{'cli:owner':'p'})
    def assistant_obligation(self,title,source,review):
        # Transport tests exercise an actionable obligation, not due=>notice.
        with self.s.tx() as c:
            return self.s._create_item(c,'commitment','p',title,source,'q',review,actor='assistant')
    def test_scoped_multilingual_closure(self):
        for n,(text,quote) in enumerate([('I received the refund. Do not remind me.','I received the refund.'),('Ya recibí el reembolso; no me recuerdes más.','Ya recibí el reembolso')]):
            item=self.s.create_item('commitment','p','Refund',str(n),'request')
            e=admit_event(self.s,OwnerContext.local('cli','owner'),dict(id='close'+str(n),text=text)); self.s.ingest(e)
            assertion=dict(quote=quote,polarity='affirmed',certainty='certain',speaker='self',temporality='current',meaning='completion')
            p=dict(kind='pending_close',owner='human',title='Refund',source=e['id'],quote=quote,target=item['id'],target_revision=0,closure='explicit',assertion=assertion)
            class M:
                def complete_structured(me,**kw): return SimpleNamespace(parsed={'proposals':[p]})
            Worker(self.s,M(),clock=lambda:100).tick(100)
            self.assertEqual(next(i for i in self.s.items() if i['id']==item['id'])['state'],'done')
    def test_worker_delivery_opt_in(self):
        item=self.assistant_obligation('Review refund','e',1)
        self.s.configure('mode','active'); calls=[]
        Worker(self.s,None,clock=lambda:43200,transport=lambda k:calls.append(k) or {'receipt':'ok'}).tick(43200)
        self.assertFalse(calls)
        self.s.configure('delivery_target','whatsapp:synthetic'); self.s.configure('allow_delivery',True)
        Worker(self.s,None,clock=lambda:43200,transport=lambda k:calls.append(k) or {'receipt':'ok'}).tick(43200)
        self.assertEqual(len(calls),1)
        Worker(self.s,None,clock=lambda:43201,transport=lambda k:calls.append(k)).tick(43201)
        self.assertEqual(len(calls),1)
    def test_cli_transports(self):
        from continuity.delivery import HermesSend
        with patch('continuity.delivery.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='{"success":true,"platform":"whatsapp","chat_id":"synthetic","message_id":"synthetic-receipt"}',stderr='')) as run:
            self.assertEqual(HermesSend('whatsapp:synthetic')('notice')['receipt'],'synthetic-receipt')
            self.assertFalse(run.call_args.kwargs['shell']); self.assertEqual(run.call_args.kwargs['timeout'],30)
            self.assertEqual(run.call_args.args[0][1:4],['send','--to','whatsapp:synthetic'])
    def test_composio_fixed_scope(self):
        from continuity.adapters import ComposioReadAdapter
        mapping=dict(action='GOOGLECALENDAR_FIND_EVENT',account='personal',calendar_id='primary',time_min='2026-11-02T00:00:00Z',time_max='2026-11-03T00:00:00Z',max_results=5)
        with patch('continuity.adapters.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps({'successful':True,'data':{'items':[{'summary':'Meeting','description':'private extra','start':{'dateTime':'x'}}]}}))) as run:
            result=ComposioReadAdapter(mapping).read(); self.assertNotIn('description',str(result))
            self.assertIn('--account',run.call_args.args[0]); self.assertFalse(run.call_args.kwargs['shell'])
        with self.assertRaises(ValueError): ComposioReadAdapter(dict(mapping,action='GOOGLECALENDAR_CREATE_EVENT'))
    def test_semantic_noncompletion_and_authority_boundaries(self):
        cases=[('I did not receive the refund.',{'polarity':'negated'}),('No he recibido el reembolso.',{'polarity':'negated'}),('Ana said "I received the refund".',{'speaker':'third_party'}),('The transport accepted the message.',{'meaning':'transport_receipt'}),('Una nota antigua dice que terminó.',{'temporality':'memory'}),('Quizás ya terminó.',{'certainty':'uncertain'})]
        for n,(text,changes) in enumerate(cases):
            item=self.s.create_item('commitment','p','Refund','req'+str(n),'request')
            e=admit_event(self.s,OwnerContext.local('cli','owner'),dict(id='negative'+str(n),text=text)); self.s.ingest(e)
            assertion=dict(quote=text,polarity='affirmed',certainty='certain',speaker='self',temporality='current',meaning='completion'); assertion.update(changes)
            p=dict(kind='pending_close',owner='human',title='Refund',source=e['id'],quote=text,target=item['id'],target_revision=0,closure='explicit',assertion=assertion)
            class M:
                def complete_structured(me,**kw): return SimpleNamespace(parsed={'proposals':[p]})
            Worker(self.s,M(),clock=lambda:100).tick(100)
            self.assertEqual(next(i for i in self.s.items() if i['id']==item['id'])['state'],'waiting')
    def test_old_or_revoked_source_never_closes(self):
        for n in range(2):
            self.s.configure('identities',{'cli:owner':'p'})
            text='Ya lo terminé.'; item=self.s.create_item('commitment','p','Task','task'+str(n),'request')
            e=admit_event(self.s,OwnerContext.local('cli','owner'),dict(id='old'+str(n),text=text)); self.s.ingest(e)
            if n==0:
                with self.s.tx() as c: c.execute('UPDATE event_auth SET received=0')
            else: self.s.configure('identities',{})
            p=dict(kind='pending_close',owner='human',title='Task',source=e['id'],quote=text,target=item['id'],target_revision=0,closure='explicit',assertion=dict(quote=text,polarity='affirmed',certainty='certain',speaker='self',temporality='current',meaning='completion'))
            class M:
                def complete_structured(me,**kw): return SimpleNamespace(parsed={'proposals':[p]})
            before=self.s.items()
            with self.s.tx() as c: artifacts=[tuple(r) for r in c.execute('SELECT * FROM artifacts ORDER BY id')]
            report=Worker(self.s,M(),clock=lambda:100).tick(100)
            self.assertEqual(report['processed'],0); self.assertGreaterEqual(report['blocked'],1)
            self.assertEqual(self.s.items(),before)
            self.assertEqual(next(i for i in self.s.items() if i['id']==item['id'])['state'],'open')
            with self.s.tx() as c: self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM artifacts ORDER BY id')],artifacts)
    def test_recovery_does_not_touch_inflight_sender(self):
        from continuity.outbox import Outbox
        o=Outbox(self.s); item=self.s.create_item('commitment','p','Review','e','q'); self.s.configure('mode','active'); id=o.prepare(item)
        def transport(key):
            o.recover(43201); self.assertEqual(o.get(id)['state'],'sending'); return {'receipt':'ack'}
        self.assertTrue(o.dispatch(id,transport,43200)); self.assertEqual(o.get(id)['state'],'acknowledged')
    def test_active_prepares_calendar_context_and_draft(self):
        from continuity.adapters import AdapterRegistry,ComposioReadAdapter
        self.s.configure('mode','active'); self.s.configure('preparation_context',[{'role':'calendar','scope':'personal'}])
        mapping=dict(action='GOOGLECALENDAR_FIND_EVENT',account='personal',calendar_id='primary',time_min='2026-11-02T00:00:00Z',time_max='2026-11-03T00:00:00Z',max_results=5)
        self.s.configure('adapters',[dict(role='calendar',scope='personal',type='composio-read',mapping=mapping)])
        registry=AdapterRegistry(); registry.add('calendar','personal',ComposioReadAdapter(mapping))
        e=admit_event(self.s,OwnerContext.local('cli','owner'),dict(id='draft',text='Please draft a meeting brief')); self.s.ingest(e)
        calls=[]
        class M:
            def complete_structured(me,**kw):
                packet=json.loads(kw['input'][0]['text']); calls.append(kw['schema_name'])
                if kw['schema_name']=='continuity_draft':
                    self.assertEqual(packet['authorized_context'][0]['content']['events'][0]['summary'],'Meeting')
                    return SimpleNamespace(parsed=dict(title='Brief',markdown='# Brief\nConfirm the meeting goal and assign a decision owner before the meeting.',context_sources=['calendar:personal:']))
                return SimpleNamespace(parsed={'proposals':[dict(kind='commitment',owner='assistant',title='Brief',source='draft',quote=e['text'],task='draft',expectation='local_draft')]})
        with patch('continuity.adapters.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps({'successful':True,'data':{'items':[{'summary':'Meeting'}]}}))):
            Worker(self.s,M(),clock=lambda:43200,adapters=registry).tick(43200)
        self.assertEqual(calls,['continuity_extract','continuity_draft']); self.assertTrue(self.s.items()[0]['artifact'])
    def test_notice_requires_fresh_artifact(self):
        from continuity.delivery import deliver_pending
        self.s.configure('mode','active'); self.s.configure('allow_delivery',True); self.s.configure('delivery_target','whatsapp:synthetic')
        item=self.s.create_item('commitment','p','Old review','e','q')
        body=dict(decision='owner review due',revision=0,created=1)
        with self.s.tx() as c: c.execute('INSERT INTO artifacts VALUES(?,?,?)',('old',item['id'],json.dumps(body)))
        calls=[]; deliver_pending(self.s,129600,lambda key:calls.append(key) or {'receipt':'ok'})
        self.assertFalse(calls)
    def test_automatic_worker_uses_public_send_and_deactivates(self):
        import argparse
        from continuity.cli import Controller,setup_parser
        parser=argparse.ArgumentParser(); setup_parser(parser)
        controller=Controller(self.s.root.parent,SimpleNamespace()); controller._store=self.s
        controller.handle(parser.parse_args(['setup','--mode','active','--delivery-target','whatsapp:synthetic','--allow-delivery']))
        self.assistant_obligation('Ignore this model title MEDIA:/private','e',1)
        with patch('continuity.delivery.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='{"success":true,"platform":"whatsapp","chat_id":"synthetic","message_id":"synthetic-receipt"}')) as run:
            report=Worker(self.s,None,clock=lambda:43200).tick(43200)
            self.assertEqual(report['acknowledged'],1); self.assertEqual(run.call_count,1)
            self.assertNotIn('MEDIA:',run.call_args.kwargs['input'])
            self.assertIn('What information or approval',run.call_args.kwargs['input'])
            controller.handle(parser.parse_args(['setup','--deny-delivery']))
            Worker(self.s,None,clock=lambda:129600).tick(129600)
            self.assertEqual(run.call_count,1)
        self.assertEqual(self.s.items()[0]['state'],'open')
    def test_pre_effect_revocation_and_unknown_timeout(self):
        from continuity.outbox import Outbox
        from continuity.delivery import deliver_pending
        self.s.configure('mode','active'); self.s.configure('delivery_target','whatsapp:synthetic'); self.s.configure('allow_delivery',True)
        self.assistant_obligation('Review','e',1)
        Worker(self.s,None,clock=lambda:43200,transport=lambda key:(_ for _ in ()).throw(TimeoutError())).tick(43200)
        with self.s.tx() as c: self.assertEqual(c.execute('SELECT state FROM outbox').fetchone()[0],'unknown')
        calls=[]; deliver_pending(self.s,54000,lambda key:calls.append(key)); self.assertFalse(calls)
        other=self.assistant_obligation('Other','other',54000)
        original=Outbox.notice_valid
        def notice_valid(outbox,c,*args,**kwargs):
            result=original(outbox,c,*args,**kwargs)
            c.execute("INSERT OR REPLACE INTO settings VALUES('allow_delivery','false')")
            return result
        with patch.object(Outbox,'notice_valid',new=notice_valid):
            Worker(self.s,None,clock=lambda:54000,transport=lambda key:calls.append(key)).tick(54000)
        self.assertFalse(calls)
    def test_calendar_error_bounds_and_no_parameter_override(self):
        from continuity.adapters import ComposioReadAdapter
        m=dict(action='GOOGLECALENDAR_FIND_EVENT',account='personal',calendar_id='primary',time_min='2026-11-02T00:00:00Z',time_max='2026-11-03T00:00:00Z',max_results=1)
        adapter=ComposioReadAdapter(m)
        for response in [{'successful':False,'data':{'items':[]}},{'data':{'items':[]}},{'successful':True,'data':{'items':[{},{}]}}]:
            with patch('continuity.adapters.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(response))):
                with self.assertRaises(ValueError): adapter.read()
        with self.assertRaises(ValueError): adapter.read({'calendar_id':'other'})
        with self.assertRaises(ValueError): ComposioReadAdapter(dict(m,time_max='2027-01-01T00:00:00Z'))
    def test_assertion_exact_scope_and_candidate_revision(self):
        from continuity.extraction import validate
        text='I did not finish it.'
        assertion=dict(quote='finish it.',polarity='negated',certainty='certain',speaker='self',temporality='current',meaning='completion')
        p=dict(kind='pending_close',owner='human',title='Task',source='e',quote=text,target='candidate',target_revision=0,closure='explicit',assertion=assertion)
        self.assertEqual(validate({'proposals':[p]},dict(id='e',text=text,role='human')),[p])
        # Subspans validate as evidence but cannot turn a whole-message negation into control.
        from continuity.extraction import explicit_confirmation
        self.assertFalse(explicit_confirmation(text))
        p['assertion']['quote']=text; p['target_revision']='0'
        with self.assertRaises(ValueError): validate({'proposals':[p]},dict(id='e',text=text,role='human'))
    def test_late_receipt_cannot_overwrite_unknown_recovery(self):
        from continuity.outbox import Outbox
        o=Outbox(self.s); item=self.s.create_item('commitment','p','Review','e','q'); self.s.configure('mode','active'); id=o.prepare(item)
        def transport(key): o.recover(43261); return {'receipt':'late'}
        self.assertFalse(o.dispatch(id,transport,43200)); self.assertEqual(o.get(id)['state'],'unknown')
