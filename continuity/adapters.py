"""Explicit read mappings. Context is never authoritative completion or permission."""
import json
import os
import contextvars
import contextlib
import queue
import threading
from pathlib import Path
from .store import secure_dir

MAX_RESPONSE=64000
# Executor checks admission in the actual dispatch thread, not its caller.
EFFECT_ADMISSION=contextvars.ContextVar('continuity_effect_admission',default=None)

def admit_effect():
    check=EFFECT_ADMISSION.get()
    if check: check()


def validate_schema(value,schema,depth=0):
    if depth>20 or type(schema) is not dict: raise ValueError('schema depth')
    kind=schema.get('type')
    allowed={'type','enum'}|{'object':{'properties','required','additionalProperties'},'array':{'items','maxItems'},'string':{'maxLength'}}.get(kind,set())
    if set(schema)-allowed: raise ValueError('unsupported schema keyword')
    types={'object':dict,'array':list,'string':str,'boolean':bool,'integer':int,'number':(int,float),'null':type(None)}
    if kind not in types or not isinstance(value,types[kind]) or (kind in ('integer','number') and isinstance(value,bool)): raise ValueError('response type')
    if 'enum' in schema and value not in schema['enum']: raise ValueError('enum')
    if kind=='object':
        if schema.get('additionalProperties') is not False: raise ValueError('closed schema required')
        properties=schema.get('properties',{})
        if set(value)-set(properties) or not set(schema.get('required',[]))<=set(value): raise ValueError('response keys')
        for key,v in value.items(): validate_schema(v,properties[key],depth+1)
    if kind=='array':
        if len(value)>min(schema.get('maxItems',1000),1000): raise ValueError('array bound')
        for v in value: validate_schema(v,schema['items'],depth+1)
    if kind=='string' and len(value)>min(schema.get('maxLength',MAX_RESPONSE),MAX_RESPONSE): raise ValueError('string bound')

class MarkdownAdapter:
    def __init__(self,root):
        self.root=Path(root).absolute()
        if not self.root.is_dir() or any(p.is_symlink() for p in (self.root,*self.root.parents)): raise ValueError('unsafe notes scope')
        st=self.root.stat(); self.identity=(st.st_dev,st.st_ino)
    def read(self,path):
        admit_effect()
        rel=Path(path)
        if rel.is_absolute() or '..' in rel.parts or rel.suffix.lower()!='.md': raise ValueError('notes scope')
        p=self.root/rel
        if any(q.is_symlink() for q in (p,*p.parents)): raise ValueError('symlink notes')
        p.resolve().relative_to(self.root.resolve())
        # Resolve every ancestor from / with no symlink following. Identity is
        # pinned when this capability is constructed, so replacement by another real directory fails.
        directory=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            for segment in self.root.parts[1:]:
                next_fd=os.open(segment,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
                os.close(directory); directory=next_fd
            st=os.fstat(directory)
            if (st.st_dev,st.st_ino)!=self.identity: raise ValueError('notes root replaced')
        except BaseException:
            os.close(directory); raise
        try:
            for segment in rel.parts[:-1]:
                next_fd=os.open(segment,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
                os.close(directory); directory=next_fd
            fd=os.open(rel.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            import stat
            with os.fdopen(fd,'rb') as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode): raise ValueError('regular Markdown file required')
                body=f.read(MAX_RESPONSE+1)
        finally: os.close(directory)
        if len(body)>MAX_RESPONSE: raise ValueError('response bound')
        return {'text':body.decode('utf-8'),'source':str(rel),'authority':'context only'}

class HostReadAdapter:
    _lock=threading.Lock()
    _inflight=set()
    def __init__(self,mapping,dispatch,describe=None,timeout=10):
        if set(mapping)-{'tool','args','schema','operation'} or mapping.get('operation','read')!='read': raise ValueError('read mapping only')
        if type(mapping.get('tool')) is not str or type(mapping.get('args')) is not dict or type(mapping.get('schema')) is not dict: raise ValueError('mapping')
        self.mapping=json.loads(json.dumps(mapping)); self.dispatch=dispatch; self.describe=describe; self.timeout=min(timeout,10)
    def read(self,args=None):
        if args is not None: raise ValueError('arguments fixed by owner mapping')
        descriptor=self.describe(self.mapping['tool']) if self.describe else None
        annotations=(descriptor or {}).get('annotations',{})
        if annotations.get('continuityScopedRead') != {'tool':self.mapping['tool'],'args':self.mapping['args']} or annotations.get('readOnlyHint') is not True or annotations.get('destructiveHint',False): raise ValueError('host tool does not declare read-only capability')
        def selectors(value):
            if isinstance(value,dict):
                for k,v in value.items():
                    if k.lower() in ('action','operation','method','mode') and v not in ('read','get','list','search','query','fetch','lookup'): raise ValueError('mutating or unknown selector')
                    selectors(v)
            elif isinstance(value,list):
                for v in value: selectors(v)
        selectors(self.mapping['args'])
        key=(getattr(self.dispatch,'__self__',self.dispatch),json.dumps(self.mapping,sort_keys=True))
        response=queue.Queue(maxsize=1)
        with self._lock:
            if key in self._inflight: raise TimeoutError('read still in flight')
            self._inflight.add(key)
        def invoke():
            try:
                admit_effect()
                result=self.dispatch(self.mapping['tool'],json.loads(json.dumps(self.mapping['args'])))
                response.put((True,result))
            except BaseException as exc: response.put((False,exc))
            finally:
                with self._lock: self._inflight.discard(key)
        context=contextvars.copy_context()
        threading.Thread(target=lambda:context.run(invoke),daemon=True,name='continuity-read').start()
        try: ok,result=response.get(timeout=self.timeout)
        except queue.Empty: raise TimeoutError('read timed out; completion unverified') from None
        if not ok: raise result
        if isinstance(result,str):
            if len(result)>MAX_RESPONSE: raise ValueError('response bound')
            result=json.loads(result)
        if len(json.dumps(result))>MAX_RESPONSE: raise ValueError('response bound')
        validate_schema(result,self.mapping['schema'])
        return result

class AdapterRegistry:
    def __init__(self): self.entries={}
    def add(self,role,scope,adapter): self.entries[(role,scope)]=adapter
    def select(self,role,scope,operation):
        if operation!='read' or (role,scope) not in self.entries: raise ValueError('capability unavailable')
        adapter=self.entries[(role,scope)]
        return adapter() if callable(adapter) else adapter

# Public `composio execute SLUG --account ACCOUNT --data JSON` contract.
# This explicit allowlist must be code-reviewed before adding any action.
import subprocess
from datetime import datetime
import shutil
COMPOSIO=shutil.which('composio')
class ComposioReadAdapter:
    ACTIONS=frozenset({'GOOGLECALENDAR_FIND_EVENT'})
    def __init__(self,mapping):
        keys={'action','account','calendar_id','time_min','time_max','max_results'}
        if type(mapping) is not dict or set(mapping)!=keys or mapping['action'] not in self.ACTIONS: raise ValueError('allowlisted read action required')
        if any(type(mapping[k]) is not str or not mapping[k] or len(mapping[k])>250 or mapping[k].startswith('-') for k in keys-{'max_results'}): raise ValueError('fixed scope required')
        if type(mapping['max_results']) is not int or not 1<=mapping['max_results']<=20: raise ValueError('result bound')
        start=datetime.fromisoformat(mapping['time_min'].replace('Z','+00:00')); end=datetime.fromisoformat(mapping['time_max'].replace('Z','+00:00'))
        if start.tzinfo is None or end.tzinfo is None or not 0<(end-start).total_seconds()<=31*86400: raise ValueError('bounded aware calendar window required')
        self.mapping=json.loads(json.dumps(mapping))
    def read(self,args=None):
        if args is not None: raise ValueError('operator-fixed parameters only')
        admit_effect()
        m=self.mapping
        if COMPOSIO is None: raise ValueError('Composio CLI unavailable')
        data=dict(calendar_id=m['calendar_id'],time_min=m['time_min'],time_max=m['time_max'],max_results=m['max_results'])
        r=subprocess.run([COMPOSIO,'execute',m['action'],'--account',m['account'],'--data',json.dumps(data)],shell=False,text=True,capture_output=True,timeout=15)
        if r.returncode or len(r.stdout)>MAX_RESPONSE: raise ValueError('calendar read failed or oversized')
        result=json.loads(r.stdout)
        if type(result) is not dict or result.get('successful') is not True or result.get('error'): raise ValueError('calendar success unverified')
        data=result.get('data')
        if type(data) is not dict: raise ValueError('calendar data shape')
        # Composio's native FIND_EVENT response nests its list and pagination
        # under event_data.event_data; Google-style flat lists remain supported.
        if type(data.get('event_data')) is dict: data=data['event_data']
        if data.get('nextPageToken') or data.get('next_page_token'): raise ValueError('calendar result incomplete; narrow window or paginate')
        events=data.get('items',data.get('events',data.get('event_data')))
        if type(events) is not list or len(events)>m['max_results']: raise ValueError('calendar result bound')
        minimal=[]
        for event in events:
            if type(event) is not dict: raise ValueError('calendar event shape')
            selected={k:event[k] for k in ('summary','start','end','status') if k in event}
            if any(type(selected[k]) is not str for k in ('summary','status') if k in selected): raise ValueError('calendar text field shape')
            if any(type(v) not in (str,dict) for v in selected.values()): raise ValueError('calendar field shape')
            # Drop descriptions, attendees, links, organizer and arbitrary nested fields.
            for k in ('start','end'):
                if k in selected and type(selected[k]) is dict: selected[k]={a:b for a,b in selected[k].items() if a in ('date','dateTime','timeZone') and type(b) is str}
            minimal.append(selected)
        return {'events':minimal,'authority':'context only','window':{'start':m['time_min'],'end':m['time_max']}}


class DayCalendarAdapter:
    """Fixed account/calendar authority, locally computed rolling day window."""
    def __init__(self, mapping, clock=None):
        from zoneinfo import ZoneInfo
        import time
        if type(mapping) is not dict or set(mapping) != {'action','account','calendar_id','timezone','max_results'}:
            raise ValueError('rolling calendar mapping')
        ZoneInfo(mapping['timezone'])
        self.mapping=json.loads(json.dumps(mapping)); self.clock=clock or time.time
        # Validate the same fixed action/account bounds before accepting config.
        ComposioReadAdapter(dict({k:v for k,v in mapping.items() if k!='timezone'},
                                 time_min='2000-01-01T00:00:00+00:00',time_max='2000-01-02T00:00:00+00:00'))
    def read(self, args=None):
        from datetime import timedelta
        from zoneinfo import ZoneInfo
        if args is not None: raise ValueError('fixed rolling day scope only')
        admit_effect()
        start=datetime.fromtimestamp(self.clock(),ZoneInfo(self.mapping['timezone'])).replace(hour=0,minute=0,second=0,microsecond=0)
        mapping={k:v for k,v in self.mapping.items() if k!='timezone'}
        mapping.update(time_min=start.isoformat(),time_max=(start+timedelta(days=1)).isoformat())
        return ComposioReadAdapter(mapping).read()


class CommandReadAdapter:
    """Trusted OPERATOR-reviewed read helper. Fixed argv, not an agent or sandbox.

    An executable hash pins the helper; operator attestation is not a proof that
    arbitrary helper code is read-only. No model may configure this capability.
    """
    def __init__(self, mapping, clock=None):
        import time
        from zoneinfo import ZoneInfo
        keys={'executable','sha256','args','timezone','approval','timeout','schema'}
        if type(mapping) is not dict or set(mapping)!=keys or mapping.get('approval')!='operator-reviewed-read-only':
            raise ValueError('explicit operator reviewed read capability required')
        if (type(mapping['args']) is not list or len(mapping['args'])>32
                or any(type(a) is not str or len(a)>8000 for a in mapping['args'])
                or type(mapping['timeout']) is not int or not 1<=mapping['timeout']<=30
                or type(mapping['schema']) is not dict): raise ValueError('bounded fixed read command')
        if not Path(mapping['executable']).is_absolute(): raise ValueError('absolute read helper executable required')
        ZoneInfo(mapping['timezone'])
        self.mapping=json.loads(json.dumps(mapping)); self.clock=clock or time.time
        self._verify()
    def _verify(self):
        import hashlib
        p=Path(self.mapping['executable'])
        if not p.is_file() or not os.access(p,os.X_OK) or hashlib.sha256(p.read_bytes()).hexdigest()!=self.mapping['sha256']:
            raise ValueError('read helper identity changed')
    def read(self, args=None):
        from datetime import timedelta
        from zoneinfo import ZoneInfo
        if args is not None: raise ValueError('operator-fixed argv only')
        admit_effect(); self._verify()
        start=datetime.fromtimestamp(self.clock(),ZoneInfo(self.mapping['timezone'])).replace(hour=0,minute=0,second=0,microsecond=0)
        replacements={'{day_start}':start.isoformat(),'{day_end}':(start+timedelta(days=1)).isoformat(),
                      '{week_start}':(start-timedelta(days=6)).isoformat()}
        argv=[self.mapping['executable']]
        for arg in self.mapping['args']:
            for token,value in replacements.items(): arg=arg.replace(token,value)
            argv.append(arg)
        result=subprocess.run(argv,shell=False,text=True,capture_output=True,timeout=self.mapping['timeout'],stdin=subprocess.DEVNULL)
        if result.returncode or len(result.stdout)>MAX_RESPONSE: raise ValueError('read helper failed or exceeded bound')
        value=json.loads(result.stdout); validate_schema(value,self.mapping['schema'])
        return value


def authorized_mapping(adapter,role,scope,mappings):
    matches=[m for m in mappings if m.get('role')==role and m.get('scope')==scope]
    if len(matches)!=1: raise ValueError('capability not currently configured')
    m=matches[0]
    if type(adapter) is MarkdownAdapter:
        valid=m.get('type')=='markdown' and str(adapter.root)==str(Path(m.get('root','')).absolute())
        if m.get('root_identity') is not None: valid=valid and list(adapter.identity)==m['root_identity']
    elif type(adapter) is ComposioReadAdapter:
        valid=m.get('type')=='composio-read' and adapter.mapping==m.get('mapping')
    elif type(adapter) is DayCalendarAdapter:
        valid=m.get('type')=='composio-day-read' and adapter.mapping==m.get('mapping')
    elif type(adapter) is CommandReadAdapter:
        valid=m.get('type')=='command-read' and adapter.mapping==m.get('mapping')
    elif type(adapter) is HostReadAdapter:
        valid=m.get('type')=='host-read' and adapter.mapping==m.get('mapping')
    else: valid=False
    if not valid: raise ValueError('selected capability differs from configuration')
    return m
