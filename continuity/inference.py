"""Hermes inference-turn accounting for one worker execution.

A turn is one PluginLlm request (including a request that raises/does not parse),
matching the agent-loop boundary, not SDK network retries. No daily model quota.
Blocked work is durable; constructing another worker cannot silently reset it.
"""
import hashlib
import json
import uuid

EXHAUSTED = 'inference_turn_limit_exhausted'

class InferenceBlocked(RuntimeError):
    pass

def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

class Execution:
    def __init__(self, store):
        from hermes_cli.config import load_config_readonly, resolve_turn_limit
        self.store=store; self.id=uuid.uuid4().hex
        try:
            raw=load_config_readonly().get('agent',{}).get('max_turns')
            if isinstance(raw,bool): raise ValueError()
            self.limit=resolve_turn_limit(raw)
            self.reason=None
        except Exception:
            self.limit=0; self.reason='invalid_agent_max_turns'
        with store.tx() as c:
            c.execute('INSERT INTO inference_executions VALUES(?,?,0,?)',
                      (self.id,self.limit,self.reason or 'available'))

    def summary(self):
        with self.store.tx() as c:
            return dict(c.execute('SELECT id,max_turns,attempts,state FROM inference_executions WHERE id=?',(self.id,)).fetchone())

    def blocked(self, work, signature):
        with self.store.tx() as c:
            row=c.execute('SELECT b.reason,e.attempts,e.max_turns FROM inference_blocks b JOIN inference_executions e ON e.id=b.execution WHERE b.work=? AND b.fingerprint=?',(work,signature)).fetchone()
            if not row: return None
            if row['reason']=='inference_attempted':
                return EXHAUSTED if row['attempts']>=row['max_turns'] else None
            return row['reason']

    def block(self, work, signature, reason=EXHAUSTED):
        with self.store.tx() as c:
            c.execute('INSERT OR REPLACE INTO inference_blocks VALUES(?,?,?,?)',(work,signature,self.id,reason))
            c.execute('UPDATE inference_executions SET state=? WHERE id=?',(reason,self.id))
        return reason

    def complete(self, model, work, signature, **kwargs):
        reason=self.blocked(work,signature)
        if reason: raise InferenceBlocked(self.block(work,signature,reason))
        with self.store.tx() as c:
            row=c.execute('SELECT attempts FROM inference_executions WHERE id=?',(self.id,)).fetchone()
            allowed=row[0]<self.limit and not self.reason
            if allowed:
                # Admission is consumed before invocation, even on provider/parse failure.
                c.execute("UPDATE inference_executions SET attempts=attempts+1,state=CASE WHEN attempts+1>=max_turns THEN 'limit_reached' ELSE 'available' END WHERE id=?",(self.id,))
                # Keep the admission-to-execution binding across a hard crash,
                # including the gap between response and caller publication.
                c.execute("INSERT OR REPLACE INTO inference_blocks VALUES(?,?,?,'inference_attempted')",(work,signature,self.id))
        if not allowed: raise InferenceBlocked(self.block(work,signature,self.reason or EXHAUSTED))
        return model.complete_structured(**kwargs)
