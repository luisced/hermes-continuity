"""Transactional state, claim fencing and code-owned budgets."""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import uuid
import time

class Stale(RuntimeError): pass

def secure_dir(path):
    p=Path(path).absolute()
    if '..' in p.parts or any(q.is_symlink() for q in (p,*p.parents)):
        raise ValueError('unsafe directory')
    p.mkdir(mode=0o700,parents=True,exist_ok=True); p.chmod(0o700)
    return p

class Store:
    def __init__(self, data_dir):
        self.root=secure_dir(data_dir); self.path=self.root/'continuity.sqlite3'
        if self.path.is_symlink(): raise ValueError('symlink database')
        fd=os.open(self.path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600); os.close(fd)
        self.path.chmod(0o600)
        with self.tx() as c:
            schema='''
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT OR IGNORE INTO settings VALUES('paused','false');
            INSERT OR IGNORE INTO settings VALUES('epoch','0');
            CREATE TABLE IF NOT EXISTS event_auth(id TEXT PRIMARY KEY,identity TEXT,received REAL);
            CREATE TABLE IF NOT EXISTS native_pending(id TEXT PRIMARY KEY,source TEXT UNIQUE NOT NULL,binding TEXT NOT NULL,epoch INTEGER NOT NULL,observed REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT,payload TEXT,dedupe TEXT UNIQUE,
              due REAL,state TEXT DEFAULT 'pending',attempts INTEGER DEFAULT 0,token TEXT,until REAL,epoch INTEGER,error TEXT);
            CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY,kind TEXT,owner TEXT,title TEXT,source TEXT,quote TEXT,
              state TEXT DEFAULT 'open',revision INTEGER DEFAULT 0,review REAL,artifact TEXT);
            CREATE TABLE IF NOT EXISTS item_events(id INTEGER PRIMARY KEY,item TEXT,revision INTEGER,state TEXT,reason TEXT);
            CREATE TABLE IF NOT EXISTS budgets(kind TEXT,day TEXT,n INTEGER,PRIMARY KEY(kind,day));
            CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY,item TEXT,body TEXT);
            CREATE TABLE IF NOT EXISTS outbox_claims(id TEXT PRIMARY KEY,token TEXT,until REAL,notice TEXT);
            CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,item TEXT,revision INTEGER,state TEXT,receipt TEXT,epoch INTEGER);
            CREATE TABLE IF NOT EXISTS checkin_days(id TEXT PRIMARY KEY,day TEXT,principal TEXT,state TEXT,due REAL,evidence TEXT,epoch INTEGER,draft TEXT,item TEXT);
            CREATE TABLE IF NOT EXISTS checkin_responses(event TEXT PRIMARY KEY,day_id TEXT,kind TEXT,followups TEXT);
            CREATE TABLE IF NOT EXISTS inference_executions(id TEXT PRIMARY KEY,max_turns INTEGER,attempts INTEGER,state TEXT);
            CREATE TABLE IF NOT EXISTS inference_blocks(work TEXT PRIMARY KEY,fingerprint TEXT,execution TEXT,reason TEXT);
            '''
            version=c.execute('PRAGMA user_version').fetchone()[0]
            if version>3: raise ValueError('unsupported database version')
            for statement in schema.split(';'):
                if statement.strip(): c.execute(statement)
            columns={r[1] for r in c.execute('PRAGMA table_info(items)')}
            for name,kind in {'principal':'TEXT','actor':"TEXT DEFAULT 'unknown'",'deadline':'REAL','expectation':'TEXT','task':'TEXT','parent':'TEXT'}.items():
                if name not in columns: c.execute('ALTER TABLE items ADD COLUMN '+name+' '+kind)
            if version<2:
                c.execute('UPDATE items SET principal=owner WHERE principal IS NULL')
                # Evidence is a set, never last-row-wins. Contradictions remain unknown.
                evidence={}
                for row in c.execute('SELECT item,body FROM artifacts ORDER BY id').fetchall():
                    try: actor=json.loads(row['body']).get('proposal',{}).get('owner')
                    except (ValueError,AttributeError,TypeError): continue
                    if actor in ('human','assistant'): evidence.setdefault(row['item'],set()).add(actor)
                for row in c.execute('SELECT id,actor FROM items').fetchall():
                    actors=evidence.get(row['id'],set())
                    actor=next(iter(actors)) if len(actors)==1 else 'unknown'
                    c.execute('UPDATE items SET actor=? WHERE id=?',(actor,row['id']))
                # Keep ambiguous duplicate IDs and references for manual review.
                c.execute("UPDATE items SET state='blocked',revision=revision+1 WHERE state NOT IN ('done','dismissed') AND id IN (SELECT a.id FROM items a JOIN items b ON a.id<>b.id AND a.source=b.source AND a.kind=b.kind AND a.principal=b.principal AND a.actor=b.actor AND a.quote=b.quote)")
                c.execute("CREATE UNIQUE INDEX IF NOT EXISTS item_natural_unique ON items(source,kind,principal,actor,quote) WHERE state NOT IN ('blocked','done','dismissed')")
                c.execute('PRAGMA user_version=2')
            if version<3:
                c.execute('''CREATE TABLE lifecycle_states(item TEXT PRIMARY KEY,revision INTEGER NOT NULL,
                    stage TEXT NOT NULL,result_state TEXT NOT NULL,next_action TEXT NOT NULL,
                    reason TEXT NOT NULL,changed_at REAL NOT NULL)''')
                c.execute('''CREATE TABLE lifecycle_results(id TEXT PRIMARY KEY,item TEXT NOT NULL,
                    revision INTEGER NOT NULL,kind TEXT NOT NULL,artifact TEXT NOT NULL,
                    artifact_digest TEXT NOT NULL,digest TEXT NOT NULL,sources TEXT NOT NULL,
                    observed_at REAL NOT NULL,verified_at REAL)''')
                c.execute('''CREATE TABLE lifecycle_events(id INTEGER PRIMARY KEY,item TEXT NOT NULL,
                    revision INTEGER NOT NULL,principal TEXT NOT NULL,thread TEXT,actor TEXT NOT NULL,
                    stage TEXT NOT NULL,result_state TEXT NOT NULL,causal_event TEXT,evidence TEXT NOT NULL,
                    closure_condition TEXT NOT NULL,next_action TEXT NOT NULL,reason TEXT NOT NULL,
                    changed_at REAL NOT NULL,UNIQUE(item,revision))''')
                c.execute('CREATE INDEX lifecycle_results_item ON lifecycle_results(item,revision)')
                c.execute('''CREATE TRIGGER lifecycle_events_immutable BEFORE UPDATE ON lifecycle_events
                    BEGIN SELECT RAISE(ABORT,'immutable lifecycle event'); END''')
                c.execute('PRAGMA user_version=3')
            checkin_columns={r[1] for r in c.execute('PRAGMA table_info(checkin_days)')}
            if 'reason' not in checkin_columns: c.execute('ALTER TABLE checkin_days ADD COLUMN reason TEXT')
            self._purge_native_pending(c)
    @contextlib.contextmanager
    def tx(self):
        c=sqlite3.connect(self.path,timeout=15,isolation_level=None); c.row_factory=sqlite3.Row
        try:
            c.execute('BEGIN IMMEDIATE'); yield c; c.commit()
        except BaseException: c.rollback(); raise
        finally: c.close()
    def setting(self,key,default=None):
        with self.tx() as c:
            r=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
            return json.loads(r[0]) if r else default
    def configure(self,key,value):
        with self.authorization_lock():
            with self.tx() as c:
                prior=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
                encoded=json.dumps(value)
                c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,encoded))
                if key in {'identities','adapters','preparation_context','mode','allow_model','allow_delivery','delivery_target','checkin','timezone','workflows'} and (not prior or json.loads(prior[0])!=value):
                    c.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='epoch'")
                    c.execute('PRAGMA secure_delete=ON')
                    c.execute('DELETE FROM native_pending')
    @contextlib.contextmanager
    def authorization_lock(self):
        from .ticker import bounded_lock
        with bounded_lock(self.root/'authorization.lock'): yield
    @contextlib.contextmanager
    def effect_admission(self,job,now,check=None):
        # Linearization point shared with revocation. After this check an operation
        # is in flight; revocation prevents subsequent admissions, not cancellation.
        with self.authorization_lock():
            with self.tx() as c:
                self.guard(c,job,now)
                if check: check(c)
        yield

    def pause(self,paused):
        with self.authorization_lock():
            with self.tx() as c:
                c.execute("UPDATE settings SET value=? WHERE key='paused'",(json.dumps(paused),))
                c.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='epoch'")
                c.execute('PRAGMA secure_delete=ON')
                c.execute('DELETE FROM native_pending')
    def _enabled(self,c):
        return c.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0]=='false'
    def _epoch(self,c): return int(c.execute("SELECT value FROM settings WHERE key='epoch'").fetchone()[0])
    def ingest(self,event,epoch=None,identity_key=None):
        with self.tx() as c:
            return self._ingest(c,event,epoch,identity_key)
    def _ingest(self,c,event,epoch=None,identity_key=None):
        if not self._enabled(c) or (epoch is not None and epoch!=self._epoch(c)): return False
        if identity_key is not None:
            identities=c.execute("SELECT value FROM settings WHERE key='identities'").fetchone()
            if not identities or json.loads(identities[0]).get(identity_key)!=event['principal']: return False
        prior=c.execute('SELECT body FROM events WHERE id=?',(event['id'],)).fetchone()
        if prior:
            if json.loads(prior[0])!=event: raise ValueError('source ID collision')
            return False
        if len(json.dumps(event))>1048576: raise ValueError('source exceeds 1 MiB; split explicitly')
        if c.execute("SELECT count(*) FROM jobs WHERE state IN ('pending','running')").fetchone()[0]>=256: raise ValueError('queue full; source not admitted')
        n=c.execute('INSERT OR IGNORE INTO events VALUES(?,?)',(event['id'],json.dumps(event))).rowcount
        if n and event.get('identity_key'):
            identities=c.execute("SELECT value FROM settings WHERE key='identities'").fetchone()
            if not identities or json.loads(identities[0]).get(event['identity_key'])!=event['principal']: raise ValueError('identity revoked')
            c.execute('INSERT INTO event_auth VALUES(?,?,?)',(event['id'],event['identity_key'],time.time()))
        if n: self._enqueue(c,'extract',{'event_id':event['id']},'extract:'+event['id'],0)
        return bool(n)
    def _purge_native_pending(self,c):
        c.execute('PRAGMA secure_delete=ON')
        c.execute('DELETE FROM native_pending WHERE epoch<>? OR observed<?', (self._epoch(c),time.time()-86400))
    def native_pending(self,id,*,source=None):
        """One exact PRE reference/status, never text or an admitted event."""
        with self.tx() as c:
            self._purge_native_pending(c)
            row=c.execute('SELECT * FROM native_pending WHERE id=? OR source=?',(id,source)).fetchone()
            return dict(row) if row and self._enabled(c) else None
    def witness_native(self,id,reference,epoch,observed):
        with self.tx() as c:
            self._purge_native_pending(c)
            if not self._enabled(c) or epoch!=self._epoch(c): return False
            prior=c.execute('SELECT * FROM native_pending WHERE id=? OR source=?',(id,reference['source'])).fetchall()
            if prior:
                if len(prior)!=1 or prior[0]['id']!=id or prior[0]['binding']!=reference['binding']:
                    raise ValueError('native PRE reference collision')
                return False  # never refresh the original observation time
            if c.execute('SELECT count(*) FROM native_pending').fetchone()[0]>=256: raise ValueError('native PRE capacity reached')
            c.execute('INSERT INTO native_pending VALUES(?,?,?,?,?)',(id,reference['source'],reference['binding'],epoch,observed))
            return True
    def confirm_native(self,reference,human,assistant,epoch,identity_key):
        """Consume a verified PRE reference and publish its pair atomically.

        Only the host calls this after strict native persisted-message proof.
        No ticker/history/memory path promotes references, including on restart.
        """
        with self.tx() as c:
            self._purge_native_pending(c)
            if not self._enabled(c) or epoch!=self._epoch(c): return False
            prior=c.execute('SELECT * FROM native_pending WHERE id=?',(human['id'],)).fetchone()
            if not prior or prior['source']!=reference['source'] or prior['binding']!=reference['binding']:
                raise ValueError('post requires exact native PRE reference')
            human=dict(human,observed_at=prior['observed'])
            if not self._ingest(c,human,epoch,identity_key): return False
            if not self._ingest(c,assistant,epoch,identity_key): raise ValueError('native pair not admitted')
            c.execute('DELETE FROM native_pending WHERE id=?',(human['id'],))
            return True
    def event(self,id):
        with self.tx() as c:
            r=c.execute('SELECT body FROM events WHERE id=?',(id,)).fetchone()
            return json.loads(r[0]) if r else None
    def _enqueue(self,c,kind,payload,key,due):
        prior=c.execute('SELECT id FROM jobs WHERE dedupe=?',(key,)).fetchone()
        if prior: return prior[0]
        if c.execute("SELECT count(*) FROM jobs WHERE state IN ('pending','running')").fetchone()[0]>=256: raise ValueError('queue full; work not admitted')
        if len(json.dumps(payload))>1048576: raise ValueError('work payload too large')
        c.execute('INSERT OR IGNORE INTO jobs(id,kind,payload,dedupe,due,epoch) VALUES(?,?,?,?,?,?)',
                  (uuid.uuid4().hex,kind,json.dumps(payload),key,due,self._epoch(c)))
        return c.execute('SELECT id FROM jobs WHERE dedupe=?',(key,)).fetchone()[0]
    def enqueue(self,kind,payload,key,due_at):
        with self.tx() as c: return self._enqueue(c,kind,payload,key,due_at)
    def claim(self,owner,now,lease_seconds=60):
        with self.tx() as c:
            if not self._enabled(c): return None
            c.execute("UPDATE jobs SET state='blocked',error='authorization generation changed' WHERE kind IN ('read','draft','workflow') AND state IN ('pending','running') AND (epoch IS NULL OR epoch<>?)",(self._epoch(c),))
            c.execute("UPDATE jobs SET state='blocked',error='lease retries exhausted' WHERE state='running' AND until<=? AND attempts>=3",(now,))
            r=c.execute("SELECT * FROM jobs WHERE due<=? AND attempts<3 AND (state='pending' OR (state='running' AND until<=?)) ORDER BY due,rowid LIMIT 1",(now,now)).fetchone()
            if not r: return None
            token=owner+':'+uuid.uuid4().hex
            c.execute("UPDATE jobs SET state='running',token=?,until=?,attempts=attempts+1,epoch=? WHERE id=?",(token,now+lease_seconds,self._epoch(c),r['id']))
            return dict(c.execute('SELECT * FROM jobs WHERE id=?',(r['id'],)).fetchone())
    def guard(self,c,job,now):
        r=c.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone()
        if not r or r['state']!='running' or r['token']!=job['token'] or r['until']<=now or r['epoch']!=self._epoch(c) or not self._enabled(c):
            raise Stale('expired, paused or superseded claim')
    def finish(self,job,now):
        with self.tx() as c:
            self.guard(c,job,now); c.execute("UPDATE jobs SET state='done' WHERE id=?",(job['id'],))
    def renew(self,job,now,seconds=60):
        with self.tx() as c:
            self.guard(c,job,now); c.execute('UPDATE jobs SET until=? WHERE id=?',(now+seconds,job['id']))
    def fail(self,job,now,error):
        with self.tx() as c:
            self.guard(c,job,now)
            c.execute('UPDATE jobs SET state=?,due=?,error=? WHERE id=?',('blocked' if job['attempts']>=3 else 'pending',now+2**job['attempts'],error[:160],job['id']))
    def defer(self,job,now,due):
        with self.tx() as c:
            self.guard(c,job,now); c.execute("UPDATE jobs SET state='pending',due=?,attempts=attempts-1 WHERE id=?",(due,job['id']))
    def jobs(self):
        with self.tx() as c: return [dict(r) for r in c.execute('SELECT * FROM jobs')]
    def create_item(self,kind,owner,title,source,quote,review=None):
        with self.tx() as c: return self._create_item(c,kind,owner,title,source,quote,review)
    def _create_item(self,c,kind,owner,title,source,quote,review=None,actor="human",deadline=None,expectation=None,task=None,parent=None):
        id=uuid.uuid5(uuid.NAMESPACE_URL,json.dumps([source,kind,owner,actor,quote])).hex
        prior=c.execute('SELECT id FROM items WHERE source=? AND kind=? AND principal=? AND actor=? AND quote=?',(source,kind,owner,actor,quote)).fetchone()
        if prior: return dict(c.execute('SELECT * FROM items WHERE id=?',(prior[0],)).fetchone())
        c.execute('INSERT INTO items(id,kind,owner,title,source,quote,review,principal,actor,deadline,expectation,task,parent) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(id,kind,owner,title,source,quote,review,owner,actor,deadline,expectation,task,parent))
        return dict(c.execute('SELECT * FROM items WHERE id=?',(id,)).fetchone())
    def items(self):
        with self.tx() as c: return [dict(r) for r in c.execute('SELECT * FROM items')]
    def artifact_current(self,c,item,artifact_id,encoded):
        """Content authorization is separate from the obligation's CAS revision.

        A registered, exact selected draft may be explicitly carried by lifecycle
        bookkeeping. Ordinary item changes invalidate that anchor. The artifact's
        original revision remains immutable and identifies its one outbox attempt.
        """
        import hashlib
        body=json.loads(encoded)
        revision=body.get('revision')
        if type(revision) is not int or not 0<=revision<=item['revision']: return False
        if body.get('type') not in ('local_draft','meeting_brief'): return revision==item['revision']
        if artifact_id!=item['artifact'] or body.get('item')!=item['id'] or body.get('source')!=item['source']: return False
        if revision==item['revision']: return True
        return c.execute('''SELECT 1 FROM lifecycle_results WHERE item=? AND revision=?
            AND artifact=? AND artifact_digest=?''',
            (item['id'],item['revision'],artifact_id,hashlib.sha256(encoded.encode()).hexdigest())).fetchone() is not None

    def transition(self,id,revision,state,reason='human control',review=None):
        if state not in ('open','done','dismissed','snoozed','blocked','waiting'): raise ValueError('state')
        with self.tx() as c: self._transition(c,id,revision,state,reason,review)
    def _transition(self,c,id,revision,state,reason,review=None):
        if c.execute("UPDATE items SET state=?,revision=revision+1,review=CASE WHEN ? IN ('waiting','blocked') AND ? IS NULL THEN review ELSE ? END WHERE id=? AND revision=?",(state,state,review,review,id,revision)).rowcount!=1: raise Stale('item revision changed')
        c.execute('INSERT INTO item_events(item,revision,state,reason) VALUES(?,?,?,?)',(id,revision+1,state,reason))
    def reserve(self,kind,day,cap):
        with self.tx() as c: return self._reserve(c,kind,day,cap)
    def _reserve(self,c,kind,day,cap):
        c.execute('INSERT OR IGNORE INTO budgets VALUES(?,?,0)',(kind,day))
        return c.execute('UPDATE budgets SET n=n+1 WHERE kind=? AND day=? AND n<?',(kind,day,cap)).rowcount==1
    def forget(self):
        from .ticker import lifecycle_lock
        with lifecycle_lock(self): return self._forget_locked()
    def _forget_locked(self):
        self.pause(True)
        with self.tx() as c:
            c.execute('PRAGMA secure_delete=ON')
            for t in ('lifecycle_states','lifecycle_results','lifecycle_events','native_pending','event_auth','events','jobs','items','item_events','artifacts','outbox','outbox_claims','budgets','checkin_days','checkin_responses','inference_executions','inference_blocks'): c.execute('DELETE FROM '+t)
            c.execute("DELETE FROM settings WHERE key NOT IN ('paused','epoch','ticker_token')")
        errors=[]
        # Logical purge is committed before fallible filesystem/scheduler cleanup.
        try:
            drafts=self.root/'drafts'
            if drafts.is_symlink(): drafts.unlink()
            elif drafts.exists():
                for draft in drafts.iterdir():
                    try:
                        if not draft.is_file() and not draft.is_symlink(): raise ValueError('unexpected managed directory')
                        draft.unlink()
                    except Exception as exc: errors.append(type(exc).__name__)
                try: drafts.rmdir()
                except Exception as exc: errors.append(type(exc).__name__)
        except Exception as exc: errors.append(type(exc).__name__)
        for p in self.root.glob('export-*'):
            try: p.unlink()
            except Exception as exc: errors.append(type(exc).__name__)
        try:
            with contextlib.closing(sqlite3.connect(self.path)) as c: c.execute('VACUUM')
        except sqlite3.Error as exc: errors.append(type(exc).__name__)
        return {'cleanup_required':bool(errors),'errors':sorted(set(errors))}
