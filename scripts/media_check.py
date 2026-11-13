#!/usr/bin/env python3
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
p=argparse.ArgumentParser();p.add_argument('--root',required=True,type=Path);a=p.parse_args()
repo=Path(__file__).resolve().parents[1];base=a.root/'.codex/hi-bridge/media-runtime'
manifest=json.loads((repo/'config/media-model.json').read_text());checks=[]
for name,expected in manifest['files'].items():
    file=base/'model'/name
    ok=file.is_file() and not file.is_symlink() and file.stat().st_size==expected['bytes']
    if ok:
        h=hashlib.sha256()
        with file.open('rb') as f:
            for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
        ok=h.hexdigest()==expected['sha256']
    checks.append({'file':name,'passed':ok})
try:r=subprocess.run([str(base/'venv/bin/python'),'-c','import av,numpy,faster_whisper,PIL'],capture_output=True,timeout=30);ok=r.returncode==0
except (OSError,subprocess.TimeoutExpired):ok=False
checks.append({'dependencies':True,'passed':ok})
print(json.dumps({'passed':all(c['passed'] for c in checks),'checks':checks,'live_media_acceptance':'pending'},indent=2))
sys.exit(0 if all(c['passed'] for c in checks) else 1)
