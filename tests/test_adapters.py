import tempfile, unittest
from pathlib import Path
from continuity.adapters import MarkdownAdapter, HostReadAdapter, AdapterRegistry

class AdapterTests(unittest.TestCase):
    def test_markdown_scope_symlink_and_write(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'note.md').write_text('synthetic context'); (root/'link.md').symlink_to(root/'note.md')
            a=MarkdownAdapter(root); self.assertEqual(a.read('note.md')['text'],'synthetic context')
            for p in ('../note.md','link.md','/etc/passwd'):
                with self.assertRaises(ValueError): a.read(p)
            reg=AdapterRegistry(); reg.add('notes','selected',a)
            self.assertIs(reg.select('notes','selected','read'),a)
            with self.assertRaises(ValueError): reg.select('notes','selected','write')
            with self.assertRaises(ValueError): reg.select('calendar','all','read')
    def test_host_exact_mapping_recursive_validation(self):
        calls=[]
        mapping={'tool':'synthetic_calendar_read','args':{'calendar':'selected'},'schema':{'type':'object','required':['events'],'additionalProperties':False,'properties':{'events':{'type':'array','items':{'type':'object','required':['id'],'additionalProperties':False,'properties':{'id':{'type':'string'}}}}}}}
        def dispatch(tool,args): calls.append((tool,args)); return {'events':[{'id':'event'}]}
        a=HostReadAdapter(mapping,dispatch,describe=lambda name:{'annotations':{'readOnlyHint':True,'continuityScopedRead':{'tool':mapping['tool'],'args':mapping['args']}}}); self.assertEqual(a.read()['events'][0]['id'],'event')
        self.assertEqual(calls,[('synthetic_calendar_read',{'calendar':'selected'})])
        for result in ({'successful':True},{'events':[{'id':True}]},{'events':[{'id':'x','permission':'write'}]}):
            with self.assertRaises(ValueError): HostReadAdapter(mapping,lambda *a:result,describe=lambda name:{'annotations':{'readOnlyHint':True,'continuityScopedRead':{'tool':mapping['tool'],'args':mapping['args']}}}).read()
        with self.assertRaises(ValueError): a.read({'calendar':'other'})
        with self.assertRaises(ValueError): HostReadAdapter(dict(mapping,operation='write'),dispatch)
    def test_host_read_requires_host_capability_and_denies_mutating_selector(self):
        mapping={'tool':'synthetic_read','args':{'action':'delete'},'schema':{'type':'object','properties':{},'additionalProperties':False}}
        with self.assertRaises(ValueError): HostReadAdapter(mapping,lambda *a:self.fail('write dispatched'),describe=lambda name:{'annotations':{'readOnlyHint':True,'continuityScopedRead':{'tool':mapping['tool'],'args':mapping['args']}}}).read()
        mapping['args']={'action':'list'}
        with self.assertRaises(ValueError): HostReadAdapter(mapping,lambda *a:self.fail('unattested dispatched'),describe=lambda name:{}).read()
    def test_host_read_timeout_does_not_claim_completion(self):
        import threading
        wait=threading.Event()
        self.addCleanup(wait.set)
        mapping={'tool':'synthetic_read','args':{},'schema':{'type':'object','properties':{},'additionalProperties':False}}
        a=HostReadAdapter(mapping,lambda *args:wait.wait(1),describe=lambda name:{'annotations':{'readOnlyHint':True,'continuityScopedRead':{'tool':mapping['tool'],'args':mapping['args']}}},timeout=0.01)
        with self.assertRaises(TimeoutError): a.read()
        with self.assertRaises(TimeoutError): a.read()
