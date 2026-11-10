"""Persisted worker acceptance; fake only synthesis and transport."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from continuity.store import Store
from continuity.extraction import Worker


class GeneralFollowthroughTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = Store(Path(self.tmp.name)/'state')
        for key, value in [('mode', 'active'), ('timezone', 'UTC'),
                           ('allow_delivery', True), ('delivery_target', 'test:owner')]:
            self.s.configure(key, value)
        self.calls = []
        self.prompts = []

    def tick(self, now=43200, model=None):
        def transport(key):
            self.calls.append(key)
            return {'receipt': 'test-receipt-'+str(len(self.calls))}
        return Worker(self.s, model, clock=lambda: now, transport=transport).tick(now)

    def extract(self, *, title='Review reservation', contact='explicit_reminder', closure='owner_confirmation'):
        from continuity.intake import OwnerContext, admit_event
        self.s.configure('identities', {'cli:owner': 'person'})
        self.s.configure('allow_model', True)
        text = 'Please remind me to review the reservation.'
        event = admit_event(self.s, OwnerContext.local('cli', 'owner'), {'id': 'request', 'text': text})
        self.s.ingest(event)
        proposal = dict(kind='commitment', owner='human', source='request', quote=text,
                        title=title, next_review=43200,
                        followthrough=dict(goal='Review reservation details', closure=closure,
                                           contact=contact, purpose='reservation', subject='reservation-review'))
        class Model:
            def complete_structured(me, **kwargs):
                self.prompts.append(kwargs['instructions'])
                return SimpleNamespace(parsed={'proposals': [proposal]})
        # Admission timestamps are real; processing is at the event time.
        self.s.configure('mode', 'prepare')
        Worker(self.s, Model(), clock=lambda: event['observed_at']).tick(event['observed_at'])
        self.s.configure('mode', 'active')
        self.assertEqual(len(self.s.items()), 1, 'structured reusable followthrough contract must be admitted')
        return self.s.items()[0]

    def test_explicit_reminder_survives_and_unchanged_subject_does_not_repeat(self):
        item = self.extract()
        self.tick()
        self.assertEqual(len(self.calls), 1)
        self.s = Store(self.s.root)
        self.tick(129600)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.s.items()[0]['state'], 'open')
        self.assertEqual(self.s.items()[0]['review'], 216000)
        with self.s.tx() as c:
            reviews = [json.loads(r[0]) for r in c.execute("SELECT body FROM artifacts WHERE id LIKE 'review:%'")]
            self.assertEqual(c.execute('SELECT count(*) FROM outbox').fetchone()[0], 1)
        self.assertEqual(reviews[-1]['next_action'], 'wait')
        self.assertEqual(reviews[-1]['contact_reason'], 'unchanged_subject')

    def test_reminder_delivery_is_actionable_not_generic_due_notice(self):
        from unittest.mock import patch
        self.extract()
        messages = []
        def send(message):
            messages.append(message)
            return {'receipt': 'message-receipt'}
        with patch('continuity.delivery.HermesSend', return_value=send):
            Worker(self.s, None, clock=lambda: 43200).tick(43200)
        self.assertEqual(len(messages), 1)
        self.assertIn('Review reservation', messages[0])
        self.assertNotIn('Completion remains unverified', messages[0])
        self.assertNotIn('an open commitment', messages[0])

    def checkin(self, purpose='reservation', facet='decision', quote='reservation details'):
        config = dict(enabled=True, principal='person', sources={}, calendars=[],
                      primary='ledger', allow_reads=True,
                      quality_contract=dict(purpose=purpose, any_of=[facet]))
        self.s.configure('checkin', config)
        self.s.configure('allow_model', True)
        self.s.create_item('commitment', 'person', quote, 'context', quote)
        return config

    def test_nonwellness_purpose_contract_uses_real_checkin_worker(self):
        self.checkin()
        class Model:
            def complete_structured(me, **kwargs):
                return SimpleNamespace(parsed=dict(message='About reservation details, what decision remains?',
                    citations=[dict(source='ledger', quote='reservation details')],
                    quality=dict(purpose='reservation', coverage=[dict(facet='decision', source='ledger', quote='reservation details')])))
        report = self.tick(model=Model())
        self.assertEqual(report['acknowledged'], 1)
        with self.s.tx() as c:
            body=json.loads(c.execute("SELECT draft FROM checkin_days").fetchone()[0])
        self.assertEqual(body['quality']['purpose'], 'reservation')

    def test_wellbeing_label_alone_does_not_turn_shopping_reply_into_coverage(self):
        from continuity.checkin import Checkins
        from continuity.intake import OwnerContext, admit_event
        self.checkin(purpose='wellbeing', facet='rest', quote='rest after a long day')
        class Model:
            def complete_structured(me, **kwargs):
                return SimpleNamespace(parsed=dict(message='About rest after a long day, what would help?',
                    citations=[dict(source='ledger', quote='rest after a long day')],
                    quality=dict(purpose='wellbeing', coverage=[dict(facet='rest', source='ledger', quote='rest after a long day')])))
        self.assertEqual(self.tick(model=Model())['acknowledged'], 1)
        self.s.configure('identities', {'cli:owner':'person'})
        event=admit_event(self.s, OwnerContext.local('cli','owner'), {'id':'reply', 'text':'Buy groceries tomorrow.'})
        self.s.ingest(event)
        with self.s.tx() as c: day=c.execute('SELECT id FROM checkin_days').fetchone()[0]
        Checkins(self.s).record_response(day, event['id'], 'wellbeing', [])
        from continuity.followthrough import activity
        self.assertIsNotNone(activity(self.s)['latest_response'])
        self.assertEqual(activity(self.s)['latest_response']['event'],event['id'])
        self.assertIsNone(activity(self.s)['latest_closure'])
        with self.s.tx() as c:
            self.assertNotEqual(c.execute('SELECT state FROM checkin_days').fetchone()[0], 'answered')

    def test_refund_contextual_reply_from_another_authorized_surface_is_response_not_closure(self):
        from continuity.intake import OwnerContext, admit_event
        from continuity.lifecycle import Lifecycle
        self.s.configure('identities', {'cli:owner':'person', 'chat:owner':'person'})
        self.s.configure('allow_model', True)
        item=self.s.create_item('commitment', 'person', 'Refund case', 'refund-request', 'Check refund')
        text='For Refund case, I am checking my bank balance.'
        event=admit_event(self.s, OwnerContext.local('chat','owner'), {'id':'refund-reply', 'text':text})
        self.s.ingest(event)
        class Model:
            def complete_structured(me, **kwargs):
                return SimpleNamespace(parsed={'proposals':[dict(kind='pending_close', owner='human', source=event['id'], quote=text,
                    title='Refund update', target=item['id'], target_revision=0, closure='none',
                    assertion=dict(quote=text, polarity='affirmed', certainty='certain', speaker='self', temporality='current', meaning='other'))]})
        self.tick(now=event['observed_at'], model=Model())
        self.assertEqual(self.s.items()[0]['state'], 'open')
        snap=Lifecycle(self.s).snapshot(item['id'], 'person')
        self.assertTrue(snap['followthrough']['responded'])
        self.assertFalse(snap['followthrough']['resolved'])
        self.assertEqual(snap['followthrough']['next_action'], 'wait')
        self.assertIn('refund-reply', snap['followthrough']['response_evidence'])

    def test_work_draft_execution_notice_and_goal_are_distinct_evidence(self):
        from continuity.lifecycle import Lifecycle
        with self.s.tx() as c:
            item=self.s._create_item(c, 'commitment','person','Work brief','request','Draft a brief',
                                      actor='assistant',task='draft',expectation='local_draft')
        self.s.configure('allow_model', True)
        self.s.enqueue('draft',dict(item=item['id'],revision=0),'draft',43200)
        class Model:
            def complete_structured(me, **kwargs):
                return SimpleNamespace(parsed=dict(title='Work brief', markdown='# Work brief\nConfirm the decision criteria and assign the document reviewer.', context_sources=[]))
        self.assertEqual(self.tick(model=Model())['acknowledged'], 1)
        snapshot=Lifecycle(self.s).snapshot(item['id'],'person')
        state=snapshot['followthrough']
        self.assertEqual(set(('prepared','executed','sent','responded','resolved'))-set(state), set())
        self.assertTrue(state['prepared'])
        self.assertFalse(state['executed'])
        self.assertTrue(state['sent'])
        self.assertFalse(state['responded'])
        self.assertFalse(state['resolved'])
        self.assertEqual(state['delivery_scope'],'notice_only')
        self.assertEqual(state['closure_condition'],'owner_confirmation')
        artifact=self.s.items()[0]['artifact']
        Lifecycle(self.s).record_result(item['id'],0,'person',dict(artifact_id=artifact,verify=True),now=43201)
        state=Lifecycle(self.s).snapshot(item['id'],'person')['followthrough']
        self.assertTrue(state['executed'])
        self.assertFalse(state['resolved'])
        self.assertEqual(state['execution_scope'],'private_file')

    def test_notice_only_goal_resolves_on_receipt_but_not_an_underlying_reservation(self):
        from continuity.lifecycle import Lifecycle
        item=self.extract(closure='notice_acknowledged')
        self.tick()
        state=Lifecycle(self.s).snapshot(item['id'],'person')['followthrough']
        self.assertTrue(state['resolved'])
        self.assertFalse(state['responded'])
        self.assertEqual(state['next_action'],'close')
        self.assertEqual(state['closure_condition'],'notice_acknowledged')
        self.assertEqual(self.s.items()[0]['state'],'open', 'closure projection does not manufacture owner confirmation')

    def test_grounded_purpose_response_closes_only_checkin_coverage(self):
        from continuity.checkin import Checkins
        from continuity.intake import OwnerContext, admit_event
        self.checkin(purpose='wellbeing', facet='rest', quote='rest after a long day')
        class Model:
            def complete_structured(me, **kwargs):
                return SimpleNamespace(parsed=dict(message='About rest after a long day, what would help?',
                    citations=[dict(source='ledger', quote='rest after a long day')],
                    quality=dict(purpose='wellbeing', coverage=[dict(facet='rest', source='ledger', quote='rest after a long day')])))
        self.assertEqual(self.tick(model=Model())['acknowledged'], 1)
        self.s.configure('identities', {'cli:owner':'person'})
        text='I feel rested after sleeping well.'
        event=admit_event(self.s, OwnerContext.local('cli','owner'), {'id':'rest-reply','text':text})
        self.s.ingest(event)
        with self.s.tx() as c: day=c.execute('SELECT id FROM checkin_days').fetchone()[0]
        import inspect
        self.assertIn('quality', inspect.signature(Checkins.record_response).parameters)
        quality=dict(purpose='wellbeing',coverage=[dict(facet='rest',source=event['id'],quote=text)])
        self.assertTrue(Checkins(self.s).record_response(day,event['id'],'wellbeing',[],quality=quality))
        with self.s.tx() as c:
            self.assertEqual(c.execute('SELECT state FROM checkin_days').fetchone()[0],'answered')
            proof=json.loads(c.execute("SELECT body FROM artifacts WHERE id=?",('purpose-response:'+event['id'],)).fetchone()[0])
        self.assertEqual(proof['quality'],quality)
        self.assertEqual(next(i for i in self.s.items() if i['kind']=='commitment')['state'],'open')

    def test_cli_status_separates_actual_delivery_reply_and_closure(self):
        import argparse
        import contextlib
        import io
        from continuity.cli import Controller, setup_parser
        from continuity.outbox import Outbox
        item=self.extract()
        self.tick()
        later=self.s.create_item('commitment','person','Later planned item','later','Later')
        Outbox(self.s).prepare(later)
        parser=argparse.ArgumentParser();setup_parser(parser)
        controller=Controller(Path(self.tmp.name),SimpleNamespace())
        controller._store=self.s
        with contextlib.redirect_stdout(io.StringIO()): status=controller.handle(parser.parse_args(['status']))
        self.assertIn('activity', status)
        self.assertEqual(status['activity']['latest_delivery']['item'],item['id'])
        self.assertEqual(status['activity']['latest_delivery']['receipt'],'test-receipt-1')
        self.assertIsNone(status['activity']['latest_response'])
        self.assertIsNone(status['activity']['latest_closure'])
        self.assertTrue(status['followthrough'][0]['followthrough']['sent'])
        self.assertFalse(status['followthrough'][0]['followthrough']['resolved'])

    def test_real_worker_requests_structured_contracts_not_implicit_generic_due_contact(self):
        from continuity.checkin import INSTRUCTIONS as checkin_instructions
        self.extract()
        self.assertIn('followthrough',self.prompts[0])
        self.assertIn('explicit_reminder',self.prompts[0])
        self.assertIn('quality',checkin_instructions)
        self.assertIn('coverage',checkin_instructions)

    def test_unchanged_checkin_subject_does_not_repeat_on_new_day(self):
        self.checkin()
        class Model:
            def complete_structured(me, **kwargs):
                return SimpleNamespace(parsed=dict(message='About reservation details, what decision remains?',
                    citations=[dict(source='ledger', quote='reservation details')],
                    quality=dict(purpose='reservation', coverage=[dict(facet='decision', source='ledger', quote='reservation details')])))
        self.tick(model=Model())
        self.s=Store(self.s.root)
        self.tick(now=129600,model=Model())
        self.assertEqual(len(self.calls),1)
        with self.s.tx() as c:
            self.assertEqual(c.execute('SELECT reason FROM checkin_days ORDER BY day DESC LIMIT 1').fetchone()[0],'unchanged_subject')
            self.assertEqual(c.execute('SELECT count(*) FROM outbox').fetchone()[0],1)

    def test_negative_future_contextual_reply_is_still_response_not_resolution(self):
        from continuity.intake import OwnerContext, admit_event
        from continuity.lifecycle import Lifecycle
        self.s.configure('identities', {'cli:owner':'person'})
        self.s.configure('allow_model',True)
        item=self.s.create_item('commitment','person','Reservation','reservation','Arrange reservation')
        text='For Reservation, I cannot decide until tomorrow.'
        event=admit_event(self.s,OwnerContext.local('cli','owner'),dict(id='update',text=text));self.s.ingest(event)
        class Model:
            def complete_structured(me, **kwargs):
                return SimpleNamespace(parsed={'proposals':[dict(kind='pending_close',owner='human',source=event['id'],quote=text,title='Update',target=item['id'],target_revision=0,closure='none',assertion=dict(quote=text,polarity='negated',certainty='uncertain',speaker='self',temporality='future',meaning='other'))]})
        self.tick(now=event['observed_at'],model=Model())
        self.assertEqual(self.s.items()[0]['state'],'open')
        state=Lifecycle(self.s).snapshot(item['id'],'person')['followthrough']
        self.assertTrue(state['responded'])
        self.assertFalse(state['resolved'])

    def test_response_proposal_without_explicit_closure_cannot_close_goal(self):
        from continuity.intake import OwnerContext, admit_event
        self.s.configure('identities',{'cli:owner':'person'});self.s.configure('allow_model',True)
        item=self.s.create_item('commitment','person','Refund case','refund','Check refund')
        text='For Refund case, I received the refund.'
        event=admit_event(self.s,OwnerContext.local('cli','owner'),dict(id='response',text=text));self.s.ingest(event)
        class Model:
            def complete_structured(me, **kwargs):
                return SimpleNamespace(parsed={'proposals':[dict(kind='pending_close',owner='human',source=event['id'],quote=text,title='Update',target=item['id'],target_revision=0,closure='none',assertion=dict(quote=text,polarity='affirmed',certainty='certain',speaker='self',temporality='current',meaning='completion'))]})
        self.tick(now=event['observed_at'],model=Model())
        self.assertNotEqual(self.s.items()[0]['state'],'done')

    def test_worker_links_structured_purpose_response_without_wellness_special_case(self):
        from continuity.intake import OwnerContext, admit_event
        self.checkin()
        class Question:
            def complete_structured(me, **kwargs):
                return SimpleNamespace(parsed=dict(message='About reservation details, what decision remains?',
                    citations=[dict(source='ledger', quote='reservation details')],
                    quality=dict(purpose='reservation', coverage=[dict(facet='decision', source='ledger', quote='reservation details')])))
        self.tick(model=Question())
        self.s.configure('identities',{'chat:owner':'person'})
        item=next(i for i in self.s.items() if i['kind']=='checkin')
        text='For '+item['id']+', I choose the earlier reservation.'
        event=admit_event(self.s,OwnerContext.local('chat','owner'),dict(id='purpose-reply',text=text));self.s.ingest(event)
        class Response:
            def complete_structured(me, **kwargs):
                self.prompts.append(kwargs['instructions'])
                packet=json.loads(kwargs['input'][0]['text'])
                self.assertEqual(next(i for i in packet['related'] if i['id']==item['id'])['purpose_requirements']['purpose'],'reservation')
                return SimpleNamespace(parsed={'proposals':[dict(kind='pending_response',owner='human',source=event['id'],quote=text,title='Reservation choice',target=item['id'],target_revision=0,
                    response_quality=dict(purpose='reservation',coverage=[dict(facet='decision',source=event['id'],quote=text)]))]})
        report=self.tick(now=time.time(),model=Response())
        jobs=self.s.jobs()
        with self.s.tx() as c:
            self.assertEqual(c.execute('SELECT state FROM checkin_days WHERE item=?',(item['id'],)).fetchone()[0],'answered', (report,jobs))
            self.assertEqual(c.execute('SELECT kind FROM checkin_responses WHERE event=?',(event['id'],)).fetchone()[0],'reservation')
        from continuity.lifecycle import Lifecycle
        state=Lifecycle(self.s).snapshot(item['id'],'person')['followthrough']
        self.assertTrue(state['responded'])
        self.assertTrue(state['resolved'])
        self.assertEqual(state['closure_condition'],'purpose_coverage')
        self.assertEqual(state['next_action'],'close')
        self.assertIn('pending_response',self.prompts[0])
        self.assertIn('response_quality',self.prompts[0])
        self.assertEqual(next(i for i in self.s.items() if i['kind']=='commitment')['state'],'open')

    def test_old_receipt_without_subject_metadata_cannot_gain_new_attempt(self):
        from continuity.outbox import Outbox
        item=self.extract()
        outbox=Outbox(self.s)
        oid=outbox.prepare(item)
        self.assertTrue(outbox.dispatch(oid,lambda key:dict(receipt='legacy-receipt'),43200))
        original=outbox.get(oid)
        self.tick()
        self.assertEqual(self.calls,[])
        self.assertEqual(outbox.get(oid),original)
        with self.s.tx() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM outbox').fetchone()[0],1)

    def test_legacy_review_notice_receipt_is_not_renewed_by_contract_metadata(self):
        import hashlib
        from continuity.outbox import Outbox
        item=self.extract()
        body=json.dumps(dict(created=43200,revision=0,decision='owner review due'))
        with self.s.tx() as c: c.execute('INSERT INTO artifacts VALUES(?,?,?)',('legacy-review',item['id'],body))
        oid=Outbox(self.s).prepare(item,dict(artifact='legacy-review',hash=hashlib.sha256(body.encode()).hexdigest(),target='test:owner',created=43200))
        # Historical fixture: current admission correctly refuses to create such a generic notice.
        with self.s.tx() as c: c.execute("UPDATE outbox SET state='acknowledged',receipt='old-review-receipt' WHERE id=?",(oid,))
        before=Outbox(self.s).get(oid)
        self.tick()
        self.assertEqual(self.calls,[])
        self.assertEqual(Outbox(self.s).get(oid),before)
        with self.s.tx() as c: self.assertEqual(c.execute('SELECT count(*) FROM outbox').fetchone()[0],1)

    def test_wellbeing_example_rejects_food_shopping_or_uncovered_purpose(self):
        sample=json.loads((Path(__file__).parents[1]/'docs/examples/wellbeing-quality.json').read_text())
        cases=[('shopping','purchase'),('food','meal'),('wellbeing','logistics')]
        for index,(purpose,facet) in enumerate(cases):
            with self.subTest(purpose=purpose,facet=facet):
                self.s=Store(Path(self.tmp.name)/('quality-'+str(index)))
                for key,value in dict(mode='active',timezone='UTC',allow_delivery=True,delivery_target='test:owner').items():self.s.configure(key,value)
                config=self.checkin(purpose='wellbeing',facet='rest',quote='buy groceries after work')
                config['quality_contract']=sample;self.s.configure('checkin',config)
                class Model:
                    def complete_structured(me,**kwargs):
                        return SimpleNamespace(parsed=dict(message='About buy groceries after work, what is missing?',
                            citations=[dict(source='ledger',quote='buy groceries after work')],
                            quality=dict(purpose=purpose,coverage=[dict(facet=facet,source='ledger',quote='buy groceries after work')])))
                self.tick(model=Model())
                self.assertEqual(self.calls,[])
                with self.s.tx() as c:
                    self.assertEqual(c.execute('SELECT state FROM checkin_days').fetchone()[0],'blocked')
                    self.assertEqual(c.execute('SELECT count(*) FROM outbox').fetchone()[0],0)

    def test_unknown_reminder_never_retries_after_review_or_restart(self):
        from continuity.outbox import Outbox
        self.extract()
        calls=[]
        def uncertain(key):
            calls.append(key)
            raise TimeoutError('synthetic transport uncertainty')
        with self.assertLogs('continuity.outbox',level='WARNING'):
            Worker(self.s,None,clock=lambda:43200,transport=uncertain).tick(43200)
        oid=calls[0];before=Outbox(self.s).get(oid)
        self.s=Store(self.s.root)
        Worker(self.s,None,clock=lambda:129600,transport=uncertain).tick(129600)
        self.assertEqual(len(calls),1)
        self.assertEqual(Outbox(self.s).get(oid),before)
        self.assertEqual(before['state'],'unknown')
        with self.s.tx() as c:self.assertEqual(c.execute('SELECT count(*) FROM outbox').fetchone()[0],1)

    def test_new_ambiguous_request_during_response_model_blocks_automatic_coverage(self):
        from continuity.intake import OwnerContext,admit_event
        self.checkin()
        class Question:
            def complete_structured(me,**kwargs):
                return SimpleNamespace(parsed=dict(message='About reservation details, what decision remains?',citations=[dict(source='ledger',quote='reservation details')],quality=dict(purpose='reservation',coverage=[dict(facet='decision',source='ledger',quote='reservation details')])))
        self.tick(model=Question())
        self.s.configure('identities',{'cli:owner':'person'})
        item=next(i for i in self.s.items() if i['kind']=='checkin')
        text='I choose the earlier reservation.'
        event=admit_event(self.s,OwnerContext.local('cli','owner'),dict(id='ambiguous-response',text=text));self.s.ingest(event)
        class Response:
            def complete_structured(me,**kwargs):
                self.s.create_item('checkin','person','Another question','another-question','quote')
                return SimpleNamespace(parsed={'proposals':[dict(kind='pending_response',owner='human',source=event['id'],quote=text,title='Choice',target=item['id'],target_revision=0,response_quality=dict(purpose='reservation',coverage=[dict(facet='decision',source=event['id'],quote=text)]))]})
        self.tick(now=time.time(),model=Response())
        with self.s.tx() as c:
            self.assertNotEqual(c.execute('SELECT state FROM checkin_days WHERE item=?',(item['id'],)).fetchone()[0],'answered')
            self.assertEqual(c.execute('SELECT count(*) FROM checkin_responses').fetchone()[0],0)

    def test_latest_actual_delivery_orders_receipts_not_attempt_creation(self):
        from continuity.outbox import Outbox
        from continuity.followthrough import activity
        first=self.s.create_item('commitment','person','First','first','quote')
        second=self.s.create_item('commitment','person','Second','second','quote')
        outbox=Outbox(self.s);a=outbox.prepare(first);b=outbox.prepare(second)
        self.assertTrue(outbox.dispatch(b,lambda key:dict(receipt='second-receipt'),43200))
        self.assertTrue(outbox.dispatch(a,lambda key:dict(receipt='first-receipt'),43201))
        self.s=Store(self.s.root)
        self.assertEqual(activity(self.s)['latest_delivery']['id'],a)
        self.assertEqual(activity(self.s)['delivery_order'],'acknowledgement_time_with_legacy_fallback')
        with self.s.tx() as c: before=c.execute('SELECT body FROM artifacts WHERE id=?',('delivery-receipt:'+a,)).fetchone()[0]
        self.assertFalse(Outbox(self.s).dispatch(a,lambda key:dict(receipt='replacement'),43202))
        with self.s.tx() as c: self.assertEqual(c.execute('SELECT body FROM artifacts WHERE id=?',('delivery-receipt:'+a,)).fetchone()[0],before)

    def test_legacy_explicit_reminder_is_enriched_in_worker_without_losing_review_or_identity(self):
        from continuity.intake import OwnerContext,admit_event
        self.s.configure('identities',{'cli:owner':'person'});self.s.configure('allow_model',True)
        self.s.configure('mode','prepare')
        text='Please remind me to review the reservation.'
        event=admit_event(self.s,OwnerContext.local('cli','owner'),dict(id='legacy-request',text=text));self.s.ingest(event)
        now=(int(time.time())//86400+1)*86400+43200
        class OldModel:
            def complete_structured(me,**kwargs):
                return SimpleNamespace(parsed={'proposals':[dict(kind='commitment',owner='human',source=event['id'],quote=text,title='Review reservation',next_review=now)]})
        self.tick(now=time.time(),model=OldModel())
        item=self.s.items()[0]
        self.s.configure('mode','active')
        class Enrichment:
            def complete_structured(me,**kwargs):
                return SimpleNamespace(parsed=dict(goal='Review reservation details',closure='owner_confirmation',contact='explicit_reminder',purpose='reservation',subject='reservation-review'))
        report=self.tick(now=now,model=Enrichment())
        self.assertEqual(report['acknowledged'],1)
        fresh=self.s.items()[0]
        self.assertEqual(fresh['id'],item['id'])
        self.assertEqual(fresh['review'],now+86400)
        self.assertEqual(len(self.s.items()),1)
        self.assertEqual(fresh['state'],'open')
        self.s=Store(self.s.root);self.tick(now=now+86400,model=Enrichment())
        self.assertEqual(len(self.calls),1)

    def test_ordinary_due_item_has_no_generic_notice_without_integrations(self):
        item = self.s.create_item('commitment', 'person', 'Reservation', 'request',
                                  'Arrange a reservation', review=43200)
        report = self.tick()
        self.assertEqual(report['reviewed'], 1)
        self.assertEqual(self.calls, [])
        fresh = self.s.items()[0]
        self.assertEqual(fresh['state'], 'open')
        self.assertEqual(fresh['review'], 129600)
        with self.s.tx() as c:
            body = json.loads(c.execute('SELECT body FROM artifacts WHERE item=?', (item['id'],)).fetchone()[0])
        self.assertEqual(body['next_action'], 'wait')
        self.assertEqual(body['contact_reason'], 'no_meaningful_contact')
