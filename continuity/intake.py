"""Owner ingress constructed by local CLI or verified native session adapter.

Never exposed as model tool arguments.
"""
from dataclasses import dataclass
import time

@dataclass(frozen=True)
class OwnerContext:
    surface: str
    identity: str
    @classmethod
    def local(cls,surface,identity):
        return cls(surface,identity)

def admit_event(store,host_context,message):
    if type(host_context) is not OwnerContext:
        raise ValueError('authenticated local-owner ingress required')
    principal=store.setting('identities',{}).get(host_context.surface+':'+host_context.identity)
    if not principal: raise ValueError('unmapped identity')
    if set(message)-{'id','text','role','parent'}: raise ValueError('unknown event field')
    if not all(type(message.get(k)) is str and message[k] for k in ('id','text')): raise ValueError('missing source')
    role=message.get('role','human')
    if role not in ('human','assistant'): raise ValueError('producer')
    if role=='assistant':
        parent=store.event(message.get('parent'))
        if not parent or parent['role']!='human' or parent['principal']!=principal: raise ValueError('unlinked promise')
    return dict(message,role=role,principal=principal,surface=host_context.surface,identity_key=host_context.surface+':'+host_context.identity,observed_at=time.time(),authorization_epoch=store.setting('epoch',0))
