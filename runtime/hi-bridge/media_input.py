"""Prepare bounded media observations while preserving each member's file boundary."""
import base64
import json
import os
from pathlib import Path
import subprocess
from attachments import AttachmentError
BASE=Path(__file__).resolve().parent
RUNTIME=BASE/'media-runtime'

def profile(directory):
    reads=['/System','/Library','/usr','/bin','/sbin','/dev','/private/etc','/private/var/db','/opt/homebrew',str(RUNTIME),str(directory)]
    return '\n'.join(['(version 1)','(allow default)','(deny network*)','(deny file-read* file-write*)','(allow file-read-metadata)','(allow file-read* (literal "/"))',
        '(allow file-read* (literal '+json.dumps(str(BASE/'media_worker.py'))+'))',
        *['(allow file-read* (subpath '+json.dumps(p)+'))' for p in reads],
        '(allow file-write* (subpath '+json.dumps(str(directory))+'))','(allow file-write* (subpath "/dev"))'])

def enrich(records,directory):
    images=[];directory=Path(directory).absolute()
    if directory.resolve()!=directory or directory.is_symlink():raise AttachmentError('媒体收件目录不安全。')
    for i,record in enumerate(records):
        kind=record.get('kind')
        if kind not in ('voice','video'):continue
        source=Path(record['path'])
        if source.is_symlink() or not source.resolve().is_relative_to(directory):raise AttachmentError('媒体源文件越过本人收件目录。')
        out=directory/f'media-{i:02d}';out.mkdir(exist_ok=True)
        if out.is_symlink() or out.resolve()!=out.absolute():raise AttachmentError('媒体输出目录不安全。')
        env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL')}
        env.update(HOME=str(out),TMPDIR=str(out),HF_HUB_OFFLINE='1',HF_HOME=str(out),TOKENIZERS_PARALLELISM='false')
        try:
            proc=subprocess.run(['/usr/bin/sandbox-exec','-p',profile(directory),str(RUNTIME/'venv/bin/python'),str(BASE/'media_worker.py'),kind,str(source),str(out)],cwd=directory,env=env,capture_output=True,text=True,timeout=900)
            if proc.returncode:raise AttachmentError('媒体处理失败：'+(proc.stderr.strip().splitlines()[-1][:200] if proc.stderr.strip() else '离线解码器不可用'))
            metadata=out/'analysis.json'
            if metadata.is_symlink() or metadata.stat().st_size>1_000_000:raise AttachmentError('媒体分析结果异常。')
            data=json.loads(metadata.read_text())
            for frame in data.get('frames',[]):
                name=frame['path']
                if not isinstance(name,str) or Path(name).name!=name:raise AttachmentError('视频帧路径无效。')
                path=out/name
                if path.is_symlink() or path.stat().st_size>20*1024*1024:raise AttachmentError('视频帧文件无效。')
                content=path.read_bytes()
                images.append({'type':'text','text':'视频采样帧：'+Path(record['path']).name+'；时间 '+str(frame.get('seconds'))+' 秒'})
                images.append({'type':'image','url':'data:image/jpeg;base64,'+base64.b64encode(content).decode('ascii')})
                frame['path']=str(path)
            record['analysis']=data
        except AttachmentError:raise
        except subprocess.TimeoutExpired:raise AttachmentError('媒体分析超时，请拆成更短片段。') from None
        except Exception:raise AttachmentError('媒体处理环境异常，请检查本机离线转写依赖。') from None
    return images
