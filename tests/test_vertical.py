import contextlib,json,sqlite3,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from continuity.cli import Controller
from continuity.host import capture
from continuity.extraction import Worker
from continuity.adapters import AdapterRegistry,MarkdownAdapter

class VerticalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.home=Path(self.tmp.name)
        self.controller=Controller(self.home,None); self.s=self.controller.store
        self.identities={'whatsapp:owner':'p','whatsapp:transport-alias':'p','telegram:owner':'p','cli:owner':'p'}
        self.s.configure('identities',self.identities)
        with contextlib.closing(sqlite3.connect(self.home/'state.db')) as c, c:
            c.executescript('CREATE TABLE schema_version(version INTEGER); INSERT INTO schema_version VALUES(32); CREATE TABLE sessions(id TEXT,source TEXT,created_source TEXT,parent_session_id TEXT,user_id TEXT,session_key TEXT,chat_id TEXT,chat_type TEXT,thread_id TEXT); CREATE TABLE state_meta(key TEXT,value TEXT);')
            c.executemany('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?)',[
                ('terminal','whatsapp','whatsapp',None,'owner','wa-route','wa-chat','dm',None),
                ('desktop','telegram','telegram',None,'owner','tg-route','tg-chat','dm',None),
                ('local-cli','cli','cli',None,'owner',None,None,None,None),
                ('cron','cron','cron',None,'owner',None,None,None,None),
                ('delegate','whatsapp','whatsapp','terminal','owner','wa-route','wa-chat','dm',None)])
    def hook(self,text,turn='1',session='terminal',phase='pre',**kw):
        if phase=='pre' and session in ('terminal','desktop'):
            from tests.native_fixtures import admit_message
            with contextlib.closing(sqlite3.connect(self.home/'state.db')) as c, c:
                # Producer evidence is server-owned, not copied from hook claims.
                admit_message(c,self.home,session,text,turn,
                              sender='transport-alias' if session=='terminal' else 'owner')
        return capture(self.controller,phase,dict(session_id=session,turn_id=turn,user_message=text,**kw))
    def test_epoch_fences_resolver_after_forget_resume(self):
        from continuity.source import NativeSource
        original=NativeSource.resolve
        def race(resolver,payload,identities=None):
            answer=original(resolver,payload,identities); self.s.forget(); self.s.configure('identities',self.identities); self.s.pause(False); return answer
        with patch.object(NativeSource,'resolve',race): self.assertFalse(self.hook('private request'))
        self.assertFalse(self.s.jobs())
    def test_unknown_cron_delegate_spoof_rejected(self):
        for sid in ('unknown','cron','delegate'):
            with self.assertRaises(ValueError): self.hook('Please draft',session=sid,producer='human')
        with self.assertRaises(ValueError): self.hook('Please draft',sender_id='impostor')
        self.assertFalse(self.s.jobs())
    def test_hook_to_draft_and_cross_surface_close(self):
        text='Please draft a meeting brief using the selected notes.'
        self.hook(text,sender_id='transport-alias'); self.hook(text,phase='post',assistant_response='I will prepare the brief by tomorrow.')
        (self.home/'outline.md').write_text('Project Cedar: launch Friday. Budget 500. Ask for decision on pilot.')
        self.s.configure('mode','prepare'); self.s.configure('preparation_context',[dict(role='notes',scope='selected',path='outline.md')])
        self.s.configure('adapters',[dict(role='notes',scope='selected',type='markdown',root=str(self.home))])
        registry=AdapterRegistry(); registry.add('notes','selected',MarkdownAdapter(self.home))
        packets=[]
        class Model:
            def complete_structured(me,**kw):
                packet=json.loads(kw['input'][0]['text']); packets.append(packet)
                if kw['schema_name']=='continuity_draft':
                    return SimpleNamespace(parsed={'title':'Cedar meeting brief','markdown':'# Cedar decision brief\nApprove a 500 budget pilot for Friday.\n\n## Agenda\nConfirm launch owner and pilot decision.','context_sources':['notes:selected:outline.md']})
                e=packet['event']; close=e['text'].startswith('I have')
                if close:
                    target=next(i for i in packet['related'] if i['actor']=='assistant')
                    proposal=dict(kind='pending_close',owner='human',target=target['id'],target_revision=target['revision'],closure='explicit',assertion=dict(quote=e['text'],polarity='affirmed',certainty='certain',speaker='self',temporality='current',meaning='completion'))
                else: proposal=dict(kind='commitment',owner='assistant',task='draft',deadline=86400,next_review=86400,expectation='local_draft')
                proposal.update(source=e['id'],quote=e['text'],title='Meeting brief')
                return SimpleNamespace(parsed={'proposals':[proposal]})
        model=Model(); worker=Worker(self.s,model,clock=lambda:100,adapters=registry)
        for _ in range(4): worker.tick(100)
        items=self.s.items(); self.assertEqual(len(items),2)
        self.assertTrue(all(i['principal']=='p' and i['actor']=='assistant' for i in items))
        with self.s.tx() as c: bodies=[json.loads(r[0]) for r in c.execute('SELECT body FROM artifacts')]
        drafts=[b for b in bodies if b.get('type')=='local_draft']; self.assertTrue(drafts)
        self.assertIn('Approve',drafts[0]['markdown']); self.assertIn('source',drafts[0]['provenance'][0])
        self.hook('I have completed the task.',turn='2',session='desktop')
        worker.tick(100)
        whatsapp=self.s.event('native:terminal:1:human')
        telegram=self.s.event('native:desktop:2:human')
        assert whatsapp is not None and telegram is not None
        self.assertEqual(whatsapp['surface'],'whatsapp')
        self.assertEqual(telegram['surface'],'telegram')
        self.assertEqual(telegram['principal'],'p')
        # Two live commitments require an exact authenticated revision control.
        self.assertFalse(any(i['state']=='done' for i in self.s.items()))
        selected=next(i for i in self.s.items() if i['state']=='waiting')
        self.controller.handle(SimpleNamespace(action='close',id=selected['id'],revision=selected['revision']))
        self.assertEqual(sum(i['state']=='done' for i in self.s.items()),1)
        self.assertEqual(len(self.s.items()),2)
        self.assertTrue(all('tools' not in p for p in packets))

    def test_native_receipt_and_machine_admission_override(self):
        receipt={'profile_id':str(self.home),'session_id':'local-cli','principal_id':'owner',
                 'entry':{'session_id':'local-cli'},'policy':{'source':'cli'}}
        with contextlib.closing(sqlite3.connect(self.home/'state.db')) as c, c:
            c.execute("UPDATE sessions SET source='local' WHERE id='local-cli'")
            c.execute('INSERT INTO state_meta VALUES(?,?)',('gateway.local_policy.v1:local-cli',json.dumps(receipt)))
        # A creation receipt is not a supported local per-turn authority protocol.
        with self.assertRaisesRegex(ValueError,'native turn admission missing'):
            self.hook('Please draft a brief',session='local-cli')
        self.assertTrue(self.hook('Please draft a brief'))
        # Even an otherwise fully linked native human envelope cannot grant cron authority.
        with contextlib.closing(sqlite3.connect(self.home/'state.db')) as c, c:
            c.execute("UPDATE session_admissions SET principal_id='cron' WHERE target_session_id='terminal'")
        with self.assertRaisesRegex(ValueError,'native principal mismatch'):
            capture(self.controller,'pre',dict(session_id='terminal',turn_id='machine',user_message='Please draft a brief'))
        receipt['policy']['source']='cron'
        with contextlib.closing(sqlite3.connect(self.home/'state.db')) as c, c:
            c.execute('UPDATE state_meta SET value=?',(json.dumps(receipt),))
        with self.assertRaisesRegex(ValueError,'invalid immutable creation receipt'):
            self.hook('Pretend human',session='local-cli',turn='bad-receipt')

    def test_receipt_ack_stale_memory_and_ambiguous_never_close(self):
        item=self.s.create_item('commitment','p','Booking','original','Reserve table')
        for index,text in enumerate(('The message was accepted; close booking.','I remember it was completed.','Maybe the booking is done.')):
            self.hook(text,turn=str(index))
            class Model:
                def complete_structured(me,**kw):
                    packet=json.loads(kw['input'][0]['text']); candidate=packet['related'][0]
                    return SimpleNamespace(parsed={'proposals':[dict(kind='pending_close',owner='human',title='Booking',source=packet['event']['id'],quote=text,target=candidate['id'],target_revision=candidate['revision'],closure='explicit')]})
            Worker(self.s,Model(),clock=lambda:100).tick(100)
            self.assertNotEqual(self.s.items()[0]['state'],'done')
        self.assertEqual(len(self.s.items()),1)

    def test_draft_without_integrations_and_pause_after_model(self):
        self.hook('Please draft a meeting checklist')
        self.s.configure('mode','prepare')
        class Model:
            pause=False
            def complete_structured(me,**kw):
                packet=json.loads(kw['input'][0]['text'])
                if kw['schema_name']=='continuity_draft':
                    self.assertEqual(packet['authorized_context'],[])
                    if me.pause: self.s.pause(True)
                    return SimpleNamespace(parsed={'title':'Checklist','markdown':'# Meeting checklist\nConfirm purpose, participants, decisions, owners, and next review date.','context_sources':[]})
                event=packet['event']
                return SimpleNamespace(parsed={'proposals':[dict(kind='commitment',owner='assistant',source=event['id'],quote=event['text'],title='Checklist',task='draft',expectation='local_draft')]})
        model=Model(); worker=Worker(self.s,model,clock=lambda:100,max_jobs=1)
        worker.tick(100); model.pause=True; worker.tick(100)
        with self.s.tx() as c:
            self.assertFalse(any(json.loads(r[0]).get('type')=='local_draft' for r in c.execute('SELECT body FROM artifacts')))
        self.s.pause(False); model.pause=False; worker.clock=lambda:221
        item=self.s.items()[0]
        self.s.enqueue('draft',dict(item=item['id'],revision=item['revision']),'fresh-draft',221)
        worker.tick(221)
        self.assertIsNotNone(self.s.items()[0]['artifact'])

    def test_deleted_generated_file_detected_at_next_review(self):
        self.hook('Please draft a meeting checklist')
        self.s.configure('mode','prepare')
        class Model:
            def complete_structured(me,**kw):
                packet=json.loads(kw['input'][0]['text'])
                if kw['schema_name']=='continuity_draft': return SimpleNamespace(parsed={'title':'Checklist','markdown':'# Meeting checklist\nConfirm agenda, decision owner, budget and next steps before meeting.','context_sources':[]})
                event=packet['event']
                return SimpleNamespace(parsed={'proposals':[dict(kind='commitment',owner='assistant',source=event['id'],quote=event['text'],title='Checklist',task='draft',expectation='local_draft',next_review=200,deadline=200)]})
        worker=Worker(self.s,Model(),clock=lambda:100); worker.tick(100)
        item=self.s.items()[0]; path=self.s.root/'drafts'/(item['artifact']+'.md')
        self.assertTrue(path.is_file()); path.unlink()
        Worker(self.s,Model(),clock=lambda:201,max_jobs=0).tick(201)
        with self.s.tx() as c: bodies=[json.loads(r[0]) for r in c.execute('SELECT body FROM artifacts')]
        self.assertTrue(any(b.get('decision')=='missing promised artifact' for b in bodies))
        self.assertEqual(self.s.items()[0]['state'],'open')
        self.s.forget(); self.assertFalse((self.s.root/'drafts').exists())

    def test_classic_native_cli_without_user_and_schema_mismatch(self):
        self.s.configure('identities',{'cli:local-owner':'p'})
        with contextlib.closing(sqlite3.connect(self.home/'state.db')) as c,c:
            c.execute("UPDATE sessions SET user_id=NULL WHERE id='local-cli'")
        with self.assertRaisesRegex(ValueError,'native turn admission missing'):
            self.hook('Please draft a brief',session='local-cli')
        self.assertFalse(self.s.jobs())
        with contextlib.closing(sqlite3.connect(self.home/'state.db')) as c,c:
            # v33 is reviewed and supported; v34 remains an unknown protocol.
            c.execute('UPDATE schema_version SET version=34')
        with self.assertRaisesRegex(ValueError,'unsupported native schema'):
            self.hook('New source',turn='2',session='local-cli')
