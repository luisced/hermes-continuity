import argparse,json,sqlite3,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from continuity.store import Store
from continuity.cli import Controller
from continuity.extraction import Worker
from continuity.adapters import MarkdownAdapter

class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name); self.s=Store(self.home/'continuity')
    def test_forget_survives_scheduler_failure(self):
        c=Controller(self.home,SimpleNamespace(dispatch_tool=lambda *a:None)); c._store=self.s
        self.s.ingest(dict(id='private',text='secret',role='human',principal='p'))
        self.s.configure('ticker_token','owned')
        with patch('continuity.cli.Ticker._remove_locked',side_effect=RuntimeError('offline')):
            result=c.handle(SimpleNamespace(action='forget'))
        self.assertTrue(self.s.setting('paused')); self.assertFalse(self.s.jobs())
        self.assertIsNone(self.s.event('private')); self.assertIn('scheduler_residue',result)
        self.assertEqual(self.s.setting('ticker_token'),'owned')
    def test_pause_during_selection_prevents_read(self):
        self.s.configure('mode','prepare'); item=self.s.create_item('commitment','p','draft','e','q')
        self.s.enqueue('read',dict(item=item['id'],revision=0,role='notes',scope='s',path='x.md'),'read',0)
        calls=[]
        class Adapter:
            def read(me,*a): calls.append(a)
        class Registry:
            def select(me,*a): self.s.pause(True); return Adapter()
        Worker(self.s,None,clock=lambda:0,adapters=Registry()).tick(0)
        self.assertEqual(calls,[])
    def test_markdown_ancestor_swap(self):
        root=self.home/'parent'/'notes'; root.mkdir(parents=True); (root/'x.md').write_text('inside')
        outside=self.home/'outside'/'notes'; outside.mkdir(parents=True); (outside/'x.md').write_text('outside')
        adapter=MarkdownAdapter(root); original=__import__('os').open; swapped=[]
        def race(path,*a,**kw):
            if not swapped:
                swapped.append(1); (self.home/'parent').rename(self.home/'saved'); (self.home/'parent').symlink_to(self.home/'outside',target_is_directory=True)
            return original(path,*a,**kw)
        with patch('continuity.adapters.os.open',side_effect=race):
            try: result=adapter.read('x.md')
            except (ValueError,OSError): return
        self.assertEqual(result['text'],'inside')
    def test_actor_and_due_missing_artifact(self):
        self.s.ingest(dict(id='e',text='Please draft a meeting brief',role='human',principal='p'))
        class M:
            def complete_structured(me,**kw): return SimpleNamespace(parsed={'proposals':[dict(kind='commitment',source='e',quote='Please draft a meeting brief',title='Brief',owner='assistant',deadline=10,next_review=10,expectation='local_draft',task='draft',target=None,target_revision=None,closure='none')]})
        Worker(self.s,M(),clock=lambda:0).tick(0)
        item=self.s.items()[0]; self.assertEqual(item['principal'],'p'); self.assertEqual(item['actor'],'assistant')
        self.assertEqual(item['review'],10)
        self.assertEqual(Worker(self.s,M(),clock=lambda:11).tick(11)['reviewed'],1)
        with self.s.tx() as db:
            bodies=[json.loads(r[0]) for r in db.execute('select body from artifacts')]
        self.assertTrue(any(b.get('decision')=='missing promised artifact' for b in bodies))

    def test_forget_list_and_remove_failures_leave_inert_cleanup_token(self):
        for failing in ('list','remove'):
            with self.subTest(failing=failing):
                self.s.pause(False); self.s.configure('ticker_token','owned')
                self.s.ingest(dict(id='private',text='private source',role='human',principal='p'))
                c=Controller(self.home,SimpleNamespace(dispatch_tool=lambda *a:None)); c._store=self.s
                def api(ticker,action,**kw):
                    self.assertTrue(self.s.setting('paused')); self.assertIsNone(self.s.event('private'))
                    if action==failing: raise RuntimeError('scheduler failed')
                    return {'jobs':[{'name':'hermes-continuity:owned','script':'continuity-owned.py','no_agent':True,'job_id':'job'}]}
                with patch('continuity.cli.Ticker.api',api): result=c.handle(SimpleNamespace(action='forget'))
                self.assertTrue(result['scheduler_residue']['cleanup_required'])
                class Never:
                    def complete_structured(me,**kw): raise AssertionError('paused model')
                self.assertEqual(Worker(self.s,Never(),adapters=lambda:(_ for _ in ()).throw(AssertionError('paused adapter'))).tick()['processed'],0)

    def test_actor_dedupe_and_identity_rechecked_at_hook_commit(self):
        event=dict(id='e',text='I will prepare the brief',role='human',principal='p')
        self.s.configure('identities',{'cli:owner':'p'})
        epoch=self.s.setting('epoch')
        self.s.configure('identities',{})
        self.assertFalse(self.s.ingest(event,epoch=epoch,identity_key='cli:owner'))
        self.s.ingest(event)
        class Model:
            def complete_structured(me,**kw):
                return SimpleNamespace(parsed={'proposals':[dict(kind='commitment',source='e',quote=event['text'],title='Brief',owner=actor) for actor in ('human','assistant')]})
        Worker(self.s,Model(),clock=lambda:0).tick(0)
        self.assertEqual({i['actor'] for i in self.s.items()},{'human','assistant'})
        self.assertEqual({i['principal'] for i in self.s.items()},{'p'})

    def test_read_mapping_revocation_during_call_fences_publication(self):
        self.s.configure('mode','prepare'); self.s.configure('adapters',[{'scope':'selected'}])
        item=self.s.create_item('commitment','p','Brief','e','q')
        self.s.enqueue('read',dict(item=item['id'],revision=0,role='notes',scope='selected',path='x.md'),'read',0)
        class Adapter:
            def read(me,*args): self.s.configure('adapters',[]); return {'text':'now revoked'}
        class Registry:
            def select(me,*args): return Adapter()
        Worker(self.s,None,clock=lambda:0,adapters=Registry()).tick(0)
        with self.s.tx() as c: self.assertEqual(c.execute('SELECT count(*) FROM artifacts').fetchone()[0],0)
