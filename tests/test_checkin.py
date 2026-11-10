import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from continuity.store import Store
from continuity.extraction import Worker

NOW = 1793613600  # fixture timestamp, UTC daytime


class CheckinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = Store(Path(self.tmp.name) / 'data')
        for key, value in {'timezone': 'UTC', 'mode': 'active', 'allow_model': True,
                           'allow_delivery': True, 'delivery_target': 'telegram:fixture',
                           'checkin': {'enabled': True, 'principal': 'fixture', 'sources': {},
                                       'calendars': [], 'primary': 'ledger', 'allow_reads': True}}.items():
            self.s.configure(key, value)
        self.item = self.s.create_item('commitment', 'fixture', 'el documento pendiente', 'fixture:request', 'el documento pendiente')
        self.calls = []
        self.sent = []
        outer = self
        class Model:
            def complete_structured(self, **kwargs):
                outer.calls.append(kwargs)
                return SimpleNamespace(parsed={'message': 'Sobre el documento pendiente, ¿qué te ayudaría hoy?',
                                               'citations': [{'source': 'ledger', 'quote': 'el documento pendiente'}]})
        self.model = Model()

    def worker(self, transport=None):
        return Worker(self.s, self.model, clock=lambda: NOW, transport=transport or self.transport)

    def transport(self, key):
        from continuity.outbox import Outbox
        row = Outbox(self.s).get(key)
        with self.s.tx() as c:
            body = json.loads(c.execute('SELECT body FROM artifacts WHERE item=?', (row['item'],)).fetchone()[0])
        self.sent.append(body['message'])
        return {'receipt': 'fixture-receipt'}

    def test_ticker_delivers_contextual_message_once_with_durable_evidence(self):
        self.worker().tick(NOW)
        self.assertEqual(self.sent, ['Sobre el documento pendiente, ¿qué te ayudaría hoy?'])
        self.worker().tick(NOW)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.calls), 1)
        with self.s.tx() as c:
            day = dict(c.execute('SELECT * FROM checkin_days').fetchone())
            evidence = json.loads(day['evidence'])
        self.assertEqual(day['state'], 'acknowledged')
        self.assertEqual(evidence['ledger']['status'], 'OK')
        self.assertEqual(evidence['email']['status'], 'UNAVAILABLE')
        self.assertIn('observed_at', evidence['ledger'])
        self.assertEqual(len(evidence), 10)

    def test_unknown_consumes_day_even_after_restart(self):
        def uncertain(key):
            self.sent.append(key)
            raise TimeoutError('fixture transport')
        self.worker(uncertain).tick(NOW)
        self.s = Store(self.s.root)
        self.worker(uncertain).tick(NOW)
        self.assertEqual(len(self.sent), 1)
        with self.s.tx() as c:
            self.assertEqual(c.execute('SELECT state FROM checkin_days').fetchone()[0], 'unknown')
            self.assertEqual(c.execute("SELECT n FROM budgets WHERE kind='checkin_delivery'").fetchone()[0], 1)

    def test_revocation_during_model_blocks_delivery(self):
        model = self.model
        def revoked(**kwargs):
            result = model.complete_structured(**kwargs)
            self.s.configure('allow_model', False)
            return result
        self.model = SimpleNamespace(complete_structured=revoked)
        self.worker().tick(NOW)
        self.assertFalse(self.sent)

    def test_closed_during_model_never_delivered_or_resurrected(self):
        model = self.model
        def closed(**kwargs):
            result = model.complete_structured(**kwargs)
            self.s.transition(self.item['id'], 0, 'done')
            return result
        self.model = SimpleNamespace(complete_structured=closed)
        self.worker().tick(NOW)
        self.assertFalse(self.sent)
        self.assertEqual(self.s.items()[0]['state'], 'done')

    def test_read_adapter_is_exercised_twice_and_stale_draft_is_invalidated(self):
        from continuity.adapters import AdapterRegistry, MarkdownAdapter
        from continuity.checkin import Checkins
        root = Path(self.tmp.name)
        (root / 'email.md').write_text('A relevant email before preparation')
        self.s.configure('adapters', [{'type':'markdown', 'role':'mail', 'scope':'selected', 'root':str(root)}])
        config = self.s.setting('checkin')
        config['sources'] = {'email':[{'role':'mail', 'scope':'selected', 'path':'email.md'}]}
        self.s.configure('checkin', config)
        registry = AdapterRegistry(); registry.add('mail', 'selected', MarkdownAdapter(root))
        self.s.configure('mode', 'prepare')
        Checkins(self.s, self.model, registry, lambda: NOW, self.transport).tick(NOW)
        (root / 'email.md').write_text('Changed after preparation: user has a new constraint')
        self.s.configure('mode', 'active')
        Checkins(self.s, self.model, registry, lambda: NOW, self.transport).tick(NOW)
        self.assertFalse(self.sent, 'must not deliver the previously drafted question using changed context')

    def test_maintenance_response_does_not_cover_wellbeing_and_followups_do_not_reopen(self):
        from continuity.checkin import Checkins
        self.worker().tick(NOW)
        self.s.transition(self.item['id'], 0, 'done')
        with self.s.tx() as c:
            day = c.execute('SELECT id FROM checkin_days').fetchone()[0]
            event = {'id':'human:fixture', 'role':'human', 'principal':'fixture', 'text':'repair the plugin', 'observed_at':NOW}
            c.execute('INSERT INTO events VALUES(?,?)', (event['id'],json.dumps(event)))
        engine = Checkins(self.s, clock=lambda: NOW)
        engine.record_response(day, event['id'], 'maintenance', [])
        with self.s.tx() as c:
            self.assertNotEqual(c.execute('SELECT state FROM checkin_days').fetchone()[0], 'answered')
        with self.assertRaises(ValueError):
            engine.record_response(day, event['id'], 'wellbeing', [{'item':self.item['id'], 'revision':1}])
        self.assertEqual(self.s.items()[0]['state'], 'done')

    def test_cli_checkin_configuration_is_explicit_and_portable(self):
        import argparse
        from continuity.cli import setup_parser, Controller
        parser=argparse.ArgumentParser(); setup_parser(parser)
        path=Path(self.tmp.name)/'checkin.json'
        path.write_text(json.dumps(self.s.setting('checkin')))
        controller=Controller(Path(self.tmp.name), SimpleNamespace())
        controller.handle(parser.parse_args(['checkin-config', str(path)]))
        self.assertTrue(controller.store.setting('checkin')['enabled'])

    def host_sources(self, sources):
        from continuity.adapters import AdapterRegistry, HostReadAdapter
        registry = AdapterRegistry(); mappings=[]; calls=[]
        for name, read in sources.items():
            mapping={'tool':'fixture_'+name, 'args':{}, 'schema':{'type':'object', 'additionalProperties':False,
                'properties':{'events':{'type':'array','maxItems':10,'items':{'type':'object','additionalProperties':False,
                    'required':['start','end'],'properties':{'start':{'type':'number'},'end':{'type':'number'}}}},
                    'window':{'type':'object','additionalProperties':False,'required':['start','end'],'properties':{'start':{'type':'number'},'end':{'type':'number'}}},
                    'entries':{'type':'array','maxItems':10,'items':{'type':'object','additionalProperties':False,'properties':{'completed':{'type':'boolean'},'name':{'type':'string'}}}}}}}
            def dispatch(tool,args,read=read):
                calls.append(tool)
                return read()
            def describe(tool,m=mapping):
                return {'annotations':{'readOnlyHint':True,'continuityScopedRead':{'tool':tool,'args':m['args']}}}
            registry.add(name,'selected',HostReadAdapter(mapping,dispatch,describe))
            mappings.append({'type':'host-read','role':name,'scope':'selected','mapping':mapping})
        self.s.configure('adapters',mappings)
        config=self.s.setting('checkin')
        config['sources']={n:[{'role':n,'scope':'selected'}] for n in sources}
        config['calendars']=[n for n in sources if n.startswith('calendar_')]
        self.s.configure('checkin',config)
        return registry,calls

    def test_calendar_gap_selection_and_unavailable_calendar_fail_closed(self):
        from continuity.checkin import Checkins
        registry,calls=self.host_sources({'calendar_work':lambda:{'events':[{'start':NOW-600,'end':NOW+1800}], 'window':{'start':NOW-86400,'end':NOW+86400}}})
        Checkins(self.s,self.model,registry,lambda:NOW,self.transport).tick(NOW)
        self.assertFalse(self.calls); self.assertFalse(self.sent)
        with self.s.tx() as c:
            self.assertEqual(c.execute('SELECT due FROM checkin_days').fetchone()[0],NOW+2700)
        # New ticker at the free interval, not a separate scheduled job.
        Checkins(self.s,self.model,registry,lambda:NOW+2700,self.transport).tick(NOW+2700)
        self.assertEqual(len(self.sent),1)
        self.assertGreaterEqual(len(calls),3)

    def test_unavailable_required_calendar_prevents_synthesis(self):
        from continuity.checkin import Checkins
        def failed(): raise TimeoutError('fixture source down')
        registry,calls=self.host_sources({'calendar_work':failed})
        Checkins(self.s,self.model,registry,lambda:NOW,self.transport).tick(NOW)
        self.assertFalse(self.calls); self.assertFalse(self.sent)
        with self.s.tx() as c:
            evidence=json.loads(c.execute('SELECT evidence FROM checkin_days').fetchone()[0])
        self.assertEqual(evidence['calendar_work']['status'],'UNAVAILABLE')

    def test_training_presets_are_not_completed_training(self):
        from continuity.checkin import Checkins
        registry,calls=self.host_sources({'completed_training':lambda:{'entries':[{'completed':True,'name':'completed row'},{'completed':False,'name':'preset row'}]}})
        Checkins(self.s,self.model,registry,lambda:NOW,self.transport).tick(NOW)
        packet=json.loads(self.calls[0]['input'][0]['text'])
        source=packet['sources']['completed_training']
        self.assertEqual(source['status'],'PARTIAL')
        self.assertIn('completed row',json.dumps(source['content']))
        self.assertNotIn('preset row',json.dumps(source['content']))

    def test_seven_day_notes_are_read_with_real_markdown_adapter(self):
        from continuity.adapters import AdapterRegistry, MarkdownAdapter
        from continuity.checkin import Checkins
        from datetime import datetime,timedelta,timezone
        root=Path(self.tmp.name); day=datetime.fromtimestamp(NOW,timezone.utc).date()
        for i in range(7): (root/((day-timedelta(days=i)).isoformat()+'.md')).write_text('fixture daily '+str(i))
        self.s.configure('adapters',[{'type':'markdown','role':'daily','scope':'selected','root':str(root)}])
        config=self.s.setting('checkin'); config['sources']={'daily_notes':[{'role':'daily','scope':'selected','path':'{date}.md'}]}
        self.s.configure('checkin',config)
        registry=AdapterRegistry(); registry.add('daily','selected',MarkdownAdapter(root))
        Checkins(self.s,self.model,registry,lambda:NOW,self.transport).tick(NOW)
        packet=json.loads(self.calls[0]['input'][0]['text'])
        self.assertEqual(len(packet['sources']['daily_notes']['references']),7)
        self.assertEqual(packet['sources']['daily_notes']['status'],'OK')

    def test_question_validation_and_unknown_model_fields_fail_closed(self):
        from continuity.checkin import Checkins
        for result in ({'message':'Hola, ¿cómo estás?', 'citations':[]},
                       {'message':'el documento pendiente. Uno. Dos. ¿Tres?', 'citations':[{'source':'ledger','quote':'el documento pendiente'}]},
                       {'message':'el documento pendiente ¿MEDIA:attachment?', 'citations':[{'source':'ledger','quote':'el documento pendiente'}]},
                       {'message':'el documento pendiente ¿qué sigue?', 'citations':[{'source':'ledger','quote':'el documento pendiente'}], 'destination':'other'}):
            with self.subTest(result=result):
                model=SimpleNamespace(complete_structured=lambda **kw:SimpleNamespace(parsed=result))
                with self.assertRaises(ValueError): Checkins(self.s,model,clock=lambda:NOW,transport=self.transport).tick(NOW)
                with self.s.tx() as c: c.execute("DELETE FROM budgets WHERE kind='checkin_model'")
        self.assertFalse(self.sent)

    def test_paused_shadow_and_no_read_consent_do_not_read_or_call_model(self):
        from continuity.checkin import Checkins
        for key,value in [('paused',True),('mode','shadow')]:
            before=self.s.setting(key)
            self.s.configure(key,value)
            self.assertEqual(Checkins(self.s,self.model,clock=lambda:NOW,transport=self.transport).tick(NOW),0)
            self.s.configure(key,before)
        config=self.s.setting('checkin'); config['allow_reads']=False; self.s.configure('checkin',config)
        self.assertEqual(Checkins(self.s,self.model,clock=lambda:NOW,transport=self.transport).tick(NOW),0)
        self.assertFalse(self.calls); self.assertFalse(self.sent)

    def test_accepted_wellbeing_response_is_durable_not_inferred_from_silence(self):
        from continuity.checkin import Checkins
        self.worker().tick(NOW)
        with self.s.tx() as c:
            day=c.execute('SELECT id FROM checkin_days').fetchone()[0]
            event={'id':'human:answer','role':'human','principal':'fixture','text':'Necesito dividir el documento en pasos','observed_at':NOW}
            c.execute('INSERT INTO events VALUES(?,?)',(event['id'],json.dumps(event)))
        engine=Checkins(self.s,clock=lambda:NOW)
        self.assertTrue(engine.record_response(day,event['id'],'wellbeing',[{'item':self.item['id'],'revision':0}]))
        self.assertFalse(engine.record_response(day,event['id'],'wellbeing',[{'item':self.item['id'],'revision':0}]))
        with self.s.tx() as c:
            self.assertEqual(c.execute('SELECT state FROM checkin_days').fetchone()[0],'acknowledged')
            self.assertEqual(len(json.loads(c.execute('SELECT followups FROM checkin_responses').fetchone()[0])),1)
        self.assertEqual(self.s.items()[0]['state'],'open')

    def test_revoke_reenable_between_artifact_and_outbox_does_not_adopt_old_authority(self):
        from unittest.mock import patch
        from continuity.outbox import Outbox
        from continuity.checkin import Checkins
        original=Outbox.prepare
        def raced(outbox,item,notice=None):
            self.s.configure('allow_delivery',False)
            self.s.configure('allow_delivery',True)
            return original(outbox,item,notice)
        with patch.object(Outbox,'prepare',raced):
            Checkins(self.s,self.model,clock=lambda:NOW,transport=self.transport).tick(NOW)
        self.assertFalse(self.sent, 'old preparation must not gain re-enabled authority')

    def test_incomplete_conversation_history_is_not_reported_as_complete(self):
        from continuity.checkin import Checkins
        Checkins(self.s,self.model,clock=lambda:NOW,transport=self.transport).tick(NOW)
        packet=json.loads(self.calls[0]['input'][0]['text'])
        self.assertNotEqual(packet['sources']['conversations']['status'],'OK')

    def test_selected_calendar_cannot_be_omitted_from_eligibility(self):
        from continuity.checkin import validate_config
        config=self.s.setting('checkin'); config['sources']={'calendar_work':[{'role':'agenda','scope':'selected'}]}
        with self.assertRaises(ValueError): validate_config(config)

    def test_revalidation_that_finishes_after_quiet_hours_cannot_send(self):
        from continuity.checkin import Checkins
        clock=[NOW]; reads=[]
        def agenda():
            reads.append(1)
            if len(reads)==2: clock[0]=NOW+12*3600
            return {'events':[], 'window':{'start':NOW-86400,'end':NOW+86400}}
        registry,_=self.host_sources({'calendar_work':agenda})
        Checkins(self.s,self.model,registry,lambda:clock[0],self.transport).tick(NOW)
        self.assertFalse(self.sent)

    def test_checkin_ignores_retired_daily_model_budget(self):
        with self.s.tx() as c: c.execute("INSERT INTO budgets VALUES('model','2026-11-02',10)")
        self.worker().tick(NOW)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(len(self.sent),1)

    def test_wellbeing_association_during_model_does_not_get_overwritten(self):
        from continuity.checkin import Checkins
        model=self.model
        def answered(**kwargs):
            result=model.complete_structured(**kwargs)
            with self.s.tx() as c:
                day=c.execute('SELECT id FROM checkin_days').fetchone()[0]
                event={'id':'human:during','role':'human','principal':'fixture','text':'Ya respondí','observed_at':NOW}
                c.execute('INSERT INTO events VALUES(?,?)',(event['id'],json.dumps(event)))
            Checkins(self.s,clock=lambda:NOW).record_response(day,event['id'],'wellbeing',[])
            return result
        self.model=SimpleNamespace(complete_structured=answered)
        self.worker().tick(NOW)
        with self.s.tx() as c: self.assertEqual(c.execute('SELECT state FROM checkin_days').fetchone()[0],'planned')
        self.assertFalse(self.sent)

    def test_source_read_plan_is_bounded(self):
        from continuity.checkin import validate_config
        config=self.s.setting('checkin')
        config['sources']={'daily_notes':[{'role':'daily','scope':'selected','path':'{date}.md'} for _ in range(7)]}
        with self.assertRaises(ValueError): validate_config(config)

    def test_model_outage_isolated_from_ticker(self):
        def failed(**kwargs): raise RuntimeError('fixture provider unavailable')
        self.model=SimpleNamespace(complete_structured=failed)
        report=self.worker().tick(NOW)
        self.assertEqual(report['blocked'],1)
        self.assertFalse(self.sent)

    def test_response_during_transport_preserved_separately_from_receipt(self):
        from continuity.checkin import Checkins
        def answered(key):
            with self.s.tx() as c:
                day=c.execute('SELECT id FROM checkin_days').fetchone()[0]
                event={'id':'human:inflight','role':'human','principal':'fixture','text':'A response','observed_at':NOW}
                c.execute('INSERT INTO events VALUES(?,?)',(event['id'],json.dumps(event)))
            Checkins(self.s,clock=lambda:NOW).record_response(day,event['id'],'wellbeing',[])
            return {'receipt':'fixture-positive'}
        self.worker(answered).tick(NOW)
        with self.s.tx() as c:
            self.assertEqual(c.execute('SELECT state FROM checkin_days').fetchone()[0],'acknowledged')
            self.assertEqual(c.execute('SELECT state FROM outbox').fetchone()[0],'acknowledged')

    def test_rolling_calendar_advances_without_reconfiguring_mapping(self):
        from continuity.adapters import DayCalendarAdapter
        from unittest.mock import patch
        clock=[NOW]; windows=[]
        adapter=DayCalendarAdapter({'action':'GOOGLECALENDAR_FIND_EVENT','account':'fixture','calendar_id':'fixture','timezone':'UTC','max_results':10},clock=lambda:clock[0])
        def run(argv,**kwargs):
            windows.append(json.loads(argv[-1]))
            return SimpleNamespace(returncode=0,stdout=json.dumps({'successful':True,'data':{'items':[]}}))
        with patch('continuity.adapters.subprocess.run',run), patch('continuity.adapters.COMPOSIO','/fixture/composio'):
            adapter.read(); clock[0]+=86400; adapter.read()
        self.assertNotEqual(windows[0]['time_min'],windows[1]['time_min'])
        self.assertEqual(windows[0]['time_max'],windows[1]['time_min'])

    def test_operator_command_reader_executes_fixed_argv_with_relative_windows(self):
        from continuity.adapters import CommandReadAdapter
        import hashlib,sys
        from unittest.mock import patch
        executable=str(Path(sys.executable).resolve())
        mapping={'executable':executable,'sha256':hashlib.sha256(Path(executable).read_bytes()).hexdigest(),
            'args':['fixture-reader','{day_start}','{day_end}','{week_start}'], 'timezone':'UTC',
            'approval':'operator-reviewed-read-only','timeout':5,
            'schema':{'type':'object','additionalProperties':False,'required':['text'],'properties':{'text':{'type':'string'}}}}
        adapter=CommandReadAdapter(mapping,clock=lambda:NOW)
        with patch('continuity.adapters.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='{"text":"actual fixture result"}')) as run:
            self.assertEqual(adapter.read()['text'],'actual fixture result')
            self.assertFalse(run.call_args.kwargs['shell'])
            self.assertNotIn('{day_start}',run.call_args.args[0])
            self.assertEqual(run.call_args.args[0][0],executable)

    def test_timing_adapts_to_imminent_event_and_recent_human_thread(self):
        from continuity.checkin import Checkins
        engine=Checkins(self.s,clock=lambda:NOW)
        config=self.s.setting('checkin'); epoch=self.s.setting('epoch')
        evidence,_=engine.collect(config,epoch,NOW)
        evidence['conversations']['content']=[{'observed_at':NOW-60}]
        self.assertEqual(engine.due(config,evidence,NOW),NOW-60+1800)
        config['calendars']=['calendar_work']
        evidence['calendar_work'].update(status='OK',content=[{'events':[{'start':NOW+600,'end':NOW+1800}], 'window':{'start':NOW-86400,'end':NOW+86400}}])
        self.assertEqual(engine.due(config,evidence,NOW),NOW+2700)

    def test_at_most_one_question(self):
        from continuity.checkin import validate_message
        evidence={'ledger':{'status':'OK','content':['el documento pendiente']}}
        with self.assertRaises(ValueError): validate_message({'message':'el documento pendiente ¿Cómo vas? ¿Qué sigue?', 'citations':[{'source':'ledger','quote':'el documento pendiente'}]},evidence)

    def test_fixed_reader_really_acquires_fixture_content_without_shell(self):
        from continuity.adapters import CommandReadAdapter
        import hashlib,sys
        executable=str(Path(sys.executable).resolve()); source=Path(self.tmp.name)/'history.json'
        source.write_text(json.dumps({'text':'Human fixture conversation: el documento pendiente'}))
        adapter=CommandReadAdapter({'executable':executable,'sha256':hashlib.sha256(Path(executable).read_bytes()).hexdigest(),
            'args':['-c','import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())',str(source)],
            'timezone':'UTC','approval':'operator-reviewed-read-only','timeout':5,
            'schema':{'type':'object','additionalProperties':False,'required':['text'],'properties':{'text':{'type':'string'}}}},clock=lambda:NOW)
        self.assertIn('Human fixture conversation',adapter.read()['text'])

    def test_no_unconditional_nine_oclock_delivery(self):
        from continuity.checkin import Checkins
        from datetime import datetime,timezone
        engine=Checkins(self.s,clock=lambda:NOW)
        config=self.s.setting('checkin'); evidence,_=engine.collect(config,self.s.setting('epoch'),NOW)
        nine=datetime.fromtimestamp(NOW,timezone.utc).replace(hour=9,minute=0,second=0).timestamp()
        self.assertGreater(engine.due(config,evidence,nine),nine)

    def test_send_uses_validated_question_not_draft_notice(self):
        from unittest.mock import patch
        from continuity.checkin import Checkins
        with patch('continuity.delivery.subprocess.run') as run:
            run.return_value=SimpleNamespace(returncode=0,stdout=json.dumps({'success':True,'platform':'telegram','chat_id':'fixture','message_id':'fixture-positive'}))
            Checkins(self.s,self.model,clock=lambda:NOW).tick(NOW)
            self.assertEqual(run.call_args.kwargs['input'],'Sobre el documento pendiente, ¿qué te ayudaría hoy?')
            self.assertFalse(run.call_args.kwargs['shell'])
            self.assertEqual(run.call_args.args[0][1:4],['send','--to','telegram:fixture'])
