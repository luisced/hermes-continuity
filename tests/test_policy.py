import tempfile, unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
from continuity.store import Store, Stale
from continuity.policy import evaluate
from continuity.outbox import Outbox

class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.s=Store(self.tmp.name+'/data')
        self.item=self.s.create_item('commitment','person','Refund is pending','source','quote')
        self.now=datetime(2026,11,1,12,tzinfo=ZoneInfo('America/New_York')).timestamp()
    def test_shadow_quiet_closed_stale(self):
        snapshot={'mode':'shadow','timezone':'UTC','state':'open','revision':0,'expected_revision':0}
        self.assertEqual(evaluate(snapshot,self.now).reason,'shadow')
        snapshot.update(mode='active',timezone='America/New_York')
        self.assertTrue(evaluate(snapshot,self.now).allowed)
        self.assertEqual(evaluate(snapshot,self.now-4*3600).reason,'quiet hours')
        snapshot['revision']=1; self.assertFalse(evaluate(snapshot,self.now).allowed)
    def test_outbox_default_no_send_and_atomic_caps(self):
        o=Outbox(self.s); id=o.prepare(self.item); calls=[]
        self.assertFalse(o.dispatch(id,lambda key:calls.append(key),self.now)); self.assertFalse(calls)
        self.s.configure('mode','active'); self.s.configure('timezone','America/New_York')
        # Permission changes fence an old planned notice; explicitly plan anew.
        with self.s.tx() as c: c.execute('DELETE FROM outbox WHERE id=?',(id,))
        id=o.prepare(self.item)
        with ThreadPoolExecutor(8) as p: list(p.map(lambda _:o.dispatch(id,lambda key:{'receipt':'r'},self.now),range(8)))
        self.assertEqual(o.get(id)['state'],'acknowledged'); self.assertEqual(self.s.items()[0]['state'],'open')
        item2=self.s.create_item('commitment','person','another','s2','q2'); id2=o.prepare(item2)
        self.assertTrue(o.dispatch(id2,lambda _:{'receipt':'r2'},self.now+1))
        item3=self.s.create_item('commitment','person','third','s3','q3'); id3=o.prepare(item3)
        self.assertTrue(o.dispatch(id3,lambda _:{'receipt':'r3'},self.now+2))
        self.assertFalse(o.dispatch(id3,lambda _:self.fail('dedupe bypass'),self.now+3))
        with self.s.tx() as c:
            self.assertIsNone(c.execute("SELECT n FROM budgets WHERE kind='delivery'").fetchone())
    def test_crash_unknown_close_pause_forget(self):
        self.s.configure('mode','active'); o=Outbox(self.s); id=o.prepare(self.item)
        self.s.transition(self.item['id'],0,'done')
        self.assertFalse(o.dispatch(id,lambda _:self.fail('closed bypass'),self.now))
        new=self.s.create_item('commitment','person','new','new','q'); id=o.prepare(new)
        with self.s.tx() as c: c.execute("update outbox set state='sending' where id=?",(id,))
        o.recover(); self.assertEqual(o.get(id)['state'],'unknown')
        self.assertFalse(o.dispatch(id,lambda _:self.fail('unknown resent'),self.now))
        self.s.pause(True); self.s.forget(); self.assertFalse(self.s.items())
        with self.s.tx() as c: self.assertEqual(c.execute('select count(*) from outbox').fetchone()[0],0)

    def test_dst_local_days_quiet_window_and_cooldown(self):
        zone=ZoneInfo('America/New_York')
        snapshot={'mode':'active','timezone':'America/New_York','state':'open','revision':0,'expected_revision':0}
        for fold in (0,1):
            night=datetime(2026,11,1,1,30,tzinfo=zone,fold=fold).timestamp()
            self.assertEqual(evaluate(snapshot,night).reason,'quiet hours')
        morning=datetime(2026,11,1,9,tzinfo=zone).timestamp()
        self.assertTrue(evaluate(snapshot,morning).allowed)
        snapshot['last_contact']=morning-10799
        self.assertTrue(evaluate(snapshot,morning).allowed)
