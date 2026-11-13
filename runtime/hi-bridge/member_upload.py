"""Account-scoped Hi Pan broker; credentials stay in the trusted bridge."""
import hashlib
import json
import os
from pathlib import Path
import socketserver
import stat
import subprocess
import threading
from urllib.parse import urlsplit

BASE=Path(__file__).resolve().parent
TOOLS=BASE/'member-tools'
SENDER='member-a@example.invalid'
LIMIT=512*1024*1024

def enabled(config,sender):
    folder=config.get('member_upload_folders',{}).get(sender)
    return sender in config.get('member_workspaces',{}) and isinstance(folder,str) and bool(folder.strip())

def storage_for(sender):
    # Preserve existing legacy receipts; new accounts never share staging or entryIds.
    account=hashlib.sha256(sender.encode()).hexdigest()
    return BASE/'member-uploads'/account

def socket_path(own):
    return own/'.hi-upload.sock'

def sop_paths(root):
    return [root/'SOP/小红书机器人发送文件链接SOP.md']

def cli(*args):
    r=subprocess.run([os.environ.get('AW_HI_BIN', 'hi'),*args],capture_output=True,text=True,timeout=240)
    if r.returncode:raise ValueError('Hi 网盘调用失败；请所有者检查授权或目录歧义，不更换通道')
    data=json.loads(r.stdout)
    if data.get('success') is False:raise ValueError('Hi 网盘未返回成功结果')
    return data

def save(path,data):
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n');tmp.chmod(0o600);tmp.replace(path)

def snapshot(own,source,dest):
    """Walk with dirfd/O_NOFOLLOW so a symlink swap cannot escape the account root."""
    source=Path(source)
    if not source.is_absolute():source=own/source
    try:parts=source.relative_to(own).parts
    except ValueError:raise ValueError('只允许上传本人工作区文件')
    if not parts or any(p in ('.','..') or p.startswith('.') for p in parts):
        raise ValueError('禁止上传隐藏目录、浏览器状态、路径跳转或凭据')
    fd=os.open(own,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=child
        sourcefd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
        with os.fdopen(sourcefd,'rb') as f:
            st=os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_size>LIMIT:
                raise ValueError('只允许512 MiB以内的普通文件，不允许硬链接')
            digest=hashlib.sha256();total=0
            with dest.open('wb') as out:
                while chunk:=f.read(1024*1024):
                    total+=len(chunk)
                    if total>LIMIT:raise ValueError('文件超限')
                    digest.update(chunk);out.write(chunk)
            after=os.fstat(f.fileno())
            if (st.st_size,st.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('文件上传前仍在变化，请完成写入后重试')
            return parts[-1],total,digest.hexdigest()
    finally:os.close(fd)

class Broker:
    def __init__(self,config,sender,own,storage=None):
        if not enabled(config,sender):raise ValueError('未授权文件上传')
        expected=Path(config['workspace'])/config['member_workspaces'][sender]
        if own!=expected or own.is_symlink() or own.resolve()!=own.absolute():
            raise ValueError('上传工作区与账号绑定不一致')
        self.own=own;self.folder=config['member_upload_folders'][sender]
        self.storage=Path(storage) if storage else storage_for(sender)
        self.storage.mkdir(parents=True,exist_ok=True)
        self.index=self.storage/'index.json'
        self.records=json.loads(self.index.read_text()) if self.index.exists() else {}
        self.lock=threading.Lock()
    def dispatch(self,args):
        if not isinstance(args,list) or not args or not all(isinstance(x,str) for x in args):raise ValueError('无效命令')
        command,*flags=args
        supported={'utils:generate-operate-code','pan:config','pan:write','pan:readmeta','pan:share'}
        if command not in supported:raise ValueError('仅开放本人文件上传、回读和分享；不开放搜索、管理或消息接口')
        if flags==['--help']:return {'help':{'utils:generate-operate-code':'无参数','pan:config':'--get-default-folder','pan:write':'--file-path <本人文件> --folder-path <本人目录> --operate-code <已生成操作码>','pan:readmeta':'--entry-id <本人已上传文件ID>','pan:share':'--entry-id <本人已上传文件ID>'}[command]}
        if command=='pan:config':
            if flags!=['--get-default-folder']:raise ValueError('不允许修改全局网盘配置')
            return {'defaultFolderPath':self.folder,'updated':False}
        with self.lock:
            if command=='utils:generate-operate-code':
                if flags:raise ValueError('不支持附加参数')
                code=cli(command)['operateCode']
                if not isinstance(code,str) or not code or not all(c.isalnum() or c in '-_' for c in code):raise ValueError('操作码无效')
                self.records[code]={'operateCode':code};save(self.index,self.records);return {'operateCode':code}
            if len(flags)%2:raise ValueError('参数须为键值对')
            params=dict(zip(flags[::2],flags[1::2]))
            if len(params)!=len(flags)//2:raise ValueError('参数重复')
            if command in ('pan:readmeta','pan:share'):
                if set(params)!={'--entry-id'}:raise ValueError('仅支持文件entry-id')
                entry=params['--entry-id']
                if not any(r.get('receipt',{}).get('entryId')==entry for r in self.records.values()):raise ValueError('只能访问本人通过此入口上传的文件')
                return cli(command,'--entry-id',entry)
            if set(params)!={'--file-path','--folder-path','--operate-code'}:raise ValueError('上传必须提供文件、本人目录和操作码')
            if params['--folder-path']!=self.folder:raise ValueError('只允许上传到本人已配置的网盘目录')
            code=params['--operate-code']
            if code not in self.records:raise ValueError('操作码不是本入口生成的，请先生成操作码')
            record=self.records[code];stage=self.storage/(code+'.incoming')
            name,size,digest=snapshot(self.own,params['--file-path'],stage)
            if record.get('sha256') and (record['sha256'],record['fileName'])!=(digest,name):raise ValueError('操作码已用于不同文件，不能替换内容')
            folder=self.storage/code;folder.mkdir(exist_ok=True)
            upload=folder/name;stage.replace(upload)
            record.update(sha256=digest,fileName=name,size=size,folder=self.folder);save(self.index,self.records)
            if not record.get('receipt'):
                receipt=cli('pan:write','--file-path',str(upload),'--file-name',name,'--folder-path',self.folder,'--operate-code',code)
                if not all(receipt.get(k) for k in ('entryId','shareUrl','fileName')):raise ValueError('上传回执不完整')
                record['receipt']=receipt;save(self.index,self.records)
            receipt=record['receipt'];meta=cli('pan:readmeta','--entry-id',receipt['entryId'])
            if (meta.get('entryId')!=receipt['entryId'] or meta.get('fileName')!=name or int(meta.get('fileSize',-1))!=size or meta.get('status')!='ACTIVE'):raise ValueError('远端文件核验未通过，不交付链接')
            if urlsplit(receipt['shareUrl']).scheme!='https':raise ValueError('分享URL无效')
            record.update(verified=True,metadata=meta);save(self.index,self.records)
            return receipt
    def start(self):
        broker=self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(300)
                try:
                    raw=self.rfile.readline(65537)
                    if len(raw)>65536:raise ValueError('请求过长')
                    result={'ok':True,'result':broker.dispatch(json.loads(raw))}
                except Exception as exc:
                    result={'ok':False,'error':str(exc) if isinstance(exc,ValueError) else '上传代理失败，请保留操作码重试或联系所有者'}
                self.wfile.write((json.dumps(result,ensure_ascii=False)+'\n').encode())
        path=socket_path(self.own)
        if path.exists() or path.is_symlink():path.unlink()
        self.server=socketserver.UnixStreamServer(str(path),Handler);path.chmod(0o600)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        return self
    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
        socket_path(self.own).unlink(missing_ok=True)
