import json,unittest
from types import SimpleNamespace
from unittest.mock import patch
from continuity.adapters import ComposioReadAdapter

class CalendarEnvelopeTests(unittest.TestCase):
    def mapping(self):return dict(action='GOOGLECALENDAR_FIND_EVENT',account='selected',calendar_id='primary',time_min='2026-11-02T00:00:00Z',time_max='2026-11-03T00:00:00Z',max_results=5)
    def test_provider_nested_event_data_is_read_and_reduced(self):
        response={'successful':True,'data':{'event_data':{'event_data':[{'id':'one','summary':'Selected event','start':{'date':'2026-11-02'},'end':{'date':'2026-11-03'},'attendees':[{'email':'private@example.test'}]}],'nextPageToken':None}}}
        with patch('continuity.adapters.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(response))):
            result=ComposioReadAdapter(self.mapping()).read()
        self.assertEqual(result['events'][0]['summary'],'Selected event')
        self.assertNotIn('attendees',result['events'][0])
    def test_nested_pagination_does_not_silently_truncate(self):
        response={'successful':True,'data':{'event_data':{'event_data':[],'nextPageToken':'remaining'}}}
        with patch('continuity.adapters.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(response))):
            with self.assertRaisesRegex(ValueError,'incomplete'):ComposioReadAdapter(self.mapping()).read()
