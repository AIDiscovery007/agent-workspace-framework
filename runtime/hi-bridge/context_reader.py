"""Read bounded context explicitly carried by Hi messages, without executing actions."""
import json
import subprocess
from message_content import normalize, ident
from attachments import AttachmentError

MAX_CONTEXTS=50
MAX_DEPTH=3
MAX_TEXT=200_000

def query(hi,command,*args):
    try:
        proc=subprocess.run([hi,command,*args],capture_output=True,text=True,timeout=60)
        if proc.returncode or len(proc.stdout)>8*1024*1024:raise ValueError()
        data=json.loads(proc.stdout,parse_int=str)
        if data.get('success') is False:raise ValueError()
        return data
    except Exception:
        raise AttachmentError('Hi 上下文读取失败（'+command+'），请检查权限或稍后重试。') from None

def expand(messages,hi,bot=None):
    """Forward origin data is provided by the shared bundle; never fetch origin chats."""
    contexts=[];media=[];visited=set();count=0
    def visit(message,label,depth):
        nonlocal count
        count+=1
        if count>MAX_CONTEXTS or depth>MAX_DEPTH:raise AttachmentError('引用或转发内容过多/过深，请拆分后发送。')
        normalized=normalize(message,bot=bot)
        entry={'source':label,'text':normalized['text'],'warnings':normalized['warnings']}
        contexts.append(entry);media.extend({**item,'source':label} for item in normalized['media'])
        for eid in normalized['cards']:
            key=('card',eid)
            if key in visited:continue
            visited.add(key)
            if sum(k[0]=='card' for k in visited)>5:raise AttachmentError('单条任务最多读取 5 张卡片。')
            cards=query(hi,'im:card-summary','--entity-id',eid).get('cards') or []
            matching=[c for c in cards if str(c.get('entityId'))==eid]
            if len(matching)!=1:raise AttachmentError('卡片实例回读不匹配，停止处理。')
            card=matching[0]
            if card.get('status') in ('missing','invalid') or not isinstance(card.get('elements'),dict):
                raise AttachmentError('卡片正文不可用，无法只凭标题处理。')
            elements={}
            for path,item in card['elements'].items():
                if not isinstance(item,dict):continue
                ext=item.get('ext') or {}
                # No template scripts, callback payloads or hidden renderData are executed/exposed.
                safe={k:v for k,v in ext.items() if k in {'text','label','value','users','tags','src','url','alt','placeholder','options','disabled','checked','name','visibility'}} if isinstance(ext,dict) else {}
                elements[path]={'type':item.get('type'),'title':item.get('title'),'ext':safe}
                if item.get('type')=='image' and safe.get('visibility') not in ('unknown','hidden',False):
                    url=safe.get('src') or safe.get('url')
                    if isinstance(url,str) and url:media.append({'kind':'image','url':url,'name':'卡片图片','allow_unknown_size':True})
            contexts.append({'source':'卡片实例','entity_id':eid,'status':card.get('status'),
                'warnings':card.get('warnings') or [],'unresolvedVariables':card.get('unresolvedVariables') or [],
                'elements':elements,'note':'按钮和表单仅为可读内容，未点击或提交；显示条件未求值时不得称为可见。'})
        for fid in normalized['forwards']:
            key=('forward',fid)
            if key in visited:
                entry['warnings'].append('重复或循环转发已跳过。');continue
            visited.add(key)
            items=query(hi,'im:forward-messages','--forward-group-id',fid).get('result')
            if not isinstance(items,list) or not items:raise AttachmentError('合并转发没有返回原消息，无法仅凭摘要处理。')
            if len(items)+count>MAX_CONTEXTS:raise AttachmentError('合并转发超过 50 条，请拆分后发送。')
            for item in items:
                if str(item.get('forwardGroupId'))!=fid:raise AttachmentError('转发资源归属不匹配。')
                if str(item.get('originMsgCategory','1'))!='1':continue
                visit({'type':item.get('originMsgType'),'data':item.get('originMsgData')},'合并转发中的资料',depth+1)
                if item.get('originRefMsgData'):
                    visit({'type':item.get('originRefMsgType'),'data':item['originRefMsgData']},'转发内附带引用资料',depth+1)
    for index,message in enumerate(messages):visit(message,'本条消息' if index==0 else '直接引用资料',0)
    if len(json.dumps(contexts,ensure_ascii=False))>MAX_TEXT:raise AttachmentError('消息上下文超过 20 万字符，请缩小范围。')
    return contexts,media

def thread_context(message,hi):
    tid=(message.get('threadContext') or {}).get('threadId')
    if not tid:return []
    tid=ident(tid);chat=ident(message.get('chatId'))
    data=query(hi,'im:thread-replies','--chat-id',chat,'--thread-id',tid,'--order','2','--limit','20')
    items=data.get('result')
    if not isinstance(items,list):raise AttachmentError('话题正文读取结果异常。')
    result=[]
    for item in items[:20]:
        if item.get('chatId')!=chat or (item.get('threadContext') or {}).get('threadId')!=tid:
            raise AttachmentError('话题消息不属于当前群或话题。')
        if str(item.get('category'))!='1' or str(item.get('recallFlag','0'))!='0' or item.get('id')==message.get('id'):continue
        result.append(item)
    return result
