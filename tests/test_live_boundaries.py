import inspect,json,unittest
from types import SimpleNamespace
from unittest.mock import patch
from continuity import adapters,delivery

class LiveBoundaryTests(unittest.TestCase):
    def test_calendar_argv_matches_discovered_schema(self):
        mapping=dict(action='GOOGLECALENDAR_FIND_EVENT',account='selected',calendar_id='primary',time_min='2026-11-02T00:00:00Z',time_max='2026-11-03T00:00:00Z',max_results=5)
        response=SimpleNamespace(returncode=0,stdout=json.dumps({'successful':True,'data':{'items':[]}}))
        with patch('continuity.adapters.subprocess.run',return_value=response) as run:
            adapters.ComposioReadAdapter(mapping).read()
        argv=run.call_args.args[0]; payload=json.loads(argv[argv.index('--data')+1])
        self.assertEqual(payload['time_min'],mapping['time_min'])
        self.assertEqual(payload['time_max'],mapping['time_max'])
        self.assertNotIn('timeMin',payload)
    def test_transport_retains_exact_receipt(self):
        response=SimpleNamespace(returncode=0,stdout=json.dumps({'success':True,'platform':'whatsapp','chat_id':'owner','message_id':'message-one'}))
        with patch('continuity.delivery.subprocess.run',return_value=response):
            self.assertEqual(delivery.HermesSend('whatsapp:owner')('notice'),{'receipt':'message-one'})
    def test_ambiguous_success_and_foreign_destination_rejected(self):
        for result in [{'success':True},{'success':True,'platform':'whatsapp','chat_id':'other','message_id':'m'}]:
            with self.subTest(result=result),patch('continuity.delivery.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(result))):
                with self.assertRaises(ValueError):delivery.HermesSend('whatsapp:owner')('notice')
    def test_cli_locations_are_not_personal_deployment_paths(self):
        self.assertNotIn('/home/',inspect.getsource(delivery))
        self.assertNotIn('/home/',inspect.getsource(adapters))
