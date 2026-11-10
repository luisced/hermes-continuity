import tempfile, unittest
from types import SimpleNamespace
from continuity.store import Store, Stale
from continuity.intake import admit_event, OwnerContext
from continuity.extraction import Worker

class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.s=Store(self.tmp.name+'/data'); self.s.configure('identities',{'terminal:owner':'person','desktop:owner':'person'})
        self.ctx=OwnerContext.local('terminal','owner')
    def event(self,text='Please prepare the document',id='turn',**kw):
        return admit_event(self.s,self.ctx,{'id':id,'text':text,**kw})
    def model(self,proposals):
        class Model:
            calls=[]
            def complete_structured(me,**kw):
                me.calls.append(kw); return SimpleNamespace(parsed={'proposals':proposals})
        return Model()
    def proposal(self,**kw):
        return dict(kind='commitment',source='turn',quote='Please prepare the document',title='Prepare document',owner='human',**kw)
    def test_trust_and_link(self):
        for ctx in ({'producer':'human'},None):
            with self.assertRaises(ValueError): admit_event(self.s,ctx,{'id':'bad','text':'please'})
        e=self.event(); self.s.ingest(e)
        a=self.event('I will prepare it','reply',role='assistant',parent='turn'); self.assertEqual(a['principal'],'person')
        with self.assertRaises(ValueError): self.event('promise','bad',role='assistant',parent='unknown')
    def test_capture_bounded_and_no_work(self):
        self.s.ingest(self.event()); m=self.model([self.proposal()]); w=Worker(self.s,m,clock=lambda:100)
        self.assertEqual(w.tick(100)['processed'],1); self.assertEqual(len(self.s.items()),1)
        self.assertEqual(len(m.calls),1); self.assertLessEqual(m.calls[0]['max_tokens'],1500)
        self.assertEqual(w.tick(101)['processed'],0); self.assertEqual(len(m.calls),1)
        with self.s.tx() as c: self.assertEqual(c.execute('select count(*) from artifacts').fetchone()[0],1)
    def test_invalid_quote_schema_no_cursor_loss(self):
        for proposal in [dict(self.proposal(),quote='invented'),dict(self.proposal(),permission='send'),dict(self.proposal(),source='spoof')]:
            with self.subTest(proposal=proposal):
                self.s.ingest(self.event(id=str(len(self.s.jobs())))); m=self.model([proposal]); Worker(self.s,m,clock=lambda:100).tick(100)
                self.assertFalse(self.s.items()); self.assertFalse(any(j['state']=='done' for j in self.s.jobs()))
    def test_large_input_budget_and_pause_race(self):
        self.s.ingest(self.event('x'*40000)); m=self.model([]); w=Worker(self.s,m,clock=lambda:100)
        w.tick(100); self.assertEqual(len(m.calls),0); self.assertEqual(self.s.jobs()[0]['state'],'blocked')
        self.s.ingest(self.event(id='small'))
        class Racing:
            def complete_structured(me,**kw):
                self.s.pause(True); return SimpleNamespace(parsed={'proposals':[]})
        Worker(self.s,Racing(),clock=lambda:100).tick(100)
        self.assertFalse(any(j['state']=='done' for j in self.s.jobs()))
    def test_budget_backlog_cas_and_opportunity(self):
        self.s.ingest(self.event()); m=self.model([dict(self.proposal(),kind='opportunity')])
        Worker(self.s,m,clock=lambda:100).tick(100); self.assertEqual(self.s.items()[0]['kind'],'opportunity')
        for _ in range(9): self.s.reserve('model','1970-01-01',10)
        self.s.ingest(self.event(id='later')); Worker(self.s,m,clock=lambda:100).tick(100)
        self.assertEqual(len(m.calls),2); self.assertEqual(self.s.jobs()[-1]['state'],'pending')
        self.assertIn('invalid evidence',self.s.jobs()[-1]['error'])
    def test_assistant_promise_requires_human_task_and_parent_context(self):
        self.s.ingest(self.event('hello','greeting'))
        self.s.ingest(self.event('I will prepare document','promise',role='assistant',parent='greeting'))
        m=self.model([{'kind':'commitment','source':'promise','quote':'I will prepare document','title':'Document','owner':'assistant'}])
        Worker(self.s,m,clock=lambda:100).tick(100)
        self.assertFalse(self.s.items())
        self.s.ingest(self.event(id='request'))
        request=self.s.create_item('commitment','person','Prepare document','request','Please prepare the document')
        self.s.ingest(self.event('I will prepare document','valid-promise',role='assistant',parent='request'))
        class Linked:
            def complete_structured(me,**kw):
                import json
                data=json.loads(kw['input'][0]['text'])
                if data['event']['id']=='valid-promise':
                    self.assertEqual(data['parent']['id'],'request')
                    return SimpleNamespace(parsed={'proposals':[{'kind':'commitment','source':'valid-promise','quote':'I will prepare document','title':'Document','owner':'assistant'}]})
                return SimpleNamespace(parsed={'proposals':[]})
        Worker(self.s,Linked(),clock=lambda:200).tick(200)
        self.assertTrue(any(i['source']=='valid-promise' for i in self.s.items()))
