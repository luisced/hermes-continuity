"""Exactly one explicitly provisioned native script job, with ownership fencing."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import uuid
import time
import sys
from .store import secure_dir

@contextlib.contextmanager
def bounded_lock(path,timeout=5):
    fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:
        until=time.monotonic()+timeout
        while True:
            try:
                fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.monotonic()>=until: raise TimeoutError('continuity lock timeout') from None
                time.sleep(.02)
        try: yield
        finally: fcntl.flock(f,fcntl.LOCK_UN)

@contextlib.contextmanager
def lifecycle_lock(store):
    with bounded_lock(store.root/'lifecycle.lock'): yield

class Ticker:
    def __init__(self,home,store,dispatch): self.home=Path(home); self.store=store; self.dispatch=dispatch
    def api(self,action,**args):
        # Public registry discovery and ctx dispatch, only during explicit setup/remove.
        from tools.registry import registry, discover_builtin_tools
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        token=set_hermes_home_override(self.home)
        try:
            if registry.get_entry('cronjob_manage') is None: discover_builtin_tools()
            result=self.dispatch('cronjob_manage',dict(action=action,**args))
            if isinstance(result,str): result=json.loads(result)
            if not isinstance(result,dict) or result.get('success') is not True: raise RuntimeError('native cron operation failed: '+str(result.get('error','unavailable')))
            return result
        finally: reset_hermes_home_override(token)
    def _owned(self,j,token):
        return j.get('name')=='hermes-continuity:'+token and j.get('script')=='continuity-'+token+'.py'
    def _valid(self,j):
        schedule=j.get('schedule')
        return j.get('no_agent') is True and j.get('deliver')=='local' and j.get('enabled') is True and j.get('state')!='paused' and not j.get('paused_at') and not j.get('prompt') and (schedule=='*/15 * * * *' or isinstance(schedule,dict) and schedule.get('expr')=='*/15 * * * *')
    def setup(self):
        with lifecycle_lock(self.store):
            return self._setup_locked()
    def _setup_locked(self):
        token=self.store.setting('ticker_token')
        if not token:
            token=uuid.uuid4().hex; self.store.configure('ticker_token',token)
        scripts=secure_dir(self.home/'scripts'); path=scripts/('continuity-'+token+'.py')
        source='''# Owned by hermes-continuity; generated only by explicit setup.
import os, sys, json, importlib.util
from pathlib import Path
os.environ['HERMES_DISABLE_LAZY_INSTALLS'] = '1'
if os.path.realpath(sys.executable) != os.path.realpath(PYTHON):
    os.execv(PYTHON, [PYTHON, __file__])
os.environ['HERMES_HOME'] = HOME
spec=importlib.util.find_spec('hermes_cli')
if spec and spec.submodule_search_locations:
    sys.path.insert(0,str(Path(next(iter(spec.submodule_search_locations))).parent))
from hermes_cli.config import load_config_readonly
config=load_config_readonly() or {}
plugins=config.get('plugins',{})
if PACKAGE_EXISTS and 'hermes-continuity' in plugins.get('enabled',[]) and 'hermes-continuity' not in plugins.get('disabled',[]):
    from hermes_cli.main import main
    sys.argv=['hermes','continuity','tick']
    main()
else:
    print(json.dumps({'processed':0,'disabled':True}))
'''.replace('PYTHON',repr(sys.executable)).replace('HOME\n',repr(str(self.home))+'\n').replace('PACKAGE_EXISTS','Path('+repr(str(Path(__file__).resolve().parents[1]/'__init__.py'))+').is_file()')
        if path.is_symlink(): raise ValueError('unsafe ticker script')
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as f: f.write(source)
        owned=[j for j in self.api('list',include_disabled=True)['jobs'] if self._owned(j,token)]
        for duplicate in owned[1:]:
            self.api('remove',job_id=duplicate['job_id'])
        if owned and self._valid(owned[0]): return owned[0]['job_id']
        if owned: self.api('remove',job_id=owned[0]['job_id'])
        job=self.api('create',prompt='',schedule='*/15 * * * *',name='hermes-continuity:'+token,script=path.name,no_agent=True,deliver='local')
        return job['job_id']
    def remove(self):
        with lifecycle_lock(self.store): return self._remove_locked()
    def _remove_locked(self):
        token=self.store.setting('ticker_token')
        if not token: return
        for j in self.api('list',include_disabled=True)['jobs']:
            if self._owned(j,token): self.api('remove',job_id=j['job_id'])
        path=self.home/'scripts'/('continuity-'+token+'.py')
        if path.is_symlink(): raise ValueError('unsafe ticker script')
        path.unlink(missing_ok=True)
