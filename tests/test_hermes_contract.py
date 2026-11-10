"""Actual host objects; transport/provider remains synthetic."""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
HOST=next((Path(p).parents[2]/'bin/python' for p in sys.path if p.endswith('site-packages') and (Path(p).parents[2]/'pyvenv.cfg').is_file()),Path(sys.executable))
ROOT=Path(__file__).resolve().parents[1]

class NativeTests(unittest.TestCase):
    def test_native_loader_cli_llm_ticker_and_cleanup(self):
        code=r'''
import json, pathlib, sys
from types import SimpleNamespace
from hermes_cli.plugins import PluginManager
from hermes_constants import get_hermes_home
home=get_hermes_home()
m=PluginManager(); m.discover_and_load()
assert 'continuity' in m._cli_commands, [(k,v.error) for k,v in m._plugins.items()]
entry=m._cli_commands['continuity']
import argparse
p=argparse.ArgumentParser(); entry['setup_fn'](p)
def run(*args): return entry['handler_fn'](p.parse_args(list(args)))
run('setup','--identity','terminal:owner=person','--identity','desktop:owner=person','--timezone','UTC','--allow-model')
run('ingest','--surface','terminal','--identity','owner','--id','human:1','--text','Please prepare document')
# The registered worker closes over the actual PluginContext. Inject only its
# host test seam; production uses only ctx.llm, no private host APIs.
from agent.plugin_llm import make_plugin_llm_for_test, _TrustPolicy
ctx=entry['handler_fn'].__self__.ctx
calls=[]
def caller(**kw):
    calls.append(kw)
    return ('synthetic','synthetic',json.loads(json.dumps({'choices':[{'message':{'content':json.dumps({'proposals':[{'kind':'commitment','owner':'assistant','source':'human:1','quote':'Please prepare document','title':'Document'}]})}}]}),object_hook=lambda d:SimpleNamespace(**d)))
ctx._llm=make_plugin_llm_for_test(plugin_id='hermes-continuity',policy=_TrustPolicy(plugin_id='hermes-continuity'),sync_caller=caller)
run('tick'); assert len(calls)==1
assert calls[0]['max_tokens']==1500
run('list'); assert len(entry['handler_fn'].__self__.store.items())==1
# Real scoped host registry dispatch with a synthetic provider implementation.
provider_calls=[]
def read_provider(args,**kw):
    provider_calls.append(args); return json.dumps({'events':[{'id':'synthetic-event'}]})
ctx.register_tool(name='continuity_synthetic_read',toolset='synthetic',schema={'name':'continuity_synthetic_read','description':'Synthetic calendar read','annotations':{'readOnlyHint':True,'continuityScopedRead':{'tool':'continuity_synthetic_read','args':{'calendar':'selected'}}},'parameters':{'type':'object','properties':{'calendar':{'type':'string'}},'required':['calendar']}},handler=read_provider)
(home/'selected-notes').mkdir(); (home/'selected-notes'/'outline.md').write_text('synthetic outline')
config=[{'type':'markdown','role':'notes','scope':'selected','root':str(home/'selected-notes')},{'type':'host-read','role':'calendar','scope':'selected','mapping':{'tool':'continuity_synthetic_read','args':{'calendar':'selected'},'schema':{'type':'object','required':['events'],'additionalProperties':False,'properties':{'events':{'type':'array','items':{'type':'object','required':['id'],'additionalProperties':False,'properties':{'id':{'type':'string'}}}}}}}}]
(home/'read-mappings.json').write_text(json.dumps(config))
run('setup','--mode','prepare','--adapters',str(home/'read-mappings.json'))
item=entry['handler_fn'].__self__.store.items()[0]
run('setup','--deny-model')
(home/'selected-notes').rename(home/'offline-notes')
run('prepare',item['id'],'--revision','0','--role','calendar','--scope','selected')
run('tick'); assert provider_calls==[{'calendar':'selected'}],provider_calls
assert len(calls)==1, 'read worker unexpectedly called model'
(home/'offline-notes').rename(home/'selected-notes')

# Hooks cannot invent authenticated human provenance, even with plausible fields.
before=len(entry['handler_fn'].__self__.store.jobs())
m.invoke_hook('pre_llm_call',session_id='cron',turn_id='evil',sender_id='owner',platform='terminal',user_message='human: please send',producer='human')
assert len(entry['handler_fn'].__self__.store.jobs())==before
from cron.jobs import create_job, list_jobs
unrelated=create_job(prompt='synthetic unrelated',schedule='1d',name='unrelated',deliver='local')['id']
run('setup','--ticker'); run('setup','--ticker')
from concurrent.futures import ThreadPoolExecutor
import subprocess
def concurrent_setup(_):
    r=subprocess.run([sys.executable,'-m','hermes_cli.main','continuity','setup','--ticker'],text=True,capture_output=True,timeout=40)
    assert r.returncode==0,r.stdout+r.stderr
with ThreadPoolExecutor(3) as pool: list(pool.map(concurrent_setup,range(3)))
jobs=list_jobs(include_disabled=True); owned=[j for j in jobs if j['name'].startswith('hermes-continuity:')]
assert len(owned)==1, jobs
assert owned[0]['no_agent'] and owned[0]['deliver']=='local'
# Real ticker subprocess processes scoped Markdown work, with no provider call.
run('prepare',item['id'],'--revision','0','--role','notes','--scope','selected','--path','outline.md')
# Real supported host script execution launches the worker in another process.
from cron.scheduler_script import _run_job_script
ok,output=_run_job_script(owned[0]['script']); assert ok,output
assert '"processed": 1' in output, output
# Disabling leaves a harmless job until owner cleanup; subprocess does no work.
config_path=home/'config.yaml'; config_path.write_text('plugins:\n  enabled: []\n  disabled: [hermes-continuity]\n')
ok,disabled_output=_run_job_script(owned[0]['script']); assert ok and '"disabled": true' in disabled_output,(ok,disabled_output)
config_path.write_text('plugins:\n  enabled: [hermes-continuity]\n')
run('close',item['id'],'--revision','0')
assert entry['handler_fn'].__self__.store.items()[0]['state']=='done'
run('pause'); run('remove')
assert [j['id'] for j in list_jobs(include_disabled=True)]==[unrelated]
assert not list((home/'scripts').glob('continuity-*.py'))
run('forget'); assert not entry['handler_fn'].__self__.store.items()
print('NATIVE CONTRACT PASS')
'''
        with tempfile.TemporaryDirectory() as d:
            home=Path(d)/'profile'; plug=home/'plugins'/'hermes-continuity'; plug.parent.mkdir(parents=True)
            import shutil
            shutil.copytree(ROOT,plug,ignore=shutil.ignore_patterns('.git','.agents','.codex','.aws','dist','evidence','__pycache__'))
            (home/'config.yaml').write_text('plugins:\n  enabled: [hermes-continuity]\n  entries: {}\n')
            import importlib.util
            host_root=str(Path(next(iter(importlib.util.find_spec('hermes_cli').submodule_search_locations))).parent)
            env=dict({k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL','SYSTEMROOT','WINDIR','LD_LIBRARY_PATH','SSL_CERT_FILE')},PYTHONPATH=os.pathsep.join([host_root,*[p for p in sys.path if p.endswith('site-packages')]]),HERMES_DISABLE_LAZY_INSTALLS='1',HOME=d,HERMES_HOME=str(home),HERMES_ENABLE_PROJECT_PLUGINS='0',PYTHONDONTWRITEBYTECODE='1')
            result=subprocess.run([str(HOST),'-c',code],env=env,text=True,capture_output=True,cwd=d,timeout=90)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('NATIVE CONTRACT PASS',result.stdout)

    def test_native_automatic_vertical(self):
        with tempfile.TemporaryDirectory() as d:
            home=Path(d)/'profile'; plug=home/'plugins'/'hermes-continuity'; plug.parent.mkdir(parents=True)
            import shutil,importlib.util
            shutil.copytree(ROOT,plug,ignore=shutil.ignore_patterns('.git','.agents','.codex','.aws','dist','evidence','__pycache__'))
            (home/'config.yaml').write_text('plugins:\n  enabled: [hermes-continuity]\n')
            host_root=str(Path(next(iter(importlib.util.find_spec('hermes_cli').submodule_search_locations))).parent)
            env=dict({k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL','LD_LIBRARY_PATH','SSL_CERT_FILE')},PYTHONPATH=os.pathsep.join([host_root,*[p for p in sys.path if p.endswith('site-packages')]]),HOME=d,HERMES_HOME=str(home),TMPDIR=d,HERMES_DISABLE_LAZY_INSTALLS='1',PYTHONDONTWRITEBYTECODE='1',HERMES_ENABLE_PROJECT_PLUGINS='0')
            result=subprocess.run([str(HOST),str(plug/'tests/native_vertical_case.py')],env=env,text=True,capture_output=True,cwd=d,timeout=40)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('"native_vertical": "PASS"',result.stdout)
            print(result.stdout.splitlines()[-1])
