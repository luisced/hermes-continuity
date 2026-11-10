"""Owner-selected output only. No model-selected destinations or attachment syntax."""
import hashlib
import json
import re
import subprocess
from pathlib import Path

import shutil
# Resolve a host-provided CLI once; never bake one user's deployment into the plugin.
HERMES_PATH=shutil.which('hermes')
HERMES=Path(HERMES_PATH) if HERMES_PATH else None

def target(value):
    if type(value) is not str or not re.fullmatch(r'[a-z][a-z0-9_-]*:[A-Za-z0-9_@.+-]+',value):
        raise ValueError('exact platform:chat_id required (no aliases, threads or options)')
    return value

class SendUnverified(ValueError):
    """Code-owned diagnostic only; never retain raw transport output or addresses."""
    REASONS=frozenset({'send_timeout','send_process_error','invalid_response','bridge_unavailable','transport_rejected','receipt_unverified'})
    def __init__(self,reason):
        self.reason=reason if reason in self.REASONS else 'invalid_response'
        super().__init__(self.reason)

class HermesSend:
    def __init__(self,destination):
        if HERMES is None: raise ValueError('Hermes CLI unavailable')
        self.destination=target(destination); self.executable=str(HERMES.absolute())
    def __call__(self,message):
        # Fixed destination; callers supply a code notice or schema/citation-validated
        # check-in question. Attachment/routing syntax is never forwarded.
        if type(message) is not str or len(message)>2000 or 'MEDIA:' in message or '[[' in message: raise ValueError('unsafe notice')
        try:
            r=subprocess.run([self.executable,'send','--to',self.destination,'--json','--file','-'],input=message,text=True,capture_output=True,shell=False,timeout=30)
        except subprocess.TimeoutExpired:
            raise SendUnverified('send_timeout') from None
        except OSError:
            raise SendUnverified('send_process_error') from None
        if len(r.stdout)>64000: raise SendUnverified('invalid_response')
        try: result=json.loads(r.stdout)
        except (ValueError,TypeError): raise SendUnverified('invalid_response') from None
        if type(result) is not dict: raise SendUnverified('invalid_response')
        # Exact code-owned host error, not fuzzy matching arbitrary error text.
        # This is diagnostic evidence only: the outbox still remains UNKNOWN.
        if result.get('error')=='WhatsApp bridge error (503): {"error":"Not connected to WhatsApp"}':
            raise SendUnverified('bridge_unavailable')
        if r.returncode or result.get('success') is not True or result.get('error') or result.get('skipped'): raise SendUnverified('transport_rejected')
        platform,chat_id=self.destination.split(':',1)
        if result.get('platform')!=platform or result.get('chat_id')!=chat_id or not isinstance(result.get('message_id'),str) or not result['message_id']: raise SendUnverified('receipt_unverified')
        return {'receipt':result['message_id']}

def deliver_pending(store,now,transport=None):
    from .outbox import Outbox
    o=Outbox(store); o.recover(now)
    if store.setting('mode','shadow')!='active' or not store.setting('allow_delivery',False) or store.setting('paused',False): return 0
    destination=target(store.setting('delivery_target',''))
    with store.tx() as c:
        rows=[dict(r) for r in c.execute('SELECT * FROM artifacts ORDER BY rowid DESC LIMIT 32')]
    for row in rows:
        body=json.loads(row['body'])
        if body.get('type')!='local_draft' and body.get('contact_reason') not in ('assistant_obligation','explicit_reminder'): continue
        if body.get('next_action') in ('wait', 'prepare', 'close'): continue
        if type(body.get('created')) not in (int,float) or not 0<=now-body['created']<=86400: continue
        item=next((i for i in store.items() if i['id']==row['item']),None)
        if not item or item['state']!='open': continue
        with store.tx() as c:
            if not store.artifact_current(c,item,row['id'],row['body']): continue
            from .lifecycle import Lifecycle
            if not Lifecycle(store).typed_artifact_valid(c,item,row['id'],row['body']): continue
        if body.get('type')=='local_draft':
            from .adapters import MarkdownAdapter
            try: content=MarkdownAdapter(store.root/'drafts').read(item['artifact']+'.md')['text']
            except (OSError,ValueError,TypeError): continue
            if hashlib.sha256(content.encode()).hexdigest()!=body.get('sha256'): continue
        notice={'artifact':row['id'],'hash':hashlib.sha256(row['body'].encode()).hexdigest(),'target':destination,'created':now}
        id=o.prepare(item,notice)
        # Render inert task data, never routing/attachment syntax from a title.
        label=re.sub(r'[\r\n<>`\\\\/\[\]]', ' ', item['title'])[:200].replace('MEDIA:', 'MEDIA ').replace('://', ' ')
        if body.get('type')=='local_draft':
            message='Continuity: a local draft is ready for your review. '+label+'. Open hermes continuity list to review.'
        elif body.get('contact_reason')=='explicit_reminder':
            message='Continuity reminder: '+label+'.'
        else:
            message='Continuity: I still owe you '+label+'. What information or approval should I use to proceed?'
        if o.dispatch(id,transport or (lambda key:HermesSend(destination)(message)),now): return 1
    return 0
