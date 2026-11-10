"""Coordinated delivery with bounded claims and optional fixed-destination notices.

A transport already in flight cannot be recalled. sending is never retried after
restart: recover() makes expired claims unknown, requiring owner reconciliation.
"""
import json
import uuid
import time
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
from .policy import evaluate

class Outbox:
    def __init__(self,store): self.store=store
    def get(self,id):
        with self.store.tx() as c:
            r=c.execute('SELECT * FROM outbox WHERE id=?',(id,)).fetchone(); return dict(r) if r else None
    def prepare(self,item,notice=None):
        with self.store.tx() as c:
            revision=item['revision']
            if notice is not None:
                a=c.execute('SELECT body FROM artifacts WHERE id=? AND item=?',
                            (notice['artifact'],item['id'])).fetchone()
                if a and json.loads(a[0]).get('type')=='local_draft' and self.store.artifact_current(c,item,notice['artifact'],a[0]):
                    # Bookkeeping is not a new content generation or a new send
                    # attempt. Reuse the original row, including UNKNOWN/epoch.
                    revision=json.loads(a[0])['revision']
            id=uuid.uuid5(uuid.NAMESPACE_URL,item['id']+':'+str(revision)).hex
            c.execute('INSERT OR IGNORE INTO outbox VALUES(?,?,?,?,?,?)',(id,item['id'],revision,'planned',None,self.store._epoch(c)))
            if notice is not None:
                # Refresh only a revalidated check-in which has never acquired a
                # claim or reserved a daily attempt. Artifact authority is checked
                # inside this same transaction; current epoch alone cannot adopt
                # a draft produced under an earlier consent generation.
                out=c.execute('SELECT * FROM outbox WHERE id=?',(id,)).fetchone()
                claim=c.execute('SELECT * FROM outbox_claims WHERE id=?',(id,)).fetchone()
                if (out['state']!='planned' or out['receipt'] is not None
                        or (claim and (claim['token'] is not None or claim['until'] is not None))):
                    return id
                current=c.execute('SELECT * FROM items WHERE id=?',(item['id'],)).fetchone()
                settings={r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM settings')}
                artifact=c.execute('SELECT body FROM artifacts WHERE id=? AND item=?',(notice['artifact'],item['id'])).fetchone()
                body=json.loads(artifact[0]) if artifact else {}
                day=c.execute('SELECT day FROM checkin_days WHERE id=?',(body.get('day_key'),)).fetchone()
                unused=day and not c.execute("SELECT 1 FROM budgets WHERE kind='checkin_delivery' AND day=? AND n>0",(day['day'],)).fetchone()
                if current and current['kind']=='checkin' and (
                        out['state']!='planned' or out['receipt'] is not None or not unused
                        or (claim and (claim['token'] is not None or claim['until'] is not None))):
                    return id
                if (current and current['kind']=='checkin' and current['revision']==item['revision']
                        and current['state']=='open' and out['state']=='planned' and out['receipt'] is None
                        and (not claim or (claim['token'] is None and claim['until'] is None))
                        and body.get('type')=='checkin' and unused and self.store._enabled(c)
                        and self.notice_valid(c,id,current,settings,notice['created'],notice=notice)):
                    c.execute('UPDATE outbox SET epoch=? WHERE id=?',(self.store._epoch(c),id))
                    c.execute('INSERT INTO outbox_claims VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET notice=excluded.notice',
                              (id,None,None,json.dumps(notice)))
                else:
                    c.execute('INSERT OR IGNORE INTO outbox_claims VALUES(?,?,?,?)',(id,None,None,json.dumps(notice)))
        return id
    def recover(self,now=None):
        now=time.time() if now is None else now
        with self.store.tx() as c:
            c.execute("UPDATE outbox SET state='unknown' WHERE state='sending' AND NOT EXISTS (SELECT 1 FROM outbox_claims x WHERE x.id=outbox.id AND x.until>?)",(now,))
    def notice_valid(self,c,id,item,settings,now,notice=None):
        if notice is None:
            claim=c.execute('SELECT * FROM outbox_claims WHERE id=?',(id,)).fetchone()
            if not claim or not claim['notice']: return True
            notice=json.loads(claim['notice'])
        n=notice
        if not settings.get('allow_delivery',False) or settings.get('delivery_target')!=n['target'] or settings.get('mode')!='active' or not 0<=now-n['created']<=86400: return False
        a=c.execute('SELECT body FROM artifacts WHERE id=? AND item=?',(n['artifact'],item['id'])).fetchone()
        if not a or hashlib.sha256(a[0].encode()).hexdigest()!=n['hash']: return False
        body=json.loads(a[0])
        from .followthrough import contact_current
        if not contact_current(c,dict(item),body,exclude=id): return False
        from .lifecycle import Lifecycle
        if not Lifecycle(self.store).typed_artifact_valid(c,item,n['artifact'],a[0]): return False
        if not self.store.artifact_current(c,item,n['artifact'],a[0]) or type(body.get('created')) not in (int,float) or not 0<=now-body['created']<=86400: return False
        if body.get('type')=='checkin':
            from .checkin import notice_current
            if not notice_current(self.store,c,body,settings,now): return False
        if body.get('type')=='local_draft':
            from .adapters import MarkdownAdapter
            try: content=MarkdownAdapter(self.store.root/'drafts').read(item['artifact']+'.md')['text']
            except (OSError,ValueError,TypeError): return False
            if hashlib.sha256(content.encode()).hexdigest()!=body.get('sha256'): return False
        return True
    def revision_current(self,c,out,item):
        if out['revision']==item['revision']: return True
        claim=c.execute('SELECT notice FROM outbox_claims WHERE id=?',(out['id'],)).fetchone()
        if not claim or not claim['notice']: return False
        notice=json.loads(claim['notice'])
        artifact=c.execute('SELECT body FROM artifacts WHERE id=? AND item=?',
                           (notice['artifact'],item['id'])).fetchone()
        if not artifact: return False
        body=json.loads(artifact[0])
        return (body.get('type')=='local_draft' and body.get('revision')==out['revision']
                and self.store.artifact_current(c,item,notice['artifact'],artifact[0]))

    def dispatch(self,id,transport,now):
        started=time.monotonic()
        with self.store.tx() as c:
            out=c.execute('SELECT * FROM outbox WHERE id=?',(id,)).fetchone()
            if not out or out['state']!='planned' or out['epoch']!=self.store._epoch(c): return False
            item=c.execute('SELECT * FROM items WHERE id=?',(out['item'],)).fetchone()
            if not item: return False
            prior_claim=c.execute('SELECT * FROM outbox_claims WHERE id=?',(id,)).fetchone()
            if prior_claim and (prior_claim['token'] is not None or prior_claim['until'] is not None): return False
            if item['kind']=='checkin' and (not prior_claim or not prior_claim['notice']): return False
            settings={r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM settings')}
            if settings.get('mode','shadow')!='active': return False
            if not self.notice_valid(c,id,item,settings,now) or not self.revision_current(c,out,item): return False
            snapshot=dict(item,expected_revision=item['revision'],mode=settings.get('mode','shadow'),paused=settings.get('paused'),timezone=settings.get('timezone','UTC'),last_contact=settings.get('last_contact'))
            if c.execute('SELECT 1 FROM outbox_claims WHERE id=? AND notice IS NOT NULL',(id,)).fetchone(): snapshot['review']=None
            if not evaluate(snapshot,now).allowed: return False
            day=datetime.fromtimestamp(now,ZoneInfo(snapshot['timezone'])).date().isoformat()
            if item['kind']=='checkin':
                if not self.store._reserve(c,'checkin_delivery',day,1): return False
            c.execute("INSERT OR REPLACE INTO settings VALUES('last_contact',?)",(json.dumps(now),))
            token=uuid.uuid4().hex
            c.execute('INSERT INTO outbox_claims(id,token,until) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET token=excluded.token,until=excluded.until',(id,token,now+60))
            c.execute("UPDATE outbox SET state='sending' WHERE id=?",(id,))
        # Recheck human control immediately before invoking the injected effect.
        # The reservation is consumed even on failure/uncertainty, preventing bursts.
        with self.store.tx() as c:
            effect_now=now+time.monotonic()-started
            claimed=c.execute('SELECT x.token,x.until,o.state FROM outbox_claims x JOIN outbox o ON x.id=o.id WHERE x.id=?',(id,)).fetchone()
            if not claimed or claimed['state']!='sending' or claimed['token']!=token or claimed['until']<=effect_now: return False
            fresh=c.execute('SELECT * FROM items WHERE id=?',(out['item'],)).fetchone()
            settings={r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM settings')}
            if not fresh or not self.notice_valid(c,id,fresh,settings,effect_now) or settings.get('mode','shadow')!='active' or fresh['revision']!=item['revision'] or not self.revision_current(c,out,fresh) or fresh['state']!='open' or not self.store._enabled(c) or out['epoch']!=self.store._epoch(c):
                c.execute("UPDATE outbox SET state='failed' WHERE id=?",(id,)); return False
            # Reapply quiet hours, eligibility and the same intentional notice
            # review exception at effect time. There is no global contact gate.
            final=dict(fresh,expected_revision=item['revision'],mode=settings.get('mode','shadow'),
                       paused=settings.get('paused'),timezone=settings.get('timezone','UTC'))
            if c.execute('SELECT 1 FROM outbox_claims WHERE id=? AND notice IS NOT NULL',(id,)).fetchone(): final['review']=None
            effect_day=datetime.fromtimestamp(effect_now,ZoneInfo(final['timezone'])).date().isoformat()
            caps=[('checkin_delivery',1)] if fresh['kind']=='checkin' else []
            budgets_valid=effect_day==day
            for kind,cap in caps:
                budget=c.execute('SELECT n FROM budgets WHERE kind=? AND day=?',(kind,effect_day)).fetchone()
                budgets_valid=budgets_valid and budget is not None and 1<=budget['n']<=cap
            if not evaluate(final,effect_now).allowed or not budgets_valid:
                c.execute("UPDATE outbox SET state='failed' WHERE id=?",(id,)); return False
        try:
            result=transport(id)
            if type(result) is not dict or set(result)!={'receipt'} or type(result['receipt']) is not str or not result['receipt']: raise ValueError('ambiguous receipt')
            state='acknowledged'; receipt=result['receipt']
        except Exception as error:
            from .delivery import SendUnverified
            import logging
            reason=error.reason if isinstance(error,SendUnverified) and error.reason in SendUnverified.REASONS else 'transport_exception'
            # No exception text, stdout/stderr, notice, principal or destination.
            logging.getLogger(__name__).warning('continuity delivery unknown outbox=%s reason=%s',id,reason)
            state='unknown'; receipt=None
        with self.store.tx() as c:
            changed=c.execute('UPDATE outbox SET state=?,receipt=? WHERE id=? AND state=? AND EXISTS (SELECT 1 FROM outbox_claims WHERE id=? AND token=?)',(state,receipt,id,'sending',id,token)).rowcount
            if changed==1 and state=='acknowledged':
                evidence=dict(type='delivery_receipt',outbox=id,receipt=receipt,
                              acknowledged_at=now+time.monotonic()-started,scope='transport_only')
                c.execute('INSERT OR IGNORE INTO artifacts VALUES(?,?,?)',
                          ('delivery-receipt:'+id,out['item'],json.dumps(evidence)))
        return state=='acknowledged' and changed==1
