"""Executed only in an isolated native-profile subprocess; synthetic LLM/provider."""
import argparse,json,sqlite3,time
from types import SimpleNamespace
from pathlib import Path
from hermes_cli.plugins import PluginManager
from hermes_constants import get_hermes_home
from hermes_state import SessionDB
from agent.plugin_llm import make_plugin_llm_for_test,_TrustPolicy

home=Path(get_hermes_home()); db=SessionDB()
for sid,source,parent in [('human','whatsapp',None),('other','telegram',None),('cron','cron',None),('delegate','whatsapp','human')]:
    db.create_session(sid,source,user_id='owner',parent_session_id=parent)
db.close()
with sqlite3.connect(home/'state.db') as c:
    for sid, platform in [('human','whatsapp'),('other','telegram')]:
        c.execute('UPDATE sessions SET created_source=?,session_key=?,chat_id=?,chat_type=? WHERE id=?',
                  (platform,platform+'-route',platform+'-chat','dm',sid))
m=PluginManager(); m.discover_and_load(); entry=m._cli_commands['continuity']; controller=entry['handler_fn'].__self__; ctx=controller.ctx
p=argparse.ArgumentParser(); entry['setup_fn'](p)
def run(*args): return entry['handler_fn'](p.parse_args(list(args)))
run('setup','--identity','whatsapp:owner=person','--identity','whatsapp:transport-alias=person','--identity','telegram:owner=person','--allow-model','--mode','prepare')
ctx.set_config('collect_references',True)
(home/'notes').mkdir(); (home/'notes'/'cedar.md').write_text('Cedar pilot launches Friday; budget 500; decision: approve pilot.')
reads=[]
def provider(args,**kw): reads.append(args); return json.dumps({'text':'Calendar: decision meeting Thursday at 10:00.'})
ctx.register_tool(name='continuity_calendar_read',toolset='synthetic',schema={'name':'continuity_calendar_read','description':'Synthetic read calendar','annotations':{'readOnlyHint':True,'continuityScopedRead':{'tool':'continuity_calendar_read','args':{}}},'parameters':{'type':'object','properties':{},'additionalProperties':False}},handler=provider)
ctx.register_tool(name='continuity_memory_read',toolset='synthetic',schema={'name':'continuity_memory_read','description':'Synthetic memory read','annotations':{'readOnlyHint':True,'continuityScopedRead':{'tool':'continuity_memory_read','args':{}}},'parameters':{'type':'object','properties':{},'additionalProperties':False}},handler=lambda args,**kw:json.dumps({'text':'Remembered context: ask who owns launch approval; not completion evidence.'}))
config=[{'type':'markdown','root':str(home/'notes'),'role':'notes','scope':'selected'},{'type':'host-read','role':'calendar','scope':'selected','mapping':{'tool':'continuity_calendar_read','args':{},'schema':{'type':'object','additionalProperties':False,'required':['text'],'properties':{'text':{'type':'string'}}}}}]
config.append({'type':'host-read','role':'memory','scope':'selected','mapping':{'tool':'continuity_memory_read','args':{},'schema':{'type':'object','additionalProperties':False,'required':['text'],'properties':{'text':{'type':'string'}}}}})
(home/'adapters.json').write_text(json.dumps(config)); (home/'context.json').write_text(json.dumps([{'role':'notes','scope':'selected','path':'cedar.md'},{'role':'calendar','scope':'selected'},{'role':'memory','scope':'selected'}]))
run('setup','--adapters',str(home/'adapters.json'),'--preparation-context',str(home/'context.json'))
calls=[]
def caller(**kw):
    calls.append(kw)
    # PluginLlm serializes the input text as a user message.
    content=next(v['content'] for v in kw['messages'] if v.get('role')=='user')
    packet=json.loads(content if isinstance(content,str) else content[-1]['text'])
    if 'authorized_context' in packet:
        assert len(packet['authorized_context'])==3
        result={'title':'Cedar decision brief','markdown':'# Cedar decision brief\n\nThursday 10:00: approve the 500 budget pilot before Friday launch.\n\n## Agenda\nConfirm owner, budget, pilot success criteria and launch readiness.','context_sources':[v['source'] for v in packet['authorized_context']]}
    else:
        event=packet['event']; close=event['text'].startswith('I have')
        if close:
            candidate=next(i for i in packet['related'] if i['parent'])
            result={'proposals':[dict(kind='pending_close',owner='human',title='Cedar brief',source=event['id'],quote=event['text'],target=candidate['id'],target_revision=candidate['revision'],closure='explicit',assertion=dict(quote=event['text'],polarity='affirmed',certainty='certain',speaker='self',temporality='current',meaning='completion'))]}
        else:
            result={'proposals':[dict(kind='commitment',owner='assistant',title='Cedar decision brief',source=event['id'],quote=event['text'],task='draft',expectation='local_draft',deadline=packet['now']+3600,next_review=packet['now']+3600)]}
    response=json.loads(json.dumps({'choices':[{'message':{'content':json.dumps(result)}}]}),object_hook=lambda v:SimpleNamespace(**v))
    return 'synthetic','synthetic',response
ctx._llm=make_plugin_llm_for_test(plugin_id='hermes-continuity',policy=_TrustPolicy(plugin_id='hermes-continuity'),sync_caller=caller)
text='Please draft a Cedar decision brief from the selected context.'
def hook(name,sid='human',turn='1',message=text,**kw):
    if name=='pre_llm_call' and sid in ('human','other'):
        from native_fixtures import admit_message
        with sqlite3.connect(home/'state.db') as c:
            admit_message(c,home,sid,message,turn,sender='transport-alias' if sid=='human' else 'owner')
    m.invoke_hook(name,session_id=sid,turn_id=turn,user_message=message,platform='whatsapp' if sid!='other' else 'telegram',**kw)
for sid in ('cron','delegate','unknown'): hook('pre_llm_call',sid,producer='human')
assert not controller.store.jobs()
hook('pre_llm_call',sender_id='transport-alias'); hook('post_llm_call',assistant_response='I will draft the Cedar brief within one hour.')

assert len(controller.store.jobs())==2,controller.store.jobs()
for _ in range(3): run('tick')
items=controller.store.items(); assert len(items)==2,controller.store.jobs()
assert all(i['actor']=='assistant' and i['principal']=='person' and i['artifact'] for i in items),items
with controller.store.tx() as c:
    artifacts=[json.loads(r[0]) for r in c.execute('SELECT body FROM artifacts')]
assert any(b.get('type')=='local_draft' and 'Thursday' in b['markdown'] and len(b['provenance'])==3 for b in artifacts)
assert len(reads)==2,reads
for item in items:
    path=controller.store.root/'drafts'/(item['artifact']+'.md')
    assert path.is_file() and 'Thursday' in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
hook('pre_llm_call',sid='other',turn='2',message='I have completed the task.',sender_id='owner'); run('tick')
assert controller.store.event('native:human:1:human')['surface']=='whatsapp'
assert controller.store.event('native:other:2:human')['surface']=='telegram'
assert controller.store.event('native:other:2:human')['principal']=='person'
assert not any(i['state']=='done' for i in controller.store.items())
selected=next(i for i in controller.store.items() if i['state']=='waiting')
run('close',selected['id'],'--revision',str(selected['revision']))
assert sum(i['state']=='done' for i in controller.store.items())==1,controller.store.items()
assert next(i for i in controller.store.items() if not i['parent'])['state']=='open'  # no implicit parent cascade
before=len(calls); run('tick'); assert len(calls)==before
assert all(kw.get('max_tokens')<=1500 and not kw.get('tools') for kw in calls)
print(json.dumps({'native_vertical':'PASS','hook_events':3,'items':2,'selected_closed_only':True,'drafts':sum(b.get('type')=='local_draft' for b in artifacts),'calendar_reads':len(reads),'selected_contexts_per_draft':3,'model_calls':len(calls),'external_sends':0,'draft_preview':next(b['markdown'] for b in artifacts if b.get('type')=='local_draft')}))
