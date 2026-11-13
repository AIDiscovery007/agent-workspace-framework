#!/usr/bin/env python3
"""Deployment helpers. No guessed IDs, implicit sends, or production overwrite."""
import argparse
import contextlib
import fcntl
import getpass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import selectors
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

SKILL = Path(__file__).resolve().parents[1]
ASSETS = SKILL/'runtime/hi-bridge'


def fail(message):
    raise RuntimeError(message)


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name+'.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
    tmp.chmod(0o600)
    tmp.replace(path)


def load(path):
    return json.loads(Path(path).read_text(), parse_int=str)


def run(argv, **kwargs):
    r = subprocess.run(argv, capture_output=True, text=True, timeout=kwargs.pop('timeout', 60), **kwargs)
    if r.returncode:
        # Do not leak command arguments, returned tokens or arbitrary CLI output.
        fail(f'{Path(argv[0]).name} {argv[1] if len(argv)>1 else ""} 退出码 {r.returncode}；在本机检查该步骤，勿复制凭据到聊天')
    return r.stdout


def binary(name, override=None):
    value = override or shutil.which(name)
    if not value or not os.access(value, os.X_OK):
        fail(f'缺少可执行程序 {name}；先按 references/prerequisites.md 安装')
    return str(Path(value).expanduser().absolute())  # retain stable symlink, not versioned Cellar target


def config(base):
    c = json.loads((base/'config.json').read_text())
    for key in ('chat_id','allowed_sender','bot_contact_id','bot_name','workspace','model','reasoning_effort','credentials_file'):
        if not isinstance(c.get(key), str) or not c[key].strip(): fail('配置缺少 '+key)
    if '*' in c['allowed_sender'] or not c['chat_id'].startswith('CHAT') or not c['bot_contact_id'].endswith('@bot.com'):
        fail('群/员工/机器人 ID 不正确，禁止通配发送人')
    if not Path(c['workspace']).is_dir(): fail('项目目录不存在')
    return c


def fingerprint(c):
    keys = ('workspace','chat_id','allowed_sender','bot_contact_id','credentials_file','model','reasoning_effort','network_access','reply_channel','codex_bin','hi_bin','node_bin','app_server_args','sender_policy','additional_allowed_senders','auto_register_enabled','svg_upload_folder')
    return hashlib.sha256(json.dumps({k:v for k,v in c.items() if k not in ('natural_mention_since_seq','group_members_since_seq','auto_register_since_ms')}, sort_keys=True).encode()).hexdigest()


def proof(base, name, data):
    save(base/'setup'/f'{name}.json', {'time':time.time(), 'config_fingerprint':fingerprint(config(base)), **data})


def require_proof(base, name):
    p = base/'setup'/f'{name}.json'
    if not p.exists(): fail(f'缺少 {name} 验收；先完成对应步骤')
    value = load(p)
    if value.get('config_fingerprint') != fingerprint(config(base)) or value.get('passed') is not True:
        fail(f'{name} 未通过或配置已变化；重新验收')
    return value


def module(base):
    os.environ['HI_CODEX_CONFIG'] = str(base/'config.json')
    sys.path.insert(0, str(base))
    spec = importlib.util.spec_from_file_location('deployed_hi_bridge', base/'bridge.py')
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def credentials_ok(c):
    p = Path(c['credentials_file']).expanduser()
    if p.stat().st_mode & 0o077: fail('凭据文件权限过宽；执行 chmod 600 后重试')
    v = load(p).get('hi') or {}
    if v.get('baseUrl') != 'https://redcity-open.xiaohongshu.com':
        fail('本包仅验收过中国区生产域名；不要把凭据发往自定义端点')
    for k in ('appId','appSecret','userAccessToken'):
        if not isinstance(v.get(k), str) or not v[k].strip(): fail('凭据缺少 '+k)
    return v


def credentials(base):
    p = Path(config(base)['credentials_file'])
    if p.exists():
        credentials_ok(config(base))
        print('现有凭据格式和权限通过；未打印、复制或覆盖')
        return
    if not sys.stdin.isatty(): fail('请用户在本机终端运行 credentials；密钥使用隐藏输入，不通过聊天传递')
    app = input('开放平台 App ID: ').strip()
    secret = getpass.getpass('App Secret（隐藏输入）: ').strip()
    token = getpass.getpass('该应用机器人 User Access Token（隐藏输入）: ').strip()
    if not all((app,secret,token)): fail('输入不完整，未写文件')
    p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd = os.open(p, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    with os.fdopen(fd,'w') as f:
        json.dump({'hi':{'baseUrl':'https://redcity-open.xiaohongshu.com','appId':app,'appSecret':secret,'userAccessToken':token}},f,indent=2)
    print('已保存本机凭据，不需要安装 claude-hi')


def deps(base):
    c=config(base)
    env={**os.environ,'PATH':str(Path(c['node_bin']).parent)+os.pathsep+os.environ.get('PATH','')}
    print(run([binary('npm'),'ci','--ignore-scripts','--no-fund','--registry=https://registry.npmjs.org'],cwd=base,env=env,timeout=300))


def doctor(base):
    c=config(base); credentials_ok(c)
    versions={}
    for k, flag in [('hi_bin','--version'),('codex_bin','--version'),('node_bin','--version'),('python_bin','--version')]:
        versions[k]=run([c[k],flag]).strip()
    if int(versions['node_bin'].lstrip('v').split('.')[0]) < 22: fail('本运行包要求 Node >=22；新安装推荐当前受支持的 Node LTS')
    run([c['node_bin'],'-e',"require('ws'); if(!global.fetch)process.exit(1)"],cwd=base)
    top=run([c['codex_bin'],'--help']); app=run([c['codex_bin'],'app-server','--help'])
    args=c.get('app_server_args') or []
    if not args or ('--no-daemon' in args and '--no-daemon' not in top) or ('--stdio' in args and '--stdio' not in app) or ('--listen' in args and '--listen' not in app):
        fail('已保存的 App Server 参数与当前 Codex help 不符；按 prerequisites 兼容分支重新核对')
    run([c['codex_bin'],'login','status'])
    me=json.loads(run([c['hi_bin'],'search:me']))
    if me.get('xhsContactId') != c['allowed_sender']: fail('Hi CLI 登录人和 allowed_sender 不一致')
    for who in (c['allowed_sender'],c['bot_contact_id']):
        v=json.loads(run([c['hi_bin'],'im:check-contact-in-chats','--contact-id',who,'--chat-id',c['chat_id']]))
        if v.get('success') is not True or c['chat_id'] not in (v.get('memberChatIdList') or []): fail('员工或应用机器人不在目标群，先添加成员')
    m=module(base); items=m.read_messages()
    if not items: fail('群消息为空；先由本人在目标群发送一条普通文本，然后重试')
    # Verify canonical detail permissions too; no full chat contents in reports.
    m.read_message(items[0]['id'])
    proof(base,'doctor',{'passed':True,'versions':versions,'message_read':True,'detail_read':True,'membership':True})
    print('PASS：程序/凭据格式/CLI 登录人/群成员/消息与详情读取。尚未证明 WS 事件、模型调用或发消息权限。')


def probe_ws(base, seconds):
    c=config(base); credentials_ok(c); require_proof(base,'doctor')
    if (base/'state.sqlite3').exists(): fail('已有运行状态，勿启动第二个 WS 消费者；改查 status 并用实际闭环验收')
    m=module(base); marker='HI-WS-'+uuid.uuid4().hex[:8]
    print(f'观察窗口 {seconds} 秒。请在目标群通过 @菜单选中 {c["bot_name"]}，发送：{marker}',flush=True)
    p=subprocess.Popen([c['node_bin'],str(base/'ws-transport.cjs')],cwd=base,
        env={**os.environ,'HI_WS_CONFIG':c['credentials_file']},stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    sel=selectors.DefaultSelector();sel.register(p.stdout,selectors.EVENT_READ)
    deadline=time.monotonic()+seconds; buf=b''; result={'passed':False,'connected':False,'pongs':0,'marker':marker}
    try:
        while time.monotonic()<deadline:
            for key,_ in sel.select(timeout=min(1,max(.01,deadline-time.monotonic()))):
                chunk=os.read(key.fileobj.fileno(),65536)
                if not chunk: fail('WS 进程退出；检查应用凭据、网络或事件配置')
                buf+=chunk
                while b'\n' in buf:
                    line,buf=buf.split(b'\n',1); e=json.loads(line)
                    if e['kind']=='connected':result['connected']=True
                    elif e['kind']=='pong':result['pongs']+=1
                    elif e['kind']=='event':
                        packet=json.loads(e['raw'],parse_int=str);payload=packet.get('payload') or {}
                        if isinstance(payload,str):payload=json.loads(payload,parse_int=str)
                        msg=payload.get('imMessage') or {};chat=payload.get('imChat') or {}
                        if packet.get('eventType')=='asn_bot_callback_msg' and chat.get('chatId')==c['chat_id'] and msg.get('creatorContactId')==c['allowed_sender']:
                            canonical=m.read_message(msg['id'])
                            if m.mentions_bot(canonical) and marker in m.text_of(canonical):
                                result.update(passed=True,message_id=msg['id'],event_type=packet['eventType'])
                                proof(base,'probe-ws',result);print('PASS：真实目标群 @事件 +规范消息回读；未启动 Codex，未 ACK。');return
                    elif e['kind']=='error':result['transport_error']=e.get('code')
        proof(base,'probe-ws',result);fail('观察超时；CONNECTED/PONG 不等于收到群事件。按 troubleshooting.md 检查并重开窗口')
    finally:
        sel.close();p.terminate()
        try:p.wait(timeout=5)
        except subprocess.TimeoutExpired:p.kill();p.wait()
        p.stdin.close();p.stdout.close()


def test_send(base):
    c=config(base);credentials_ok(c); require_proof(base,'doctor');m=module(base)
    p=base/'setup/test-send-operation.json'
    if p.exists():
        op=load(p)
        if op['config_fingerprint']!=fingerprint(c):fail('发送测试配置已变化，先核对旧回执；不能盲目重发')
    else:
        op={'operate_code':uuid.uuid4().hex,'marker':'HI-SEND-'+uuid.uuid4().hex[:8],'config_fingerprint':fingerprint(c)};save(p,op)
    if not op.get('message_id'):
        receipt=m.APP_SENDER.send(c['chat_id'],f'**连接验证**\n\n{op["marker"]}\n\n应用机器人发送通道测试。',op['operate_code'],c['bot_contact_id'])
        op['message_id']=receipt['id'];save(p,op)
    msg=m.read_message(op['message_id'])
    if msg.get('creatorContactId')!=c['bot_contact_id'] or msg.get('chatId')!=c['chat_id'] or op['marker'] not in msg.get('data',''):
        fail('发送回执已保存，但回读身份/正文不匹配；不要更换操作码重发')
    proof(base,'test-send',{'passed':True,'message_id':op['message_id'],'marker':op['marker']})
    print('PASS：应用机器人发送与目标群回读一致')


def smoke_codex(base):
    require_proof(base,'doctor');m=module(base)
    with tempfile.TemporaryDirectory(prefix='hi-codex-smoke-') as tmp:
        m.BASE=Path(tmp);m.DB=m.BASE/'state.sqlite3';save(m.BASE/'baseline.json',{'seq':'0'});m.initialize()
        log=base/'setup/codex-smoke.log';rpc=m.AppServer(log)
        marker='HI-CODEX-'+uuid.uuid4().hex[:8]
        try:
            result=rpc.run('这是部署验收。只读取当前工作目录路径，不修改任何文件。回复 '+marker+' 并写出当前工作目录的绝对路径。',read_only=True)
            if marker not in result or str(m.ROOT) not in result:fail('模型未返回验收标记及当前项目路径；查看本机 codex-smoke.log')
            thread=m.get_meta('thread_id')
        finally:rpc.close()
    proof(base,'smoke-codex',{'passed':True,'model':m.CONFIG['model'],'effort':m.CONFIG['reasoning_effort'],'thread_id':thread})
    print('PASS：真实 App Server 初始化、线程、指定模型/effort、只读回合已完成；未发群')


def baseline(base):
    c=config(base)
    for name in ('doctor','probe-ws','test-send','smoke-codex'):require_proof(base,name)
    if (base/'baseline.json').exists() or (base/'state.sqlite3').exists():fail('已有基线或运行状态，禁止重置游标')
    messages=module(base).read_messages()
    if not messages:fail('无法取得最新群序号，禁止使用 0 代替')
    seq=max(int(m['seq']) for m in messages)
    save(base/'baseline.json',{'seq':str(seq),'created':time.time(),'chat_id':c['chat_id']})
    c['natural_mention_since_seq']=str(seq);c['group_members_since_seq']=str(seq);c['auto_register_since_ms']=str(int(time.time()*1000));save(base/'config.json',c)
    print('PASS：历史消息基线已冻结。现在以前的消息不会执行；启动后发新验收任务。')


def state(base):
    p=base/'state.sqlite3'
    if not p.exists():return {'initialized':False,'counts':{},'meta':{}}
    with contextlib.closing(sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)) as db:
        meta=dict(db.execute('select key,value from meta')); counts=dict(db.execute('select status,count(*) from inbox group by status'))
        pending=db.execute("select count(*) from outbox where status='pending'").fetchone()[0]
    return {'initialized':True,'counts':counts,'pending_replies':pending,'meta':meta}


def service(base, action, keepawake):
    if sys.platform!='darwin':fail('自动托管只支持 macOS')
    c=config(base);label=c['label']; domain=f'gui/{os.getuid()}'
    directory=Path.home()/'Library/LaunchAgents';directory.mkdir(parents=True,exist_ok=True)
    path=directory/(label+'.plist'); power=directory/(label+'.keepawake.plist')
    if action in ('stop','restart'):
        s=state(base)
        if s['counts'].get('running',0) or s['counts'].get('queued',0) or s.get('pending_replies',0):fail('队列非空，等待任务及待发消息完成再停止/重启，避免中断任务')
    if action=='stop':
        run(['/bin/launchctl','bootout',domain,str(path)])
        if power.exists(): subprocess.run(['/bin/launchctl','bootout',domain,str(power)],capture_output=True)
        print('服务已停止；配置、凭据、去重库和日志均保留');return
    if action=='restart':
        run(['/bin/launchctl','kickstart','-k',domain+'/'+label]);print('已请求空闲重启；稍后用 status 核验新心跳');return
    for name in ('doctor','probe-ws','test-send','smoke-codex'):require_proof(base,name)
    if not (base/'baseline.json').exists():fail('请先冻结 baseline')
    if action=='start':
        run(['/bin/launchctl','bootstrap',domain,str(path)])
        if power.exists():run(['/bin/launchctl','bootstrap',domain,str(power)])
        print('已重新加载已有服务；用 status/accept 验证');return
    if path.exists() or power.exists():fail('LaunchAgent 已存在；不要覆盖，用 status 或 service start')
    paths=list(dict.fromkeys([str(Path(c[k]).parent) for k in ('hi_bin','codex_bin','node_bin','python_bin')]+[str(Path.home()/'.local/bin'),'/opt/homebrew/bin','/usr/local/bin','/usr/bin','/bin','/usr/sbin','/sbin']))
    value={'Label':label,'ProgramArguments':[c['python_bin'],str(base/'bridge.py')],'WorkingDirectory':c['workspace'],
        'EnvironmentVariables':{'PATH':':'.join(paths),'PYTHONUNBUFFERED':'1'},'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,
        'StandardOutPath':str(base/'launchd.out.log'),'StandardErrorPath':str(base/'launchd.err.log'),'ProcessType':'Background'}
    path.write_bytes(plistlib.dumps(value));path.chmod(0o600)
    run(['/usr/bin/plutil','-lint',str(path)]);run(['/bin/launchctl','bootstrap',domain,str(path)])
    if keepawake:
        power.write_bytes(plistlib.dumps({'Label':label+'.keepawake','ProgramArguments':['/usr/bin/caffeinate','-i'],'RunAtLoad':True,'KeepAlive':True}))
        power.chmod(0o600);run(['/bin/launchctl','bootstrap',domain,str(power)])
    proof(base,'service',{'passed':True,'keepawake':keepawake,'label':label})
    print('LaunchAgent 已载入；需要 status 和真实群任务证明运行。caffeinate -i 允许息屏，仅阻止空闲系统睡眠。')


def status(base):
    c=config(base);s=state(base);meta=s.pop('meta')
    now=time.time();s['ws_online']=meta.get('ws_state')=='connected' and now-float(meta.get('last_ws_pong') or 0)<20
    s['last_poll_age_seconds']=round(now-float(meta['last_poll'])) if meta.get('last_poll') else None
    s['thread_created']=bool(meta.get('thread_id'));s['label']=c['label'];s['workspace']=c['workspace']
    if sys.platform=='darwin':
        r=subprocess.run(['/bin/launchctl','print',f'gui/{os.getuid()}/{c["label"]}'],capture_output=True,text=True)
        s['launchd_loaded']=r.returncode==0
        match=re.search(r'\bpid = (\d+)',r.stdout);s['pid']=int(match[1]) if match else None
    print(json.dumps(s,ensure_ascii=False,indent=2))


def accept(base, ident, marker, chat_id=None, source="websocket"):
    chat_id = chat_id or config(base)["chat_id"]
    c=config(base);m=module(base)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',ident):fail('message-id 格式不正确')
    if not re.fullmatch(r'HI-E2E-[A-Za-z0-9-]{4,64}',marker):fail('验收标记需为 HI-E2E- 加 4–64 位字母/数字/连字符')
    msg=m.read_message(ident,chat_id=chat_id)
    if not m.mentions_bot(msg) or marker not in (m.command_of(msg) or ''):fail('验收输入必须是本人在目标群的真实 @，且包含验收标记')
    with contextlib.closing(sqlite3.connect((base/'state.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row;rows=db.execute('select * from inbox where id=?',(ident,)).fetchall()
        if len(rows)!=1 or rows[0]['status']!='complete' or marker not in (rows[0]['result'] or ''):fail('任务尚未唯一完成或结果缺少标记；等待队列，勿重发')
        events=db.execute("select count(*) from ws_events where message_id=? and status='processed'",(ident,)).fetchone()[0]
        if source=='websocket' and not events:fail('此消息未证明 WS 事件路径（可能仅靠轮询），先排查事件再发送新的验收标记')
        replies=db.execute('select * from outbox where task_id=? order by id',(ident,)).fetchall()
        if not replies or any(r['status']!='sent' or r['channel']!='app' or not r['message_id'] for r in replies):fail('最终回复未全部应用直发；检查 outbox，不重跑业务')
    full=[]
    for r in replies:
        reply=m.read_message(r['message_id'],chat_id=chat_id)
        if reply.get('chatId')!=chat_id or reply.get('creatorContactId')!=c['bot_contact_id']:fail('回群身份不匹配')
        full.append(reply.get('data',''))
    if marker not in '\n'.join(full):fail('群内回读正文缺少验收标记')
    proof(base,'accept-'+marker,{'passed':True,'input_message_id':ident,'source':rows[0]['source'],'ws_event_seen':bool(events),'chat_id':chat_id,'sender':msg.get('creatorContactId'),'unique_tasks':1,'reply_message_ids':[r['message_id'] for r in replies]})
    print('PASS：真实 @ → '+('WS 事件' if events else '轮询补收')+' → 唯一任务 → Codex 完成 → 原群应用回复与回读')


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base',type=Path,help='已初始化的项目/.codex/hi-bridge 绝对路径')
    sub=p.add_subparsers(dest='command',required=True)
    for name in ('credentials','deps','doctor','test-send','smoke-codex','baseline','status'):sub.add_parser(name)
    w=sub.add_parser('probe-ws');w.add_argument('--seconds',type=int,choices=range(15,301),default=120,metavar='15..300')
    s=sub.add_parser('service');s.add_argument('action',choices=['install','start','stop','restart']);s.add_argument('--keepawake',action='store_true')
    a=sub.add_parser('accept');a.add_argument('--message-id',required=True);a.add_argument('--marker',required=True);a.add_argument('--chat-id');a.add_argument('--source',choices=['websocket','any'],default='websocket')
    args=p.parse_args()
    if not args.base:fail('此命令需要 --base 项目/.codex/hi-bridge')
    base=args.base.expanduser().resolve();config(base)
    # Serialize setup mutations; status/accept only read operational state.
    if args.command in ('status','accept'):
        return status(base) if args.command=='status' else accept(base,args.message_id,args.marker,args.chat_id,args.source)
    with (base/'setup.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.command=='probe-ws':return probe_ws(base,args.seconds)
        if args.command=='service':return service(base,args.action,args.keepawake)
        globals()[args.command.replace('-','_')](base)

if __name__=='__main__':
    try:main()
    except (RuntimeError,FileNotFoundError,PermissionError,ValueError,subprocess.TimeoutExpired) as exc:
        print('STOP: '+str(exc),file=sys.stderr);sys.exit(2)
