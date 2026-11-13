#!/usr/bin/env python3
import argparse
import ast
import collections
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import signal
import shutil
import subprocess
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'notifications'))
from hi_format import format_reply

ROOT = Path(__file__).resolve().parents[3]
SETTINGS = json.loads((ROOT / '.codex/workspace.json').read_text())
TASK = ROOT / '.codex/tasks/organize'
TZ = ZoneInfo(SETTINGS['timezone'])
PROTECTED = {'AGENTS.md', 'CLAUDE.md', 'README.md', 'LICENSE', 'package.json', 'package-lock.json', 'pyproject.toml', 'requirements.txt'}
RESERVED = {'SOP', '通知配置', '成员工作区', '错题本'}
# Exact project-relative files allowed only in read-only dependency checks.
READ_ONLY_DEPENDENCIES = frozenset({'.agents/skills/eval-data-funnel/scripts/analyze.py'})
CODEX = SETTINGS['binaries']['codex']


def save(path, data):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


def fingerprint(path):
    st = path.stat()
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return {'size': st.st_size, 'mtime_ns': st.st_mtime_ns, 'sha256': h.hexdigest()}


def notify(message):
    if not SETTINGS.get('notifications_enabled', False): return
    config = json.loads((ROOT / '通知配置/webhooks.json').read_text())
    url = config['hi']['codex_scheduled_task_complete']['url']
    for part in format_reply(message, status='error' if '文件整理 · 异常' in message else 'done'):
        request = urllib.request.Request(url, data=json.dumps({'msgtype': 'markdown', 'markdown': {'content': part}}).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=25) as response:
            result = json.load(response)
        if result.get('errcode') != 0:
            raise RuntimeError('hi 通知失败，错误码 ' + str(result.get('errcode')))


def validate(plan, candidates):
    seen, targets = set(), set()
    for item in plan['moves'] + plan['skipped'] + plan.get('duplicates', []):
        src = item['source']
        if src not in candidates or src in seen:
            raise ValueError('候选来源重复或超出范围')
        seen.add(src)
    if seen != set(candidates):
        raise ValueError('计划遗漏候选文件')
    for item in plan['moves']:
        relative = PurePosixPath(item['destination'])
        if relative.is_absolute() or len(relative.parts) < 2 or any(p in ('.', '..') or p.startswith('.') for p in relative.parts) or relative.parts[0] in RESERVED:
            raise ValueError('目标目录超出业务范围')
        dest = ROOT / relative
        if not dest.resolve().is_relative_to(ROOT) or any(p.is_symlink() for p in [dest, *dest.parents] if p != ROOT):
            raise ValueError('目标不能经过符号链接')
        if dest.exists() or str(dest) in targets:
            raise ValueError('目标重名，不覆盖')
        if Path(item['source']).suffix and Path(item['source']).suffix.lower() != dest.suffix.lower():
            raise ValueError('不能修改现有扩展名')
        targets.add(str(dest))
    for item in plan.get('duplicates', []):
        kept = safe_file(item['keep'])
        if len(Path(item['keep']).parts) < 2 or Path(item['keep']).parts[0] in RESERVED:
            raise ValueError('保留副本必须在业务目录中')
        src = ROOT / item['source']
        if fingerprint(src)['sha256'] != fingerprint(kept)['sha256']:
            raise ValueError('去重必须字节完全一致')
    edited = set()
    for item in plan.get('edits', []):
        path = safe_file(item['path'])
        if item['path'] in edited or path.name in PROTECTED:
            raise ValueError('重复修改或受保护文件')
        if path.suffix.lower() not in {'.py', '.js', '.ts', '.html', '.css', '.json', '.md', '.sh', '.txt'}:
            raise ValueError('仅允许路径相关文本修改')
        if any(item['path'] == d['keep'] or item['path'] == d['source'] for d in plan.get('duplicates', [])):
            raise ValueError('去重文件不能同时修改')
        if fingerprint(path)['sha256'] != item['sha256']:
            raise ValueError('待修改文件已变化：' + item['path'])
        edited.add(item['path'])
        revised_text(path, item)
    destinations = {x['destination'] for x in plan['moves']}
    removed = {x['source'] for x in plan['moves'] + plan.get('duplicates', [])}
    for relative in plan.get('checks', []):
        safe_dependency(relative, must_exist=False)
        if relative in removed or (relative not in destinations and not (ROOT / relative).is_file()):
            raise ValueError('整理后依赖不存在：' + relative)


def safe_file(relative, must_exist=True):
    parts = PurePosixPath(relative).parts
    path = ROOT / relative
    if not parts or PurePosixPath(relative).is_absolute() or any(p in {'.', '..'} or p.startswith('.') for p in parts) or parts[0] == '通知配置':
        raise ValueError('路径超出整理范围')
    if not path.resolve().is_relative_to(ROOT) or any(p.is_symlink() for p in [path, *path.parents] if p != ROOT):
        raise ValueError('路径不能经过符号链接')
    if must_exist and not path.is_file():
        raise ValueError('文件不存在：' + relative)
    return path


def safe_dependency(relative, must_exist=True):
    """Check dependencies without extending safe_file's write permissions."""
    if relative not in READ_ONLY_DEPENDENCIES:
        return safe_file(relative, must_exist=must_exist)
    path = ROOT / relative
    if not path.resolve().is_relative_to(ROOT) or any(p.is_symlink() for p in [path, *path.parents] if p != ROOT):
        raise ValueError('路径不能经过符号链接')
    # Tool dependencies must already exist, including during preflight checks.
    if not path.is_file():
        raise ValueError('文件不存在：' + relative)
    return path


def revised_text(path, item):
    text = path.read_bytes().decode('utf-8')
    for replacement in item['replacements']:
        old, new, count = replacement['old'], replacement['new'], replacement['count']
        if not old or count < 1 or text.count(old) != count:
            raise ValueError('路径替换匹配次数不符：' + item['path'])
        text = text.replace(old, new)
    if path.suffix == '.py':
        ast.parse(text, filename=str(path))
    if path.suffix == '.json':
        json.loads(text)
    return text


def apply_plan(plan, candidates, run, report):
    """Stage destinations, fix references, verify, then remove old names.

    pending_batch + backups allow recovery after interruption; never replay blindly.
    """
    validate(plan, candidates)
    for item in plan['moves'] + plan.get('duplicates', []):
        source = safe_file(item['source'])
        if fingerprint(source) != candidates[item['source']]:
            raise RuntimeError('分析期间文件已变化：' + item['source'])
    edits = []
    move_map = {x['source']: x['destination'] for x in plan['moves']}
    for i, item in enumerate(plan.get('edits', [])):
        source = ROOT / item['path']
        backup = run / 'backups' / str(i)
        backup.parent.mkdir(exist_ok=True)
        shutil.copy2(source, backup)
        edits.append((item, ROOT / move_map.get(item['path'], item['path']), revised_text(source, item), backup))
    report['pending_batch'] = {'plan': plan, 'backups': [
        {'path': e[0]['path'], 'backup': str(e[3]), 'final': str(e[1])} for e in edits]}
    save(run / 'report.json', report)
    # All old names remain until the new locations and edits have passed checks.
    for item in plan['moves']:
        source, dest = ROOT / item['source'], ROOT / item['destination']
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, dest)
        if fingerprint(dest) != candidates[item['source']]:
            raise RuntimeError('执行期间文件已变化：' + item['source'])
    for item, dest, content, backup in edits:
        if fingerprint(dest)['sha256'] != item['sha256']:
            raise RuntimeError('执行期间脚本已变化：' + item['path'])
        temp = dest.with_name(dest.name + '.organize-tmp')
        with temp.open('x', encoding='utf-8', newline='') as stream:
            stream.write(content)
        shutil.copymode(dest, temp)
        temp.replace(dest)
        report.setdefault('edited', []).append({'path': str(dest.relative_to(ROOT)), 'reason': item['reason'], 'sha256': fingerprint(dest)['sha256']})
        save(run / 'report.json', report)
    for relative in plan.get('checks', []):
        safe_dependency(relative)
    for item in plan['moves']:
        source, dest = ROOT / item['source'], ROOT / item['destination']
        if fingerprint(source) != candidates[item['source']]:
            raise RuntimeError('删除旧路径前发现源文件变化：' + item['source'])
        # Edited scripts intentionally have different destination bytes.
        expected = next((e['sha256'] for e in report.get('edited', []) if e['path'] == item['destination']), candidates[item['source']]['sha256'])
        if fingerprint(dest)['sha256'] != expected:
            raise RuntimeError('目标文件校验失败：' + item['destination'])
        source.unlink()
        report['moved'].append({**item, 'sha256': fingerprint(dest)['sha256']})
        save(run / 'report.json', report)
    for item in plan.get('duplicates', []):
        source, kept = safe_file(item['source']), safe_file(item['keep'])
        if fingerprint(source) != candidates[item['source']] or fingerprint(source)['sha256'] != fingerprint(kept)['sha256']:
            raise RuntimeError('去重前文件已变化：' + item['source'])
        source.unlink()
        report.setdefault('deduplicated', []).append({**item, 'sha256': fingerprint(kept)['sha256']})
        save(run / 'report.json', report)
    report.pop('pending_batch', None)
    save(run / 'report.json', report)


def notification(report, now):
    counts = collections.Counter(PurePosixPath(x['destination']).parts[0] for x in report['moved'])
    message = f'**文件整理 · {now:%m/%d}**\n\n已归档 {len(report["moved"])} 个文件，去重 {len(report.get("deduplicated", []))} 个，修正引用 {len(report.get("edited", []))} 处文件。'
    if counts:
        message += '\n\n' + '\n'.join(f'• {k}：{v} 个' for k, v in counts.most_common())
    if report['skipped']:
        message += f'\n\n**待确认 / 暂缓：{len(report["skipped"])} 个**\n' + '\n'.join(f'• {x["source"]}：{x["reason"]}' for x in report['skipped'])
    elif not counts and not report.get('deduplicated'):
        message += '\n根目录无待整理散文件。'
    return message


def plan_files(candidates, run):
    if not candidates:
        return {'moves': [], 'skipped': []}
    prompt = (TASK / 'prompt.md').read_text() + '\n候选文件及校验信息：\n' + json.dumps(candidates, ensure_ascii=False)
    command = [CODEX, '--no-daemon', '-a', 'never', 'exec', '--sandbox', 'read-only', '--skip-git-repo-check', '-C', str(ROOT), '--output-schema', str(TASK / 'schema.json'), '-o', str(run / 'plan.json'), '-']
    with (run / 'codex.log').open('w') as log:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=log, text=True, start_new_session=True)
        try:
            process.communicate(prompt, timeout=2700)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise RuntimeError('内容分析超过 45 分钟')
        if process.returncode:
            raise RuntimeError('Codex 分析失败，详见本次 codex.log')
    return json.loads((run / 'plan.json').read_text())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--scheduled', action='store_true')
    args = ap.parse_args()
    now = dt.datetime.now(TZ)
    state_path = TASK / 'state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if args.scheduled and (now.weekday() >= 5 or now.hour < 20 or state.get('completed_date') == now.date().isoformat()):
        return
    with (TASK / 'run.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        run = TASK / 'logs' / now.strftime('%Y%m%d-%H%M%S-%f')
        run.mkdir(parents=True)
        report = {'started': now.isoformat(), 'dry_run': args.dry_run, 'moved': [], 'skipped': []}
        try:
            for previous in (TASK / 'logs').glob('*/report.json'):
                old_report = json.loads(previous.read_text())
                if old_report.get('pending_batch') or old_report.get('pending_move'):
                    raise RuntimeError('存在未完成整理，先核对恢复：' + str(previous))
            candidates = {}
            for path in sorted(ROOT.iterdir()):
                if path.name.startswith('.') or path.name in PROTECTED or path.is_symlink() or not path.is_file():
                    continue
                if time.time() - path.stat().st_mtime < 600 or path.suffix.lower() in {'.crdownload', '.download', '.part', '.tmp'}:
                    report['skipped'].append({'source': path.name, 'reason': '正在下载或最近 10 分钟仍有修改'})
                    continue
                candidates[path.name] = fingerprint(path)
            save(run / 'inventory.json', candidates)
            plan = plan_files(candidates, run)
            validate(plan, candidates)
            save(run / 'plan.json', plan)
            report['skipped'].extend(plan['skipped'])
            if args.dry_run:
                report['status'] = 'dry_run_ok'
                save(run / 'report.json', report)
                print(str(run))
                return
            apply_plan(plan, candidates, run, report)
            message = notification(report, now)
            report['status'] = 'organized'
            report['notification'] = message
            save(run / 'report.json', report)
            notify(message)
            report['status'] = 'complete'
            save(state_path, {'completed_date': now.date().isoformat(), 'last_run': str(run)})
        except Exception as exc:
            report['status'] = 'failed'
            report['error'] = str(exc)
            if not args.dry_run:
                try:
                    notify(f'**文件整理 · 异常**\n\n已整理 {len(report["moved"])} 个文件，任务未正常完成。\n\n请查看本机任务日志。')
                except Exception as notification_error:
                    report['notification_error'] = str(notification_error)
            raise
        finally:
            save(run / 'report.json', report)
        print(str(run))

if __name__ == '__main__':
    main()
