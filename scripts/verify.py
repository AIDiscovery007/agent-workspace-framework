#!/usr/bin/env python3
"""Offline regression in a disposable deployment. Never touches the live workspace."""
import argparse
import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import aw

def verify(node=False):
    if importlib.util.find_spec('PIL') is None:
        raise SystemExit('Missing Pillow. Activate the project venv and run: python -m pip install -r requirements.lock')
    reports=[]
    with tempfile.TemporaryDirectory(prefix='agent-workspace-verify-') as tmp:
        root=Path(tmp).resolve()/'workspace';root.mkdir()
        cfg=aw.read(aw.REPO/'config/workspace.example.json')
        cfg['model']='test-model';cfg['binaries']={k:k for k in ('hi','codex','node','hc','herdr-app')}
        aw.materialize(root,cfg,'core',fixture=True)
        home=Path(tmp)/'home';home.mkdir()
        env={**os.environ,'HOME':str(home),'CODEX_HOME':str(home/'.codex'),'PYTHONDONTWRITEBYTECODE':'1'}
        env.pop('HI_CODEX_CONFIG',None)
        for folder in ('notifications','hi-bridge','gravekeeper','tasks/organize','tasks/daily-log','tasks/weekly-report'):
            started=time.monotonic()
            result=subprocess.run([sys.executable,'-m','unittest','discover','-s',str(root/'.codex'/folder),'-p','test_*.py'],env=env,capture_output=True,text=True,timeout=180)
            match=re.search(r'Ran (\d+) tests?',result.stderr)
            reports.append({'suite':folder,'tests':int(match.group(1)) if match else 0,'passed':result.returncode==0,'seconds':round(time.monotonic()-started,2)})
            print(folder+': '+('PASS' if result.returncode==0 else 'FAIL'),flush=True)
            print(result.stderr.strip(),flush=True)
        result=subprocess.run([sys.executable,'-m','unittest','discover','-s',str(aw.REPO/'tests'),'-p','test_*.py'],env=env,capture_output=True,text=True,timeout=180)
        match=re.search(r'Ran (\d+) tests?',result.stderr)
        reports.append({'suite':'installer-and-contracts','tests':int(match.group(1)) if match else 0,'passed':result.returncode==0});print(result.stdout+result.stderr,flush=True)
        if node:
            base=root/'.codex/hi-bridge'
            result=subprocess.run(['npm','ci','--ignore-scripts','--no-fund','--registry=https://registry.npmjs.org'],cwd=base,capture_output=True,text=True,timeout=180)
            if result.returncode:raise RuntimeError('npm ci failed; inspect registry access')
            result=subprocess.run(['node',str(base/'test_ws_transport.cjs')],cwd=base,capture_output=True,text=True,timeout=30)
            reports.append({'suite':'websocket','passed':result.returncode==0});print(result.stdout+result.stderr,flush=True)
    return {'passed':all(r['passed'] for r in reports),'suites':reports,'external_business_services_called':False,'scope':'isolated unit/integration fixtures; no live Hi, model or Herdr end-to-end acceptance'}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--node',action='store_true',help='also install locked ws dependency and run transport tests');p.add_argument('--report',type=Path);args=p.parse_args()
    report=verify(args.node)
    if args.report:args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(aw.dump(report))
    print(aw.dump(report));sys.exit(0 if report['passed'] else 1)
