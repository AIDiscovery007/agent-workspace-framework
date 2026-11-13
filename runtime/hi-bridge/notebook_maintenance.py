"""Owner-authorized, local-only backscan queue; executed by the normal serial worker."""
import json
import time

def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS notebook_maintenance(
        id TEXT PRIMARY KEY,chat_id TEXT NOT NULL,sender TEXT NOT NULL,thread_id TEXT NOT NULL,
        status TEXT NOT NULL,created REAL,result TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS maintenance_requests(
        id TEXT PRIMARY KEY, prompt TEXT NOT NULL, authorization TEXT NOT NULL)''')
    # Submission is idempotent; an uncertain run is not automatically replayed.
    db.execute("UPDATE notebook_maintenance SET status='interrupted' WHERE status='running'")

def step(b):
    """Called only when the business inbox is empty. No fake Hi message or group reply."""
    with b.connect() as db:
        row=db.execute("SELECT * FROM notebook_maintenance WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        if not row:return False
        db.execute("UPDATE notebook_maintenance SET status='running' WHERE id=?",(row['id'],))
    rpc=None
    try:
        if b.get_meta(b.session_key(row['sender'],row['chat_id']))!=row['thread_id']:
            raise ValueError('原 session 绑定已变化，维护任务未执行')
        b.require_current_member(row['sender'],chat_id=row['chat_id'])
        run=b.BASE/'logs'/row['id'];run.mkdir(exist_ok=True)
        with b.connect() as db:
            request=db.execute('SELECT * FROM maintenance_requests WHERE id=?',(row['id'],)).fetchone()
        rpc=b.AppServer(run/'app-server.log', sender=row['sender']);b.ACTIVE_RPC=rpc
        rpc.local_only=True
        prompt=(f'项目所有者已授权所有项目 Agent 回扫并填写新上线的共享错题本。'
                f'这是本地维护任务 {row["id"]}，当前 session 为 {row["thread_id"]}；不是群内新消息。'
                '先读最新 SOP/错题记录与治理SOP.md，回顾本 session 可核验的共享流程实际失败、部分失败、错误回执或人工补救；身份/登录信息识别失败也应记录，根因可待定位。已确认纯业务/外部故障且共享处理正常不提交。'
                '不读取其他成员会话、其他群聊天，不修改业务文件或桥接配置。'
                '提供 scope 的具体环节、observed_evidence 实际失败证据和同类影响，再用 notebook.py submit 提交；同一次事件沿用稳定事件标识。不能把业务目录中发生或经验有用当作收录理由，共享环节失败已核验但根因未明可提交 candidate，由守墓人核实。'
                '优先使用原消息ID/任务ID与故障环节作为 incident_key；没有真实时间不要编造，用 occurred_at=unknown 并填 time_note 说明范围/缺失原因。'
                '记录数据库现位于可写的 错题本/.state/，脚本仍在 .codex/gravekeeper/；此前受保护目录的写入问题已修正，无需提升权限。'
                '事件可能已由其他 Agent记录，先查共享索引去重；无数量目标，不记流水，不为回扫而制造记录。'
                '无真实可确认问题则如实写无新增。不要调用消息发送接口，本次不回群。'
                '最终简短列出提交 ID 或无新增原因。')
        if request:
            prompt=('这是由项目所有者明确授权、通过桥接串行队列执行的本地验收任务，'
                    '不是群内新消息；沿用本 session 的桥接模型、effort 和权限，不继承调度者配置。'
                    '只执行以下授权范围，不向 Hi 群发送消息，不操作其他会话。\n'
                    '授权依据：'+request['authorization']+'\n任务：\n'+request['prompt'])
        result=rpc.run(prompt,sender=row['sender'],chat_id=row['chat_id'])
        (run/'result.txt').write_text(result)
        with b.connect() as db:
            db.execute("UPDATE notebook_maintenance SET status='complete',result=? WHERE id=?",(result,row['id']))
    except Exception as exc:
        # Store only a safe class; detailed task logs remain in the local run directory.
        with b.connect() as db:
            db.execute("UPDATE notebook_maintenance SET status='failed',result=? WHERE id=?",(type(exc).__name__,row['id']))
        b.log.exception('notebook maintenance failed %s',row['id'])
    finally:
        if rpc:rpc.close()
        b.ACTIVE_RPC=None
    return True

def enqueue(b, ident, thread_id, prompt, authorization):
    """Local owner-authorized requests only; never exposed as a Hi chat command."""
    import re
    if not re.fullmatch(r'local-[A-Za-z0-9_-]{1,100}', ident):
        raise ValueError('任务 ID 必须为 local- 开头的字母数字下划线或横线')
    if not prompt.strip() or not authorization.strip():
        raise ValueError('必须提供任务及本机所有者的明确授权依据')
    # Resolve the actual owner binding; never guess a chat from a session ID.
    matches=[]
    for chat in b.chat_ids():
        sender=b.CONFIG['allowed_sender']
        if b.get_meta(b.session_key(sender,chat))==thread_id:
            matches.append((chat,sender))
    if len(matches)!=1:
        raise ValueError('必须唯一匹配当前项目所有者的已绑定 Hi session')
    chat,sender=matches[0]
    with b.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        prior=db.execute('SELECT * FROM notebook_maintenance WHERE id=?',(ident,)).fetchone()
        if prior:
            req=db.execute('SELECT * FROM maintenance_requests WHERE id=?',(ident,)).fetchone()
            if not req or prior['thread_id']!=thread_id or req['prompt']!=prompt or req['authorization']!=authorization:
                raise ValueError('任务 ID 已被不同内容占用')
            return {'id':ident,'duplicate':True,'status':prior['status']}
        db.execute('INSERT INTO maintenance_requests VALUES (?,?,?)',(ident,prompt,authorization))
        db.execute('INSERT INTO notebook_maintenance VALUES (?,?,?,?,?,?,NULL)',
                   (ident,chat,sender,thread_id,'queued',time.time()))
    return {'id':ident,'status':'queued','thread_id':thread_id}

if __name__=='__main__':
    import argparse
    from pathlib import Path
    p=argparse.ArgumentParser(description='向原 Hi session 提交本机所有者授权的串行验收任务，不回群')
    p.add_argument('--id',required=True)
    p.add_argument('--thread-id',required=True)
    p.add_argument('--prompt-file',type=Path,required=True)
    p.add_argument('--authorization',required=True)
    args=p.parse_args()
    import bridge
    # Do not call initialize(): it marks running tasks interrupted at service startup.
    with bridge.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS maintenance_requests(id TEXT PRIMARY KEY,prompt TEXT NOT NULL,authorization TEXT NOT NULL)')
    print(json.dumps(enqueue(bridge,args.id,args.thread_id,args.prompt_file.read_text(),args.authorization),ensure_ascii=False))
