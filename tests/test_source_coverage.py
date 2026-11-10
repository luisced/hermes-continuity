import hashlib,json,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from continuity.adapters import AdapterRegistry,CommandReadAdapter
from continuity.checkin import Checkins
from continuity.store import Store

class SourceCoverageTests(unittest.TestCase):
 def test_partial_reader_coverage_is_preserved_and_usable_without_fake_completeness(self):
  with tempfile.TemporaryDirectory() as d:
   s=Store(Path(d)/'state')
   schema={'type':'object','additionalProperties':False,'required':['coverage','observations'],'properties':{'coverage':{'type':'string','enum':['OK','PARTIAL']},'observations':{'type':'array','items':{'type':'string'}}}}
   mapping={'executable':sys.executable,'sha256':hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),'args':[],'timezone':'UTC','approval':'operator-reviewed-read-only','timeout':3,'schema':schema}
   for key,value in {'mode':'prepare','allow_model':True,'timezone':'UTC','adapters':[{'type':'command-read','role':'facts','scope':'selected','mapping':mapping}],'checkin':{'enabled':True,'allow_reads':True,'principal':'owner','primary':'ledger','calendars':[],'sources':{'ledger':[{'role':'facts','scope':'selected'}]}}}.items():s.configure(key,value)
   registry=AdapterRegistry();registry.add('facts','selected',CommandReadAdapter(mapping))
   calls=[]
   def complete(**kwargs):
    calls.append(kwargs)
    return SimpleNamespace(parsed={'message':'Sobre el viaje anunciado, ¿qué te ayudaría hoy?','citations':[{'source':'ledger','quote':'el viaje anunciado'}]})
   result=SimpleNamespace(returncode=0,stdout=json.dumps({'coverage':'PARTIAL','observations':['el viaje anunciado']}))
   now=1793613600
   engine=Checkins(s,SimpleNamespace(complete_structured=complete),registry,lambda:now)
   with patch('continuity.adapters.subprocess.run',return_value=result):
    evidence,_=engine.collect(engine.config(),s.setting('epoch'),now)
    self.assertEqual(evidence['ledger']['status'],'PARTIAL')
    engine.tick(now)
   self.assertEqual(len(calls),1)
   with s.tx() as c:self.assertIsNotNone(c.execute('select draft from checkin_days').fetchone()[0])
