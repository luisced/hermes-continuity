import json,tempfile,unittest
from pathlib import Path
from continuity.adapters import AdapterRegistry, MarkdownAdapter, HostReadAdapter
from continuity.extraction import Worker
from continuity.store import Store

class ReadWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name)
        self.s=Store(self.root/'data'); self.item=self.s.create_item('commitment','person','Prepare document','e','quote')
        self.s.configure('adapters',[dict(type='markdown',role='notes',scope='selected',root=str(self.root))])
        self.r=AdapterRegistry(); (self.root/'note.md').write_text('Existing outline, not a completed document')
        self.r.add('notes','selected',MarkdownAdapter(self.root))
    def enqueue(self,role='notes'):
        self.s.enqueue('read',{'item':self.item['id'],'revision':0,'role':role,'scope':'selected','path':'note.md'},'read:'+role,0)
    def test_shadow_never_reads_and_prepare_artifact(self):
        self.enqueue(); calls=[]
        class Spy:
            def select(me,*args): calls.append(args); return self.r.select(*args)
        Worker(self.s,None,clock=lambda:0,adapters=Spy()).tick(0); self.assertFalse(calls)
        self.s.configure('mode','prepare')
        with self.s.tx() as c: c.execute("DELETE FROM jobs WHERE kind='read'")
        self.enqueue(); Worker(self.s,None,clock=lambda:10,adapters=Spy()).tick(10)
        self.assertEqual(len(calls),1); self.assertEqual(self.s.items()[0]['state'],'open')
        with self.s.tx() as c:
            body=json.loads(c.execute('select body from artifacts').fetchone()[0]); self.assertEqual(body['authority'],'context only')
    def test_close_during_read_and_failed_connector_isolated(self):
        self.s.configure('mode','prepare'); self.enqueue('calendar'); self.enqueue()
        Worker(self.s,None,clock=lambda:0,adapters=self.r).tick(0)
        self.assertEqual(sum(j['state']=='done' for j in self.s.jobs()),1)
        class Racing:
            def read(me,path):
                self.s.transition(self.item['id'],0,'done'); return {'text':'stale remembered context'}
        self.r.add('notes','race',Racing())
        with self.s.tx() as c: c.execute('delete from artifacts')
        self.s.enqueue('read',{'item':self.item['id'],'revision':0,'role':'notes','scope':'race','path':'note.md'},'race',0)
        Worker(self.s,None,clock=lambda:0,adapters=self.r).tick(0)
        with self.s.tx() as c: self.assertEqual(c.execute('select count(*) from artifacts').fetchone()[0],0)
