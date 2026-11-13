"""Developer delivers code; the owning guardian tests and accepts it."""
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
import guardian_sessions as sessions
from notebook import dump


def require_developer(n, terminal, session=None):
    current=str(uuid.UUID(os.environ.get('CODEX_THREAD_ID','')))
    if not terminal or os.environ.get('HC_TERMINAL_ID')!=terminal or (session and current!=session):
        raise ValueError('开发者终端或 session 不匹配')
    info=sessions.session_info(current)
    cfg=json.loads((n.base/'config.json').read_text())
    central=json.loads((Path(__file__).parent/'config.json').read_text())
    effort=cfg.get('executors',central['executors'])['developer']['effort']
    if info['cwd']!=str(n.root) or info['archived'] or info['reasoning_effort']!=effort:
        raise ValueError('开发者实际目录、状态或 effort 不符')
    with n.db() as db:
        if current==n.meta(db,'guardian_session'):raise ValueError('守墓人不能登记为开发者')
    return current


def report_path(n, ident, report, test=False, round_number=None):
    path=Path(report).resolve()
    folder=n.base/'work'/ident
    if test:folder=folder/'testing'/str(round_number)
    if not path.is_relative_to(folder.resolve()) or not path.is_file():
        raise ValueError('报告必须在该任务的指定目录中')
    return str(path.relative_to(n.root))


def start_test(n, ident, round_number, spec):
    if any(not isinstance(spec.get(k),str) or not spec[k].strip() for k in ('plan','acceptance')):
        raise ValueError('测试计划必须包含 plan、acceptance')
    if not isinstance(spec.get('artifacts'),list) or not spec['artifacts']:
        raise ValueError('测试计划必须列出待测代码 artifacts 路径')
    with n.db() as db:
        db.execute('BEGIN IMMEDIATE')
        job=db.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone()
        if not job or job['round']!=round_number:raise ValueError('任务或轮次不匹配')
        guardian=n.require_guardian(db,job['owner_run'])
        if guardian['session_id']==job['developer_session']:raise ValueError('开发者不能测试自己的交付')
        old=db.execute('SELECT * FROM job_tests WHERE job=? AND round=?',(ident,round_number)).fetchone()
        if old:return {'id':ident,'round':round_number,'status':old['status'],'duplicate':True}
        if job['status']!='ready':raise ValueError('开发尚未交付，不能开始测试')
        artifacts={}
        for value in spec['artifacts']:
            path=(n.root/value).resolve()
            if not path.is_relative_to((n.base/'work'/ident).resolve()) or not path.is_file():
                raise ValueError('待测代码必须在本开发任务目录内')
            artifacts[str(path.relative_to(n.root))]=hashlib.sha256(path.read_bytes()).hexdigest()
        db.execute('INSERT INTO job_tests(job,round,status,session_id,created,spec,artifacts) VALUES (?,?,?,?,?,?,?)',
                   (ident,round_number,'running',guardian['session_id'],time.time(),dump(spec),dump(artifacts)))
        db.execute("UPDATE jobs SET status='testing' WHERE id=?",(ident,))
    folder=n.base/'work'/ident/'testing'/str(round_number);folder.mkdir(parents=True,exist_ok=True)
    return {'id':ident,'round':round_number,'status':'testing','report_directory':str(folder),'artifacts':artifacts}


def test_ready(n,ident,round_number,result,report):
    if result not in ('passed','failed'):raise ValueError('测试结论须为 passed/failed')
    path=report_path(n,ident,report,test=True,round_number=round_number)
    with n.db() as db:
        db.execute('BEGIN IMMEDIATE')
        job=db.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone()
        row=db.execute('SELECT * FROM job_tests WHERE job=? AND round=?',(ident,round_number)).fetchone()
        if not job or job['round']!=round_number or not row:raise ValueError('测试轮次已过期')
        guardian=n.require_guardian(db,job['owner_run'])
        if row['session_id']!=guardian['session_id'] or guardian['session_id']==job['developer_session']:
            raise ValueError('只能由原守墓人提交测试结果')
        if row['status'] in ('passed','failed'):
            if row['status']==result and row['report']==path:return {'duplicate':True}
            raise ValueError('已有不同测试结论，返工需开启新轮次')
        if row['status']!='running' or job['status']!='testing':raise ValueError('测试尚未开始')
        if result=='passed':n.checked_artifacts(json.loads(row['artifacts']))
        digest=hashlib.sha256((n.root/path).read_bytes()).hexdigest()
        db.execute('UPDATE job_tests SET status=?,report=?,report_digest=? WHERE job=? AND round=?',(result,path,digest,ident,round_number))
        db.execute("UPDATE jobs SET status='tested' WHERE id=?",(ident,))
        n.bump(db,'test-ready',{'id':ident,'round':round_number,'result':result,'report':path})
    return {'id':ident,'round':round_number,'status':'tested','test_result':result}
