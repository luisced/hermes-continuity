#!/usr/bin/env python3
"""Synthetic behavior acceptance plus native loader/CLI from extracted release.

Fixture proposals test enforcement, not LLM semantic quality. No credentials,
network model, live tools or sends. Requires existing Hermes Python environment.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import zipfile
ROOT=Path(__file__).resolve().parents[1]
HOST=next((Path(p).parents[2]/'bin/python' for p in sys.path if p.endswith('site-packages') and (Path(p).parents[2]/'pyvenv.cfg').is_file()),Path(sys.executable))
sys.path.insert(0,str(ROOT))
from continuity.store import Store
from continuity.intake import OwnerContext,admit_event
from continuity.extraction import Worker
from continuity.outbox import Outbox

def corpus():
    results=[]
    with tempfile.TemporaryDirectory() as d:
        s=Store(Path(d)/'data'); s.configure('identities',{'terminal:owner':'person','desktop:owner':'person'})
        fixtures=json.loads((ROOT/'tests/fixtures/scenarios.json').read_text())
        for case in fixtures:
            event=admit_event(s,OwnerContext.local('terminal','owner'),{'id':case['name'],'text':case['text']}); s.ingest(event)
            proposal={k:case[k] for k in ('kind','owner')}; proposal.update(source=case['name'],quote=case['text'],title=case['name'])
            class Model:
                def complete_structured(me,**kw): return SimpleNamespace(parsed={'proposals':[proposal]})
            assert Worker(s,Model()).tick()['processed']==1
            if case['kind']=='pending_close':
                assert not any(i['source']==case['name'] for i in s.items())
                evidence='untargeted closure retained as proposal only; no extra obligation'
            else:
                item=next(i for i in s.items() if i['source']==case['name']); assert item['state']==case['expected']
                evidence='fixture proposal validated; item remains open'
            results.append({'scenario':case['name'],'passed':True,'evidence':evidence})
        item=next(i for i in s.items() if i['source']=='reembolso_pending')
        # Explicit authenticated owner control at another mapped surface.
        close_event=admit_event(s,OwnerContext.local('desktop','owner'),{'id':'desktop:closure','text':'The refund arrived; close this item.'})
        s.ingest(close_event)
        class CloseModel:
            def complete_structured(me,**kw):
                return SimpleNamespace(parsed={'proposals':[dict(kind='pending_close',owner='human',source=close_event['id'],quote=close_event['text'],title='Refund closure',target=item['id'],target_revision=item['revision'],closure='explicit',assertion=dict(quote=close_event['text'],polarity='affirmed',certainty='certain',speaker='self',temporality='current',meaning='completion'))]})
        assert Worker(s,CloseModel()).tick()['processed']==1
        pending=next(i for i in s.items() if i['id']==item['id'])
        assert pending['state']=='waiting'  # multiple live commitments need exact control
        s.transition(pending['id'],pending['revision'],'done','authenticated local owner control')
        assert next(i for i in s.items() if i['id']==item['id'])['state']=='done'
        results.append({'scenario':'cross_surface_owner_closure','passed':True,'evidence':'ambiguous conversation proposes; authenticated exact-item control closes by revision CAS'})
        s.pause(True); assert s.claim('paused',0) is None
        results.append({'scenario':'pause','passed':True})
        s.pause(False); o=Outbox(s); id=o.prepare(next(i for i in s.items() if i['state']=='open'))
        with s.tx() as c: c.execute("update outbox set state='sending' where id=?",(id,))
        o.recover(); assert o.get(id)['state']=='unknown'; assert not o.dispatch(id,lambda _:(_ for _ in ()).throw(AssertionError('resent')),0)
        results.append({'scenario':'unknown_send_restart','passed':True})
    return results

def run(archive):
    report={'synthetic_only':True,'semantic_quality':'unverified','live_delivery':'unverified','cases':corpus(),'commands':[]}
    host_spec=importlib.util.find_spec('hermes_cli')
    if host_spec is None: raise RuntimeError('Run with the existing Hermes environment')
    host_root=str(Path(next(iter(host_spec.submodule_search_locations))).parent)
    with tempfile.TemporaryDirectory() as d:
        scratch=Path(d); extracted=scratch/'extracted'; extracted.mkdir()
        with zipfile.ZipFile(archive) as z:
            for entry in z.infolist():
                p=Path(entry.filename)
                if p.is_absolute() or '..' in p.parts: raise ValueError('unsafe archive')
            z.extractall(extracted)
        package=next(extracted.iterdir()); manifest=json.loads((package/'CONTENTS.sha256.json').read_text())
        for name,digest in manifest.items(): assert hashlib.sha256((package/name).read_bytes()).hexdigest()==digest,name
        home=scratch/'profile'; plugins=home/'plugins'; plugins.mkdir(parents=True)
        import shutil
        shutil.copytree(package,plugins/'hermes-continuity')
        (home/'config.yaml').write_text('plugins:\n  enabled: [hermes-continuity]\n')
        env=dict({k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL','SYSTEMROOT','WINDIR','LD_LIBRARY_PATH','SSL_CERT_FILE')},HOME=str(scratch/'home'),HERMES_HOME=str(home),TMPDIR=str(scratch),PYTHONPATH=os.pathsep.join([host_root,*[p for p in sys.path if p.endswith('site-packages')]]),HERMES_DISABLE_LAZY_INSTALLS='1',PYTHONDONTWRITEBYTECODE='1',HERMES_ENABLE_PROJECT_PLUGINS='0')
        (scratch/'home').mkdir()
        commands=[
            [str(HOST),'-m','unittest','discover','-s',str(package/'tests'),'-t',str(package),'-v'],
            [str(HOST),'-m','hermes_cli.main','plugins','doctor',str(package),'--ci'],
            [str(HOST),'-m','hermes_cli.main','plugins','validate',str(package)],
            [str(HOST),'-m','hermes_cli.main','continuity','status'],
            [str(HOST),'-m','hermes_cli.main','continuity','setup','--identity','terminal:owner=person','--allow-model','--timezone','UTC','--ticker'],
            [str(HOST),'-m','hermes_cli.main','continuity','setup','--ticker'],
            [str(HOST),'-m','hermes_cli.main','continuity','tick'],
            [str(HOST),'-m','hermes_cli.main','continuity','remove'],
        ]
        for command in commands:
            result=subprocess.run(command,cwd=package,env=env,text=True,capture_output=True,timeout=90)
            record={'command':[str(x).replace(str(scratch),'$SANDBOX') for x in command],'returncode':result.returncode,'stdout':result.stdout.replace(str(scratch),'$SANDBOX'),'stderr':result.stderr.replace(str(scratch),'$SANDBOX')}; report['commands'].append(record)
            if result.returncode:
                report['passed']=False; return report
        assert not list((home/'scripts').glob('continuity-*.py'))
        report['passed']=True
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--archive',type=Path,default=ROOT/'dist/hermes-continuity-0.2.0.zip'); parser.add_argument('--report',type=Path,default=ROOT/'evidence/acceptance.json'); args=parser.parse_args()
    report=run(args.archive); args.report.parent.mkdir(parents=True,exist_ok=True); args.report.write_text(json.dumps(report,indent=2)+'\n'); print(json.dumps({'passed':report['passed'],'scenarios':len(report['cases']),'commands':len(report['commands']),'report':str(args.report)}));
    if not report['passed']: sys.exit(1)
