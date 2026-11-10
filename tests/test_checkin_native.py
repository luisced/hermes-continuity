"""Native plugin/CLI/LLM/scoped dispatch, with fixture providers and transport only."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.test_hermes_contract import HOST, ROOT


class NativeCheckinTests(unittest.TestCase):
    def test_native_checkin_cli_reads_before_model_and_sends_question(self):
        code = r'''
import argparse, importlib, json
from types import SimpleNamespace
from unittest.mock import patch
from hermes_cli.plugins import PluginManager
from hermes_constants import get_hermes_home
from agent.plugin_llm import make_plugin_llm_for_test, _TrustPolicy
home=get_hermes_home(); manager=PluginManager(); manager.discover_and_load()
schedule_before={str(p.relative_to(home)):p.read_bytes() for p in (home/'cron').rglob('*') if p.is_file()}
entry=manager._cli_commands['continuity']; controller=entry['handler_fn'].__self__; ctx=controller.ctx
parser=argparse.ArgumentParser(); entry['setup_fn'](parser)
def run(*args): return entry['handler_fn'](parser.parse_args(list(args)))
cli=importlib.import_module(type(controller).__module__)
delivery=importlib.import_module(cli.__package__+'.delivery')
NOW=1793613600
order=[]
def read(args,**kwargs):
    order.append('read')
    return json.dumps({'events':[], 'window':{'start':NOW-86400,'end':NOW+86400}})
ctx.register_tool(name='checkin_fixture_read',toolset='fixture',schema={'name':'checkin_fixture_read','description':'Fixture bounded agenda',
    'annotations':{'readOnlyHint':True,'continuityScopedRead':{'tool':'checkin_fixture_read','args':{'calendar':'approved'}}},
    'parameters':{'type':'object','properties':{'calendar':{'type':'string'}},'required':['calendar']}},handler=read)
schema={'type':'object','additionalProperties':False,'required':['events','window'],'properties':{
    'events':{'type':'array','maxItems':0,'items':{'type':'string'}},
    'window':{'type':'object','additionalProperties':False,'required':['start','end'],'properties':{'start':{'type':'number'},'end':{'type':'number'}}}}}
mappings=[{'type':'host-read','role':'agenda','scope':'selected','mapping':{'tool':'checkin_fixture_read','args':{'calendar':'approved'},'schema':schema}}]
(home/'mappings.json').write_text(json.dumps(mappings))
run('setup','--identity','terminal:fixture=fixture','--timezone','UTC','--allow-model','--mode','active','--adapters',str(home/'mappings.json'),'--delivery-target','telegram:fixture','--allow-delivery')
controller.store.create_item('commitment','fixture','el documento pendiente','fixture:request','el documento pendiente')
config={'enabled':True,'allow_reads':True,'principal':'fixture','primary':'ledger','calendars':['calendar_work'],
        'sources':{'calendar_work':[{'role':'agenda','scope':'selected'}]}}
(home/'checkin.json').write_text(json.dumps(config)); run('checkin-config',str(home/'checkin.json'))
def caller(**kw):
    order.append('model')
    assert 'read' in order
    encoded=json.dumps(kw,ensure_ascii=False)
    assert 'el documento pendiente' in encoded and 'UNAVAILABLE' in encoded
    assert kw['max_tokens']==700 and kw['timeout']==45
    assert 'tools' not in kw
    content={'message':'Sobre el documento pendiente, ¿qué te ayudaría hoy?', 'citations':[{'source':'ledger','quote':'el documento pendiente'}]}
    return ('fixture','fixture',json.loads(json.dumps({'choices':[{'message':{'content':json.dumps(content)}}]}),object_hook=lambda d:SimpleNamespace(**d)))
ctx._llm=make_plugin_llm_for_test(plugin_id='hermes-continuity',policy=_TrustPolicy(plugin_id='hermes-continuity'),sync_caller=caller)
sent=[]
def send(self,message):
    order.append('send'); sent.append(message)
    assert self.destination=='telegram:fixture'
    return {'receipt':'fixture-native-receipt'}
worker=cli.Worker
def fixed_worker(*args,**kwargs): return worker(*args,**kwargs,clock=lambda:NOW)
with patch.object(cli,'Worker',fixed_worker), patch.object(delivery.HermesSend,'__call__',send):
    first=run('tick'); second=run('tick')
assert first['acknowledged']==1 and second['acknowledged']==0
assert first['inference']['max_turns']==1 and first['inference']['attempts']==1
assert second['inference']['attempts']==0
assert order==['read','model','read','send'],order
assert sent==['Sobre el documento pendiente, ¿qué te ayudaría hoy?'],sent
status=run('checkin-status')
assert status['days'][0]['state']=='acknowledged'
assert {str(p.relative_to(home)):p.read_bytes() for p in (home/'cron').rglob('*') if p.is_file()}==schedule_before, 'checkin changed scheduler state'
print('NATIVE CHECKIN FIXTURE PASS: scoped read -> PluginLlm -> reread -> validated question -> one receipt; no live services')
'''
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / 'profile'
            plug = home / 'plugins' / 'hermes-continuity'
            plug.parent.mkdir(parents=True)
            shutil.copytree(ROOT, plug, ignore=shutil.ignore_patterns('.git', 'dist', 'evidence', '__pycache__'))
            (home / 'config.yaml').write_text('agent:\n  max_turns: 1\nplugins:\n  enabled: [hermes-continuity]\n')
            spec = importlib.util.find_spec('hermes_cli')
            host_root = str(Path(next(iter(spec.submodule_search_locations))).parent)
            env = dict({k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'LC_ALL', 'LD_LIBRARY_PATH', 'SSL_CERT_FILE')},
                       PYTHONPATH=os.pathsep.join([host_root, *[p for p in sys.path if p.endswith('site-packages')]]),
                       HOME=d, HERMES_HOME=str(home), TMPDIR=d, HERMES_DISABLE_LAZY_INSTALLS='1',
                       HERMES_ENABLE_PROJECT_PLUGINS='0', PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run([str(HOST), '-c', code], cwd=d, env=env, text=True, capture_output=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('NATIVE CHECKIN FIXTURE PASS', result.stdout)
            print(result.stdout.splitlines()[-1])
