"""Copy only explicit, bounded incident reports from a member's own outbox."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

RULES = ('遇到共享流程实际失败、部分失败、静默跳过、错误回执或需人工补救时必须记录，不以整个 Agent 卡死或根因已知为前提。'
         '例如成员身份/登录信息识别失败，即使已向用户返回失败也要记录。只记录自己核验的事实，推断标待定位，凭据和业务正文不得写入。'
         '隔离成员在本人目录 .notebook-outbox/ 下写 UTF-8 JSON，每事件一个稳定文件名。'
         '字段 title、trigger、expected、actual、attempts、conclusion、impact 均为非空字符串，evidence 为1至12条脱敏证据摘要；'
         'scope 对象包含 domain（instructions/sop/skill/bridge/automation/governance）、component（失败环节）、observed_evidence（实际失败证据）、reusable_reason（同类影响）。'
         '时间已知填 occurred_at（带时区），未知填 unknown 和 time_note。身份、session 和事件键由可信桥接绑定。'
         '桥接代收后生成同名 .receipt 文件；只有回执 accepted=true 才可称已进入共享错题本，否则说明已本地记录、待处理。'
         '不直接写共享目录，不申请额外权限，不读取其他成员记录。')

def collect(root, workspace, task_id, session):
    root=Path(root).resolve();workspace=Path(workspace)
    folder=workspace/'.notebook-outbox'
    if not folder.exists():return []
    if workspace.resolve()!=workspace.absolute() or not workspace.is_relative_to(root/'成员工作区'):
        raise ValueError('invalid workspace')
    fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    results=[]
    try:
        for name in sorted(os.listdir(fd))[:100]:
            if not name.endswith('.json') or len(name)>120:continue
            receipt=name+'.receipt'
            try:
                os.stat(receipt,dir_fd=fd,follow_symlinks=False)
                continue
            except FileNotFoundError:pass
            try:
                f=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
                with os.fdopen(f,'rb') as source:
                    st=os.fstat(source.fileno())
                    if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_size>65536:raise ValueError('invalid report')
                    data=json.loads(source.read(65537))
                data.update(agent='Hi 隔离会话',session=session or 'unknown',business=str(workspace.relative_to(root)),
                            incident_key='hi-outbox:'+str(workspace.relative_to(root))+':'+name)
                data.setdefault('occurred_at','unknown')
                data.setdefault('time_note','成员未提供精确发生时间；桥接仅确认本任务代收，不以代收时间伪造发生时间')
                p=subprocess.run([sys.executable,str(root/'.codex/gravekeeper/notebook.py'),'submit'],
                                 input=json.dumps(data,ensure_ascii=False),capture_output=True,text=True,timeout=30)
                if p.returncode:raise ValueError('report validation failed')
                result={'accepted':True,**json.loads(p.stdout)}
            except Exception as exc:
                # No raw error or submitted content enters shared bridge logs.
                result={'accepted':False,'error':type(exc).__name__,'hint':'核对字段、脱敏和文件类型后删除本回执再重试'}
            try:
                out=os.open(receipt,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
                with os.fdopen(out,'w') as dest:json.dump(result,dest,ensure_ascii=False)
            except FileExistsError:pass
            results.append(result)
    finally:os.close(fd)
    return results
