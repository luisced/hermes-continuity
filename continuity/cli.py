"""Local OS-owner CLI. Not registered as an LLM tool or remote slash command."""
import json
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo
from .store import Store, secure_dir
from .intake import OwnerContext, admit_event
from .extraction import Worker
from .ticker import Ticker


def setup_parser(p):
    sub=p.add_subparsers(dest='action',required=True)
    setup=sub.add_parser('setup'); setup.add_argument('--identity',action='append',default=[],metavar='SURFACE:IDENTITY=PRINCIPAL'); setup.add_argument('--timezone'); model=setup.add_mutually_exclusive_group(); model.add_argument('--allow-model',action='store_true'); model.add_argument('--deny-model',action='store_true'); setup.add_argument('--ticker',action='store_true')
    setup.add_argument('--preparation-context',help='JSON file of selected role/scope/path contexts for automatic local drafts'); setup.add_argument('--mode',choices=['shadow','prepare','active']); setup.add_argument('--adapters',help='explicit JSON read mappings file')
    setup.add_argument('--delivery-target',help='your own exact platform:chat_id; never a model route')
    delivery=setup.add_mutually_exclusive_group(); delivery.add_argument('--allow-delivery',action='store_true'); delivery.add_argument('--deny-delivery',action='store_true')
    for command in ('send-pending','list','why','pause','resume','remove','forget','export','tick','status','capabilities'): sub.add_parser(command)
    from .lifecycle import STAGES
    lifecycle=sub.add_parser('lifecycle-list',help='principal-scoped lifecycle page (local OS owner only)')
    lifecycle.add_argument('--principal',required=True); lifecycle.add_argument('--stage',choices=sorted(STAGES))
    lifecycle.add_argument('--after'); lifecycle.add_argument('--limit',type=int,default=50)
    checkin=sub.add_parser('checkin-config'); checkin.add_argument('path',help='operator-authored opt-in JSON; no model-selected tools or destinations')
    workflow=sub.add_parser('workflow-config'); workflow.add_argument('path',help='operator-authored named workflow presets JSON')
    enqueue=sub.add_parser('workflow-enqueue'); enqueue.add_argument('id'); enqueue.add_argument('--revision',required=True,type=int)
    enqueue.add_argument('--workflow',required=True,choices=['local_draft','meeting_brief']); enqueue.add_argument('--preset',required=True)
    sub.add_parser('checkin-status')
    response=sub.add_parser('checkin-response'); response.add_argument('day'); response.add_argument('--event',required=True); response.add_argument('--kind',required=True,choices=['wellbeing','maintenance']); response.add_argument('--followups',help='JSON file of exact existing item/revision links')
    ingest=sub.add_parser('ingest'); ingest.add_argument('--surface',required=True); ingest.add_argument('--identity',required=True); ingest.add_argument('--id',required=True); ingest.add_argument('--text',required=True); ingest.add_argument('--role',choices=['human','assistant'],default='human'); ingest.add_argument('--parent')
    for command in ('close','dismiss','snooze'):
        q=sub.add_parser(command); q.add_argument('id'); q.add_argument('--revision',required=True,type=int)
        if command=='snooze': q.add_argument('--until',required=True,type=float)
    prepare=sub.add_parser('prepare'); prepare.add_argument('id'); prepare.add_argument('--revision',required=True,type=int); prepare.add_argument('--role',required=True); prepare.add_argument('--scope',required=True); prepare.add_argument('--path')
    read=sub.add_parser('read'); read.add_argument('role'); read.add_argument('scope'); read.add_argument('--path')

class Controller:
    def __init__(self,home,ctx):
        self.home=Path(home); self.ctx=ctx; self._store=None
    @property
    def store(self):
        if self._store is None: self._store=Store(self.home/'continuity')
        return self._store
    def describe(self,name):
        from tools.registry import registry
        entry=registry.get_entry(name,scope=str(self.home))
        return entry.schema if entry else None
    def adapters(self):
        from .adapters import AdapterRegistry, MarkdownAdapter, HostReadAdapter, ComposioReadAdapter, DayCalendarAdapter, CommandReadAdapter
        registry=AdapterRegistry()
        for m in self.store.setting('adapters',[]):
            registry.add(m['role'],m['scope'],lambda m=m: MarkdownAdapter(m['root']) if m['type']=='markdown' else ComposioReadAdapter(m['mapping']) if m['type']=='composio-read' else DayCalendarAdapter(m['mapping']) if m['type']=='composio-day-read' else CommandReadAdapter(m['mapping']) if m['type']=='command-read' else HostReadAdapter(m['mapping'],self.ctx.dispatch_tool,self.describe))
        return registry
    def handle(self,args):
        if args.action=='setup':
            from .ticker import lifecycle_lock
            with lifecycle_lock(self.store): return self._handle(args)
        return self._handle(args)
    def _handle(self,args):
        s=self.store; a=args.action; result={'ok':True}
        if a=='setup':
            identities=s.setting('identities',{})
            for value in args.identity:
                key,principal=value.split('=',1)
                if ':' not in key or not principal: raise ValueError('identity mapping')
                identities[key]=principal
            s.configure('identities',identities)
            if args.timezone: ZoneInfo(args.timezone); s.configure('timezone',args.timezone)
            if args.allow_model: s.configure('allow_model',True)
            if args.deny_model:
                paused=s.setting('paused',False); s.pause(True); s.configure('allow_model',False); s.pause(paused)
            if args.mode: s.configure('mode',args.mode)
            if getattr(args,'delivery_target',None):
                from .delivery import target
                s.configure('allow_delivery',False); s.configure('delivery_target',target(args.delivery_target)); s.pause(s.setting('paused',False))
            if getattr(args,'allow_delivery',False):
                from .delivery import target
                target(s.setting('delivery_target','')); s.configure('allow_delivery',True)
            if getattr(args,'deny_delivery',False):
                s.configure('allow_delivery',False)
                # Fence planned effects across revoke/re-enable, preserving paused state.
                s.pause(s.setting('paused',False))
            if args.adapters:
                mappings=json.loads(Path(args.adapters).read_text())
                if type(mappings) is not list: raise ValueError('adapter list')
                for m in mappings:
                    if set(m)-{'role','scope','type','root','mapping'} or m.get('type') not in ('markdown','host-read','composio-read','composio-day-read','command-read') or not all(type(m.get(k)) is str and m[k] for k in ('role','scope')): raise ValueError('adapter configuration')
                    from .adapters import MarkdownAdapter,HostReadAdapter,ComposioReadAdapter,DayCalendarAdapter,CommandReadAdapter
                    if m['type']=='markdown': m['root_identity']=list(MarkdownAdapter(m['root']).identity)
                    elif m['type']=='composio-read': ComposioReadAdapter(m['mapping'])
                    elif m['type']=='composio-day-read': DayCalendarAdapter(m['mapping'])
                    elif m['type']=='command-read': CommandReadAdapter(m['mapping'])
                    else: HostReadAdapter(m['mapping'],self.ctx.dispatch_tool,self.describe)
                s.configure('adapters',mappings)
            if args.preparation_context:
                selections=json.loads(Path(args.preparation_context).read_text())
                if type(selections) is not list or len(selections)>4: raise ValueError('bounded preparation context list')
                for selection in selections:
                    if type(selection) is not dict or set(selection)-{'role','scope','path'} or not all(type(selection.get(k)) is str and selection[k] for k in ('role','scope')): raise ValueError('context selector')
                    self.adapters().select(selection['role'],selection['scope'],'read')
                s.configure('preparation_context',selections)
            if args.ticker:
                if not identities or not (s.setting('allow_model',False) or (s.setting('mode','shadow') in ('prepare','active') and s.setting('adapters',[]))): raise ValueError('ticker needs identities and explicit model or read preparation consent')
                result['ticker']=Ticker(self.home,s,self.ctx.dispatch_tool)._setup_locked()
        elif a=='checkin-config':
            from .checkin import validate_config
            config=validate_config(json.loads(Path(args.path).read_text()))
            s.configure('checkin',config)
        elif a=='workflow-config':
            from .workflow_config import validate_workflow_config
            from .adapters import authorized_mapping
            config=validate_workflow_config(json.loads(Path(args.path).read_text()))
            mappings=s.setting('adapters',[]); registry=self.adapters()
            principals=set(s.setting('identities',{}).values())
            for preset in config['presets'].values():
                if preset['principal'] not in principals: raise ValueError('configured principal required')
                for selection in preset['selectors']:
                    adapter=registry.select(selection['role'],selection['scope'],'read')
                    mapping=authorized_mapping(adapter,selection['role'],selection['scope'],mappings)
                    if ('path' in selection)!=(mapping['type']=='markdown'): raise ValueError('path only for selected Markdown capability')
            s.configure('workflows',config)
        elif a=='workflow-enqueue':
            from .workflows import WorkflowRunner
            result={'job':WorkflowRunner(s,None,None,None).enqueue(args.id,args.revision,args.workflow,args.preset,now=time.time())}
        elif a=='checkin-status':
            with s.tx() as c:
                result={'days':[dict(r) for r in c.execute('SELECT * FROM checkin_days ORDER BY day')], 'responses':[dict(r) for r in c.execute('SELECT * FROM checkin_responses')]}
        elif a=='checkin-response':
            from .checkin import Checkins
            links=json.loads(Path(args.followups).read_text()) if args.followups else []
            result={'associated':Checkins(s).record_response(args.day,args.event,args.kind,links)}
        elif a=='ingest':
            message={'id':args.id,'text':args.text,'role':args.role}
            if args.parent: message['parent']=args.parent
            result['admitted']=s.ingest(admit_event(s,OwnerContext.local(args.surface,args.identity),message))
        elif a=='send-pending':
            from .delivery import deliver_pending
            result={'acknowledged':deliver_pending(s,time.time())}
        elif a=='tick':
            consent=s.setting('allow_model',False)
            result=Worker(s,self.ctx.llm if consent else None,adapters=self.adapters,allow_model=consent).tick()
        elif a=='lifecycle-list':
            from .lifecycle import Lifecycle
            result=Lifecycle(s).page(args.principal,stage=args.stage,after=args.after,limit=args.limit)
        elif a in ('list','why'):
            with s.tx() as c:
                result={'items':[dict(r) for r in c.execute('SELECT * FROM items')],'jobs':[dict(r) for r in c.execute('SELECT * FROM jobs')],'artifacts':[dict(r) for r in c.execute('SELECT * FROM artifacts')],'transitions':[dict(r) for r in c.execute('SELECT * FROM item_events')],
                        'inference':{'executions':[dict(r) for r in c.execute('SELECT * FROM inference_executions ORDER BY rowid')],
                                     'blocks':[dict(r) for r in c.execute("SELECT * FROM inference_blocks WHERE reason<>'inference_attempted'")]}}
        elif a=='prepare':
            payload={'item':args.id,'revision':args.revision,'role':args.role,'scope':args.scope,'path':args.path}
            key='read:'+json.dumps(payload,sort_keys=True)
            result={'job':s.enqueue('read',payload,key,0)}
        elif a in ('close','dismiss','snooze'):
            s.transition(args.id,args.revision,{'close':'done','dismiss':'dismissed','snooze':'snoozed'}[a],review=getattr(args,'until',None))
        elif a in ('pause','resume'): s.pause(a=='pause')
        elif a=='remove': s.pause(True); Ticker(self.home,s,self.ctx.dispatch_tool).remove()
        elif a=='forget':
            from .ticker import lifecycle_lock
            with lifecycle_lock(s):
                cleanup=s._forget_locked()
                if cleanup['cleanup_required']: result['file_residue']=cleanup
                try: Ticker(self.home,s,self.ctx.dispatch_tool)._remove_locked()
                except Exception as exc: result['scheduler_residue']={'cleanup_required':True,'reason':type(exc).__name__}
                else:
                    with s.tx() as c: c.execute("DELETE FROM settings WHERE key='ticker_token'")
        elif a=='read':
            if s.setting('mode','shadow') not in ('prepare','active') or s.setting('paused',False): raise ValueError('explicit prepare mode required for adapters')
            adapter=self.adapters().select(args.role,args.scope,'read')
            from .adapters import authorized_mapping,EFFECT_ADMISSION
            epoch=s.setting('epoch')
            def admission():
                with s.authorization_lock():
                    with s.tx() as c:
                        if s._epoch(c)!=epoch or not s._enabled(c) or Worker.setting_in(c,'mode','shadow') not in ('prepare','active'): raise ValueError('read consent revoked')
                        authorized_mapping(adapter,args.role,args.scope,Worker.setting_in(c,'adapters',[]))
            token=EFFECT_ADMISSION.set(admission)
            try:
                admission()
                result={'context':adapter.read(args.path) if args.path is not None else adapter.read(),'authority':'context only; does not close items'}
            finally: EFFECT_ADMISSION.reset(token)
        elif a=='capabilities':
            from tools.registry import registry
            from hermes_constants import set_hermes_home_override,reset_hermes_home_override
            token=set_hermes_home_override(self.home)
            try:
                tools=[{'name':e.name,'toolset':e.toolset,'read_only':e.schema.get('annotations',{}).get('readOnlyHint') is True,'description':e.schema.get('description','')} for e in registry.get_all_entries()]
            finally: reset_hermes_home_override(token)
            result={'registered_tools':tools,'supported':['scoped Markdown read','explicit fixed host read mappings'],'unsupported':['external memory writes'],'delivery':'disabled unless owner opts in to fixed own destination','mode':s.setting('mode','shadow')}
        elif a=='export':
            p=s.root/('export-'+str(time.time_ns())+'.json')
            fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
            with s.tx() as c: artifacts=[dict(r) for r in c.execute('SELECT * FROM artifacts')]
            with os.fdopen(fd,'w') as f: json.dump({'items':s.items(),'artifacts':artifacts},f)
            result={'path':str(p),'limits':'plugin logical data only; native history and backups unaffected'}
        elif a=='status':
            from .followthrough import activity
            from .lifecycle import Lifecycle
            result={'mode':s.setting('mode','shadow'),'paused':s.setting('paused',False),'delivery_consent':s.setting('allow_delivery',False),'delivery_target':s.setting('delivery_target'),'model_consent':s.setting('allow_model',False),'pending':len([j for j in s.jobs() if j['state']!='done']),
                    'activity':activity(s),'followthrough':[Lifecycle(s).snapshot(i['id'],i['principal']) for i in s.items()]}
        print(json.dumps(result,ensure_ascii=False)); return result
