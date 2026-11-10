import json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from continuity.store import Store
from continuity.intake import OwnerContext,admit_event
from continuity.extraction import Worker
from continuity.adapters import ComposioReadAdapter

class LateReviewTests(unittest.TestCase):
    def test_live_uniqueness_is_not_limited_to_model_context(self):
        with tempfile.TemporaryDirectory() as d:
            s=Store(Path(d)/'state');s.configure('identities',{'cli:owner':'principal'})
            s.create_item('commitment','principal','First task','first','first')
            for n in range(19):s.create_item('opportunity','principal','Interest',str(n),'quote')
            target=s.create_item('commitment','principal','Last task','last','last')
            text='I finished it.';event=admit_event(s,OwnerContext.local('cli','owner'),dict(id='closure',text=text));s.ingest(event)
            proposal=dict(kind='pending_close',owner='human',source='closure',quote=text,title='Done',target=target['id'],target_revision=0,closure='explicit',assertion=dict(quote=text,polarity='affirmed',certainty='certain',speaker='self',temporality='current',meaning='completion'))
            class M:
                def complete_structured(self,**kw):return SimpleNamespace(parsed={'proposals':[proposal]})
            self.assertEqual(Worker(s,M()).tick()['processed'],1)
            self.assertEqual(next(i for i in s.items() if i['id']==target['id'])['state'],'waiting')
    def test_calendar_text_fields_cannot_hide_nested_private_content(self):
        mapping=dict(action='GOOGLECALENDAR_FIND_EVENT',account='selected',calendar_id='primary',time_min='2026-11-02T00:00:00Z',time_max='2026-11-03T00:00:00Z',max_results=5)
        for field in ('summary','status'):
            result={'successful':True,'data':{'items':[{field:{'attendees':['private@example.test']}}]}}
            with self.subTest(field=field),patch('continuity.adapters.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(result))):
                with self.assertRaises(ValueError):ComposioReadAdapter(mapping).read()
