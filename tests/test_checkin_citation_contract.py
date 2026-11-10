import unittest
from continuity.checkin import SCHEMA,validate_message
from continuity.adapters import validate_schema
class CitationContractTests(unittest.TestCase):
 def response(self,source):return {'message':'Me dijiste «terminé el proyecto». ¿Cómo te encuentras después de terminarlo?','citations':[{'source':source,'quote':'terminé el proyecto'}]}
 def test_provider_schema_rejects_nested_reference_source(self):
  with self.assertRaises(ValueError):validate_schema(self.response('conversations/native-history/123.0'),SCHEMA)
 def test_provider_schema_does_not_offer_secondary_memory_as_primary(self):
  with self.assertRaises(ValueError):validate_schema(self.response('memory'),SCHEMA)
 def test_exact_source_grounded_in_partial_read_is_accepted(self):
  evidence={'conversations':{'status':'PARTIAL','content':[{'text':'terminé el proyecto'}]}}
  self.assertEqual(validate_message(self.response('conversations'),evidence),self.response('conversations')['message'])
