#!/usr/bin/env python3
"""Check public-source hygiene, Python syntax, JSON and repository links.

This conservative allowlist scan supplements review; it is not a secret detector guarantee.
"""
import ast
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote
ROOT=Path(__file__).resolve().parents[1]
SKIP={'.git','.venv','node_modules','__pycache__','reports','dist'}

def check():
    errors=[];count=0
    try:
        tracked=subprocess.run(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=ROOT,capture_output=True,text=True,check=True).stdout.split('\0')
        paths=[ROOT/name for name in tracked if name]
    except subprocess.CalledProcessError:
        paths=[p for p in ROOT.rglob('*') if not (set(p.relative_to(ROOT).parts)&SKIP)]
    for p in paths:
        if not p.is_file() or p.is_symlink():continue
        rel=p.relative_to(ROOT);count+=1
        if p.suffix in ('.sqlite','.sqlite3','.log') or p.name in ('credentials.json','state.json','baseline.json','config.toml','workspace.local.json'):
            errors.append(f'{rel}: private runtime artifact');continue
        s=p.read_text()
        if p.suffix in ('.js','.cjs','.mjs') and re.search(r'''\brequire\(\s*["']/[^"']*/node_modules/''',s):
            errors.append(f'{rel}: absolute external package import')
        patterns=[r'/Users/(?!a/)[A-Za-z][^/\s]+/',r'gh[pousr]_[A-Za-z0-9]{25,}',r'github_pat_[A-Za-z0-9_]{30,}',r'AKID[A-Za-z0-9]{24,}',r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',r'https?://[^\s"<>]+webhook[^\s"<>]+[?&]key=[A-Za-z0-9-]{16,}']
        if p.name!='release_check.py':
            for pattern in patterns:
                if re.search(pattern,s):errors.append(f'{rel}: potentially private content (pattern omitted)')
        if p.suffix=='.py':
            try:ast.parse(s)
            except SyntaxError as e:errors.append(f'{rel}: {e}')
        if p.suffix=='.json':
            try:json.loads(s)
            except ValueError as e:errors.append(f'{rel}: {e}')
        if p.suffix=='.md':
            # Inline Markdown links only; exclude examples inside fenced code.
            text=re.sub(r'```[\s\S]*?```','',s)
            for target in re.findall(r'(?<!!)\[[^\]]*\]\(([^\s)]+)\)',text):
                if target.startswith(('https:','http:','#','mailto:')):continue
                path=unquote(target.split('#')[0])
                if '<' in path or not path:continue
                # Generated template links resolve in an installed workspace.
                if rel.parts[0]=='templates':
                    if path.startswith('../.codex/'):candidate=ROOT/'runtime'/path[len('../.codex/'):]
                    elif p.name=='AGENTS.md':candidate=p.parent/path
                    else:candidate=p.parent/path
                elif rel.parts[:2]==('runtime','gravekeeper'):
                    continue # runtime guardian operational paths are validated after install
                else:candidate=p.parent/path
                if not candidate.exists():errors.append(f'{rel}: broken local link {target}')
    print(json.dumps({'passed':not errors,'files_checked':count,'errors':errors,'scope':'static review aid; external acceptance is separate'},ensure_ascii=False,indent=2))
    return not errors
if __name__=='__main__':sys.exit(0 if check() else 1)
