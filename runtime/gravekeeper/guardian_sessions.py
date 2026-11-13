"""Durable guardian cycles: fresh Herdr session, one review, verified archive."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime
from notebook import atomic, dump

SETTINGS = json.loads((Path(__file__).resolve().parent/'config.json').read_text())
class Binaries:
    def __truediv__(self, name):
        return Path(SETTINGS['binaries'][name])
BIN = Binaries()
CODEX_DIR = Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex')))
EFFORT = 'xhigh'


def command(argv, timeout=45, missing_ok=False):
    result = subprocess.run([str(x) for x in argv], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        if missing_ok and result.returncode == 3:
            return None
        raise RuntimeError(f'{Path(argv[0]).name} failed ({result.returncode}): {result.stderr[-500:]}')
    return result.stdout


def call(argv, **kwargs):
    text = command(argv, **kwargs)
    return json.loads(text) if text else None


def session_info(ident):
    ident = str(uuid.UUID(ident))
    for database in sorted(CODEX_DIR.glob('state_*.sqlite'), reverse=True):
        with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT id,cwd,rollout_path,archived,model,reasoning_effort,sandbox_policy,approval_mode FROM threads WHERE id=?', (ident,)).fetchone()
            if row:
                return dict(row)
    raise RuntimeError('未找到本轮 Codex session 的准确元数据')


def transcript_complete(info, since):
    path = Path(info['rollout_path'])
    if not path.is_file():
        return False
    complete = False
    with path.open() as stream:
        for line in stream:
            try:
                event = json.loads(line)
                payload = event.get('payload', {})
                if event.get('type') == 'event_msg' and payload.get('type') == 'task_complete':
                    at = datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')).timestamp()
                    complete = at >= since
            except (ValueError, KeyError):
                continue
    return complete


def cycle_path(n, ident):
    if not ident.startswith('G-') or not ident[2:].isalnum():
        raise ValueError('非法守墓人轮次 ID')
    return n.base / 'sessions' / ident / 'cycle.json'


def save(n, cycle):
    atomic(cycle_path(n, cycle['id']), dump(cycle))


def active(n):
    with n.db() as db:
        ident = n.meta(db, 'guardian_cycle')
    return json.loads(cycle_path(n, ident).read_text()) if ident else None


def new_cycle(n, cfg, legacy=None):
    previous = active(n)
    if previous:
        return previous
    if legacy:
        run = legacy['run']
    else:
        run = n.begin()  # An interrupted review keeps its original snapshot.
    ident = 'G-' + uuid.uuid4().hex[:12]
    cycle = {'id': ident, 'name': '守墓人 · ' + ident, 'root': str(n.root), 'space': cfg['space'],
             'created': time.time(), 'run_id': run['id'], 'effort': EFFORT, 'status': 'starting'}
    if legacy:
        cycle.update(legacy=True, terminal=legacy['terminal'], session_id=legacy['session_id'],
                     name=legacy['name'], status='running', created=0)
    save(n, cycle)
    with n.db() as db:
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('guardian_cycle', ident))
        if not legacy:
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('last_dispatch', str(time.time())))
        db.execute('INSERT INTO audit(at,action,detail) VALUES (?,?,?)', (time.time(), 'guardian-cycle', dump(cycle)))
    return cycle


def launch_cycle(n, cycle, launch, prompt):
    terminal, created = launch(n, cycle['name'], prompt, effort=EFFORT)
    cycle['terminal'] = terminal
    cycle['visible_verified'] = True  # launch verifies both hc get and Herdr layout.
    save(n, cycle)
    with n.db() as db:
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('guardian_terminal', terminal))
    return {'launched': terminal, 'cycle': cycle['id'], 'created': created}


def register(n, ident):
    cycle = active(n)
    if not cycle or cycle['id'] != ident or cycle['status'] not in ('starting', 'running'):
        raise ValueError('当前轮次不可接收')
    session = str(uuid.UUID(os.environ.get('CODEX_THREAD_ID', '')))
    terminal = os.environ.get('HC_TERMINAL_ID')
    if not terminal or terminal != cycle.get('terminal'):
        raise ValueError('守墓人接收终端与本轮不匹配')
    if cycle.get('session_id') and cycle['session_id'] != session:
        raise ValueError('本轮已由另一 session 接收')
    info = session_info(session)
    if info['cwd'] != str(n.root) or info['archived'] or info['reasoning_effort'] != EFFORT:
        raise ValueError('守墓人 session 目录、归档状态或实际 effort 不符')
    if info['approval_mode'] != 'never' or json.loads(info['sandbox_policy']).get('type') != 'disabled':
        raise ValueError('守墓人 session 未采用已授权的免审批 full access 配置')
    cycle.update(session_id=session, status='running', registered_at=time.time(),
                 execution={'model': info['model'], 'effort': info['reasoning_effort'],
                            'approval_policy': info['approval_mode'], 'sandbox': json.loads(info['sandbox_policy'])})
    save(n, cycle)
    with n.db() as db:
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('guardian_session', session))
    return {'accepted': True, 'cycle': ident, 'run_id': cycle['run_id'], 'session_id': session}


def adopt_legacy(n, cfg):
    """The user's migration includes the previous persistent guardian only."""
    with n.db() as db:
        terminal = n.meta(db, 'guardian_terminal')
        session = n.meta(db, 'guardian_session')
        row = db.execute('SELECT * FROM runs ORDER BY created DESC LIMIT 1').fetchone()
    if not terminal:
        return None
    if not session or not row:
        raise RuntimeError('旧守墓人缺少 session 或巡检记录，停止迁移')
    state = call([BIN / 'hc', 'get', terminal])
    if state.get('cwd') != str(n.root) or state.get('labels', {}).get('herdr.space') != cfg['space']:
        raise RuntimeError('旧守墓人终端归属不匹配')
    info = session_info(session)
    if info['cwd'] != str(n.root):
        raise RuntimeError('旧守墓人 session 目录不匹配')
    # Migration must match the known resume UUID, not just the display name.
    argv = state.get('foreground', {}).get('argv', [])
    if state.get('state') == 'live' and session not in argv:
        raise RuntimeError('旧守墓人前台不是账本中绑定的 session')
    layout = call([BIN / 'herdr-app', 'ls'])
    matches = [tab for space in layout['spaces'] for tab in space['tabs']
               if space['id'] == cfg['space'] and any(t['terminal'] == terminal for t in tab['terminals'])]
    if len(matches) != 1:
        raise RuntimeError('旧守墓人可见标签页不唯一')
    return new_cycle(n, cfg, {'terminal': terminal, 'session_id': session, 'run': dict(row), 'name': matches[0]['name']})


def owned_state(n, cycle):
    state = call([BIN / 'hc', 'get', cycle['terminal']], missing_ok=True)
    if state and (state.get('cwd') != str(n.root) or
                  state.get('labels', {}).get('herdr.space') != cycle['space']):
        raise RuntimeError('守墓人终端归属发生变化，停止归档')
    if state and state.get('state') == 'live':
        argv = state.get('foreground', {}).get('argv', [])
        if cycle.get('legacy'):
            matches = cycle['session_id'] in argv
        else:
            matches = any(cycle['id'] in str(arg) for arg in argv)
        if not matches:
            raise RuntimeError('守墓人前台已切换，停止归档')
    return state


def archive_cycle(n, cycle):
    """Retries archive/cleanup only; never rerun the review or delete transcripts."""
    with n.db() as db:
        pending = n.open_jobs(db)
    if pending:
        return {'waiting_for_jobs': pending, 'cycle': cycle['id']}
    state = owned_state(n, cycle)
    if state and state.get('state') == 'live':
        if (state.get('status') or {}).get('state') not in ('idle', 'done'):
            return {'busy_or_blocked': state.get('status'), 'terminal': cycle['terminal']}
    info = session_info(cycle['session_id'])
    if info['cwd'] != str(n.root):
        raise RuntimeError('守墓人 session 目录不符，停止归档')
    if state and state.get('state') == 'live':
        call([BIN / 'hc', 'kill', cycle['terminal']])
        call([BIN / 'hc', 'wait', cycle['terminal'], '--timeout', '15'], timeout=25)
    if not info['archived']:
        command([BIN / 'codex', 'archive', cycle['session_id']], timeout=90)
    info = session_info(cycle['session_id'])
    if not info['archived'] or not Path(info['rollout_path']).is_file():
        raise RuntimeError('session 归档或历史保留回读失败')
    cycle.update(session_archived=True, archived_rollout=info['rollout_path'])
    save(n, cycle)
    layout = call([BIN / 'herdr-app', 'ls'])
    for space in layout['spaces']:
        for tab in space['tabs']:
            if any(t['terminal'] == cycle['terminal'] for t in tab['terminals']):
                if space['id'] != cycle['space'] or tab.get('name') != cycle['name'] or len(tab['terminals']) != 1:
                    raise RuntimeError('守墓人标签页归属变化，保留待核对')
                call([BIN / 'herdr-app', 'do', 'tab.close', dump({'id': tab['id'], 'confirmed': True})])
    call([BIN / 'hc', 'rm', cycle['terminal']], missing_ok=True)
    if call([BIN / 'hc', 'get', cycle['terminal']], missing_ok=True) is not None:
        raise RuntimeError('守墓人终端移除后仍存在')
    cycle.update(status='archived', archived_at=time.time(), terminal_removed=True)
    save(n, cycle)
    with n.db() as db:
        # Clear only our active pointer; ledger revision and unfinished jobs survive.
        if n.meta(db, 'guardian_cycle') == cycle['id']:
            db.execute("DELETE FROM meta WHERE key IN ('guardian_cycle','guardian_terminal','guardian_session')")
        db.execute('INSERT INTO audit(at,action,detail) VALUES (?,?,?)', (time.time(), 'guardian-archive', dump(cycle)))
    return {'archived': cycle['session_id'], 'cycle': cycle['id'], 'outcome': cycle.get('outcome')}


def advance(n, cycle):
    if cycle['status'] in ('archiving', 'archived'):
        return archive_cycle(n, cycle)
    if not cycle.get('terminal'):
        return {'launch_pending': cycle['id']}
    state = owned_state(n, cycle)
    agent = (state or {}).get('status') or {}
    if state and state.get('state') == 'live' and agent.get('state') not in ('idle', 'done'):
        return {'busy_or_blocked': agent, 'terminal': cycle['terminal']}
    if not cycle.get('session_id'):
        key = 'starting' if time.time() - cycle['created'] < 180 else 'needs_attention'
        return {key: '等待守墓人首轮登记；未重发任务', 'terminal': cycle['terminal']}
    with n.db() as db:
        pending = n.open_jobs(db)
        run = db.execute('SELECT * FROM runs WHERE id=?', (cycle['run_id'],)).fetchone()
    if pending:
        if not state or state.get('state') != 'live':
            return {'needs_attention': '守墓人中断但仍有未验收任务；保留归属与会话，停止自动归档', 'jobs': pending}
        return wake_for_jobs(n, cycle, pending)
    info = session_info(cycle['session_id'])
    if info['cwd'] != str(n.root):
        raise RuntimeError('守墓人 session 目录不匹配')
    if state and state.get('state') == 'live':
        if not run or run['status'] != 'complete':
            return {'needs_attention': '会话已停下但巡检尚未 finish，保留本轮', 'terminal': cycle['terminal']}
        if not transcript_complete(info, max(cycle['created'], run['finished'] or 0)):
            return {'waiting_for_turn_end': cycle['terminal']}
        cycle['outcome'] = 'complete'
    else:
        cycle['outcome'] = 'complete' if run and run['status'] == 'complete' else 'interrupted'
    cycle['status'] = 'archiving'
    save(n, cycle)
    return archive_cycle(n, cycle)


def wake_for_jobs(n, cycle, pending):
    actionable=[x for x in pending if x['status'] in ('ready','tested')]
    if not actionable:
        return {'waiting_for_jobs': pending, 'cycle': cycle['id']}
    key=json.dumps([(x['id'],x['round'],x['status']) for x in actionable],sort_keys=True)
    wake=cycle.get('wake',{})
    if wake.get('key')==key and (wake.get('accepted_at') or time.time()-wake.get('sent_at',0)<300):
        return {'waiting_for_acceptance': actionable, 'terminal': cycle['terminal']}
    if wake.get('key')!=key:
        wake={'id':'W-'+uuid.uuid4().hex[:12], 'key':key}
    wake['sent_at']=time.time();cycle['wake']=wake;save(n,cycle)
    entry=Path(__file__).parent/'dispatch.py'
    message=(f'你仍是原轮次 {cycle["id"]} 的守墓人，session 不变。开发已有交付，或本轮测试待验收。先执行：\n'
             f'```bash\npython3 {entry} --root {n.root} guardian-continue {cycle["id"]} --delivery {wake["id"]}\n```\n'
             '仅 accepted=true 后继续，重复通知直接结束回合。按最新守墓人职责读取待办：ready 由你亲自编写/执行测试；'
             'tested 由你审阅测试报告并最终验收，失败退回原开发 Agent。开发者只开发，不测试、不验收。'
             '仍有未验收任务时结束回复并待命，不执行 finish，不归档；所有任务验收通过后才 finish。')
    call([BIN/'hc','send',cycle['terminal'],message],timeout=60)
    return {'woken':cycle['terminal'],'delivery':wake['id'],'jobs':actionable}


def continue_cycle(n, ident, delivery):
    with n.db() as db:cycle=n.require_guardian(db)
    if cycle['id']!=ident or cycle.get('wake',{}).get('id')!=delivery:
        raise ValueError('唤醒轮次或通知不匹配')
    if cycle['wake'].get('accepted_at'):
        return {'accepted':False,'duplicate':True}
    cycle['wake']['accepted_at']=time.time();save(n,cycle)
    with n.db() as db:pending=n.open_jobs(db)
    return {'accepted':True,'cycle':ident,'run_id':cycle['run_id'],'jobs':pending}
