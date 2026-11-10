"""Native profile-bound observer intake; callbacks never return instructions."""
from pathlib import Path
import sqlite3
import logging
import time
from .cli import Controller, setup_parser
from .intake import OwnerContext,admit_event
from .source import NativeSource, NativeSourceError


def capture(controller,phase,payload):
    s=controller.store
    # Epoch captured before source resolution fences callbacks across forget/resume.
    epoch=s.setting('epoch',0)
    if s.setting('paused',False): return False
    observed=time.time()
    if phase not in {'pre','post'}: raise ValueError('unknown native phase')
    sid=payload.get('session_id'); turn=payload.get('turn_id'); text=payload.get('user_message')
    if not all(type(v) is str and v and len(v)<=1048576 for v in (sid,turn,text)): raise ValueError('incomplete native turn')
    human_id='native:'+sid+':'+turn+':human'
    identities=s.setting('identities',{})
    resolver=NativeSource(controller.home)
    try:
        surface,identity=resolver.resolve(payload,identities=identities)
    except NativeSourceError as exc:
        if phase!='pre' or exc.code!='native_user_message_not_persisted': raise
        surface,identity,reference=resolver.reference(payload,identities,pending=True)
        if s.setting('identities',{})!=identities: return False
        return s.witness_native(human_id,reference,epoch,observed)
    if s.setting('identities',{})!=identities: return False
    if phase=='pre':
        _,_,reference=resolver.reference(payload,identities)
        if s.native_pending(human_id,source=reference['source']):
            return s.witness_native(human_id,reference,epoch,observed)
    principal=identities.get(surface+':'+identity)
    def ingest(event):
        # Re-read consent after admission as well as source resolution; the
        # store's atomic epoch + identity-key fence remains the final guard.
        if event['principal']!=principal or s.setting('identities',{})!=identities: return False
        return s.ingest(event,epoch=epoch,identity_key=surface+':'+identity)
    context=OwnerContext.local(surface,identity)
    human=admit_event(s,context,dict(id=human_id,text=text,role='human'))
    existing=s.event(human_id)
    if existing: human['observed_at']=existing.get('observed_at',human['observed_at'])
    if existing and existing.get('authorization_epoch')!=human.get('authorization_epoch'): return False
    if phase=='pre': return ingest(human)
    parent=s.event(human_id)
    response=payload.get('assistant_response')
    if not parent:
        # A missing human is not repaired from the transcript. Require a PRE
        # reference, re-prove the exact native message, then consume it atomically.
        if type(response) is not str or not response: return False
        resolved_surface,resolved_identity,reference=resolver.reference(payload,identities)
        if (resolved_surface,resolved_identity)!=(surface,identity) or s.setting('identities',{})!=identities: return False
        assistant=dict(human,id='native:'+sid+':'+turn+':assistant',text=response,role='assistant',parent=human_id,observed_at=time.time())
        return s.confirm_native(reference,human,assistant,epoch,surface+':'+identity)
    human['observed_at']=parent.get('observed_at',human['observed_at'])
    if parent!=human: raise ValueError('post requires exact admitted pre turn')
    if type(response) is not str or not response: return False
    event=admit_event(s,context,dict(id='native:'+sid+':'+turn+':assistant',text=response,role='assistant',parent=human_id))
    existing=s.event(event['id'])
    if existing: event['observed_at']=existing.get('observed_at',event['observed_at'])
    return ingest(event)


def recent_history(controller,session_id,*,principal,limit=20,before_seq=None):
    """Authorized same-route read for consumers; never backfills capture/events.

    The result is a snapshot. Consumers doing later work must preserve/recheck
    their own epoch fence; this function fences authorization across the read.
    """
    s=controller.store
    epoch=s.setting('epoch',0)
    identities=s.setting('identities',{})
    if s.setting('paused',False): return []
    if controller.ctx is not None and controller.ctx.get_config('collect_references',False) is not True: return []
    rows=NativeSource(controller.home).recent(session_id,identities,principal=principal,limit=limit,before_seq=before_seq)
    if s.setting('epoch',0)!=epoch or s.setting('identities',{})!=identities or s.setting('paused',False): return []
    return rows


def register(ctx):
    from hermes_constants import get_hermes_home
    controller=Controller(Path(get_hermes_home()).absolute(),ctx)
    ctx.register_cli_command('continuity','Manage profile-local continuity',setup_parser,controller.handle)
    def callback(phase,**payload):
        if ctx.get_config('collect_references',False) is not True: return
        try: capture(controller,phase,payload)
        except Exception as exc:
            # Only adapter-owned constant reasons are logged; never exception
            # text from JSON/SQLite/model data, IDs, or transcript fragments.
            reason=exc.code if isinstance(exc,NativeSourceError) else ('native_storage_error' if isinstance(exc,sqlite3.Error) else 'native_capture_error')
            logging.getLogger(__name__).warning('continuity source rejected reason=%s error=%s',reason,type(exc).__name__)
            return
    ctx.register_hook('pre_llm_call',lambda **p:callback('pre',**p))
    ctx.register_hook('post_llm_call',lambda **p:callback('post',**p))
