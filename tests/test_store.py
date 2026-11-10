import tempfile, unittest
from concurrent.futures import ThreadPoolExecutor
from continuity.store import Store, Stale

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.s=Store(self.tmp.name+'/data')
    def tearDown(self): self.tmp.cleanup()
    def test_concurrent_dedupe_and_claim(self):
        with ThreadPoolExecutor(8) as p:
            ids=list(p.map(lambda _:self.s.enqueue('extract',{'text':'synthetic'},'same',0),range(20)))
        self.assertEqual(len(set(ids)),1)
        with ThreadPoolExecutor(8) as p:
            claims=list(p.map(lambda n:self.s.claim(str(n),0,10),range(8)))
        self.assertEqual(sum(c is not None for c in claims),1)
    def test_expiry_fences_and_retries(self):
        self.s.enqueue('extract',{},'a',0); a=self.s.claim('a',0,10); b=self.s.claim('b',11,10)
        with self.assertRaises(Stale): self.s.finish(a,12)
        self.s.fail(b,12,'synthetic'); c=self.s.claim('c',100,10)
        self.s.fail(c,101,'synthetic'); self.assertIsNone(self.s.claim('d',200,10))
        self.assertEqual(self.s.jobs()[0]['state'],'blocked')
    def test_revision_pause_and_atomic_budget(self):
        item=self.s.create_item('commitment','human','synthetic','e','quote')
        self.s.transition(item['id'],0,'done')
        with self.assertRaises(Stale): self.s.transition(item['id'],0,'open')
        with ThreadPoolExecutor(8) as p:
            ok=list(p.map(lambda _:self.s.reserve('model','2026-01-01',10),range(30)))
        self.assertEqual(sum(ok),10)
        self.s.enqueue('extract',{},'a',0); a=self.s.claim('a',0,10); self.s.pause(True)
        with self.assertRaises(Stale): self.s.finish(a,1)
        self.assertIsNone(self.s.claim('a',20,10))
