#!/usr/bin/env python3
"""Build deterministic native directory-plugin archives without git or network."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile
import io
import zipfile

ROOT=Path(__file__).resolve().parents[1]
NAME='hermes-continuity-0.2.0'

def source_files(root):
    exact=['__init__.py','plugin.yaml','README.md','SECURITY.md','LICENSE','pyproject.toml']
    files=[root/p for p in exact]
    for directory in ('continuity','scripts','tests'):
        files.extend(p for p in (root/directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix in ('.py','.json'))
    files.extend(root/p for p in ('docs/ACCEPTANCE.md','docs/INTEGRATIONS.md','docs/REVIEW-MATRIX.md','docs/CHECKINS.md',
                                  'docs/GENERAL-FOLLOWTHROUGH.md','docs/CONFIGURATION.md','docs/DEVELOPMENT.md','docs/examples/wellbeing-quality.json'))
    if any(p.is_symlink() for p in files): raise ValueError('release symlink')
    return sorted(set(files))

def build(root=ROOT,out=None):
    out=Path(out or root/'dist'); out.mkdir(parents=True,exist_ok=True)
    files=source_files(root); manifest={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    payload=dict(manifest); entries={str(p.relative_to(root)):p.read_bytes() for p in files}
    entries['CONTENTS.sha256.json']=(json.dumps(manifest,sort_keys=True,indent=2)+'\n').encode()
    zp=out/(NAME+'.zip'); tp=out/(NAME+'.tar.gz')
    with zipfile.ZipFile(zp,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for name,data in sorted(entries.items()):
            info=zipfile.ZipInfo(NAME+'/'+name,date_time=(2026,1,1,0,0,0)); info.external_attr=0o100644<<16; info.compress_type=zipfile.ZIP_DEFLATED; z.writestr(info,data)
    import gzip
    with tp.open('wb') as raw,gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0) as gz,tarfile.open(fileobj=gz,mode='w') as t:
        for name,data in sorted(entries.items()):
            info=tarfile.TarInfo(NAME+'/'+name); info.size=len(data); info.mode=0o644; info.mtime=1767225600; t.addfile(info,io.BytesIO(data))
    checks={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (zp,tp)}
    (out/'SHA256SUMS').write_text(''.join(v+'  '+k+'\n' for k,v in checks.items()))
    return {'archives':checks,'files':len(entries)}

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--out',type=Path); args=parser.parse_args(); print(json.dumps(build(out=args.out),indent=2))
