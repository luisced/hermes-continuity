import json, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from continuity.store import Store, Stale
from continuity.extraction import Worker
from continuity.intake import OwnerContext, admit_event

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.s=Store(self.tmp.name+'/data')
        self.s.configure('identities',{'terminal:owner':'person'})
    def event(self,id='e',text='Please prepare document'):
        e=admit_event(self.s,OwnerContext.local('terminal','owner'),{'id':id,'text':text}); self.s.ingest(e); return e
    def test_expired_worker_cannot_publish_after_takeover(self):
        self.event(); clock=[0]
        class M:
            def complete_structured(me,**kw):
                clock[0]=121; self.s.claim('replacement',121,120)
                return SimpleNamespace(parsed={'proposals':[{'kind':'commitment','source':'e','quote':'Please prepare document','title':'Document','owner':'assistant'}]})
        Worker(self.s,M(),clock=lambda:clock[0],max_jobs=1).tick(0)
        self.assertFalse(self.s.items())
        with self.s.tx() as c: self.assertEqual(c.execute('select count(*) from artifacts').fetchone()[0],0)
    def test_closure_during_model_does_not_replace_artifact(self):
        self.event(); item=self.s.create_item('commitment','person','Document','e','Please prepare document')
        class M:
            def complete_structured(me,**kw):
                self.s.transition(item['id'],0,'done','owner desktop closure')
                return SimpleNamespace(parsed={'proposals':[{'kind':'commitment','source':'e','quote':'Please prepare document','title':'Document','owner':'assistant'}]})
        Worker(self.s,M(),clock=lambda:0,max_jobs=1).tick(0)
        self.assertEqual(self.s.items()[0]['state'],'done')
        with self.s.tx() as c: self.assertEqual(c.execute('select count(*) from artifacts').fetchone()[0],0)
    def test_invalid_json_retries_exhaust_and_forget_scrubs_refs(self):
        self.event()
        class M:
            def complete_structured(me,**kw): return SimpleNamespace(parsed=None)
        for now in (0,10,20): Worker(self.s,M(),clock=lambda:now).tick(now)
        self.assertEqual(self.s.jobs()[0]['state'],'blocked')
        self.s.configure('hook_reference:sensitive',{'session_id':'private'})
        self.s.forget()
        self.assertIsNone(self.s.setting('hook_reference:sensitive'))
        self.assertEqual(self.s.path.stat().st_mode&0o777,0o600)
        self.assertEqual(self.s.root.stat().st_mode&0o777,0o700)
    def test_event_collision_visible_and_backpressure_preserves_existing(self):
        self.event()
        with self.assertRaises(ValueError): self.event(text='different text same ID')
        self.assertEqual(len(self.s.jobs()),1)
    def test_bounded_work_and_context_does_not_resurrect(self):
        for n in range(7): self.event(str(n))
        class M:
            calls=0
            def complete_structured(me,**kw): me.calls+=1; return SimpleNamespace(parsed={'proposals':[]})
        m=M(); Worker(self.s,m,clock=lambda:0,max_jobs=999).tick(0)
        self.assertEqual(m.calls,4); self.assertEqual(sum(j['state']=='pending' for j in self.s.jobs()),3)
    def test_closure_fences_changed_quote_from_same_source(self):
        self.event(text='Please prepare document and outline')
        item=self.s.create_item('commitment','person','Document','e','Please prepare document')
        class Racing:
            def complete_structured(me,**kw):
                self.s.transition(item['id'],0,'done','fresh owner closure')
                return SimpleNamespace(parsed={'proposals':[{'kind':'commitment','source':'e','quote':'outline','title':'New phrasing','owner':'assistant'}]})
        report=Worker(self.s,Racing(),clock=lambda:0,max_jobs=1).tick(0)
        self.assertEqual(len(self.s.items()),1)
        self.assertEqual(report['stale'],1)
        self.assertNotEqual(self.s.jobs()[0]['state'],'done')
    def test_queue_cap_applies_to_owner_preparation_too(self):
        for n in range(256): self.s.enqueue('read',{},str(n),0)
        with self.assertRaises(ValueError): self.s.enqueue('read',{},'overflow',0)
        self.assertEqual(len(self.s.jobs()),256)
