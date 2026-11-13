"""Hi message bodies normalized without granting authority to embedded content."""
import json
import re
from attachments import AttachmentError

TYPES={1:'text',2:'image',3:'video',4:'voice',5:'sticker',6:'file',9:'richText',10:'markdown',11:'forward',12:'emoji',13:'card',14:'stream'}
QUOTES={'quote','blockquote','reference','forward'}
MAX_NODES=3000

def body(message):
    raw=message.get('data') or '{}'
    if isinstance(raw,str):
        if len(raw)>2_000_000:raise AttachmentError('消息正文过大，请分段发送。')
        try:raw=json.loads(raw)
        except (ValueError,TypeError):raise AttachmentError('消息正文格式异常，请重新发送。') from None
    if not isinstance(raw,dict):raise AttachmentError('消息正文不是对象，无法解析。')
    return raw

def ident(value):
    if isinstance(value,int):value=str(value)
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value):
        raise AttachmentError('消息资源标识无效，停止读取。')
    return value

def string(value):
    return value if isinstance(value,str) else ''

def normalize(message, bot=None, include_quotes=False):
    data=body(message)
    try:typ=int(message.get('type',0))
    except (ValueError,TypeError):typ=0
    result={'text':'','media':[],'cards':[],'forwards':[],'warnings':[]}
    seen=set();count=0
    def media(kind,value):
        if not isinstance(value,dict):raise AttachmentError('媒体节点格式异常。')
        info=value.get('videoInfo') or {} if kind=='video' else value
        if not isinstance(info,dict):info={}
        url=info.get('url') or value.get('url')
        if not url:raise AttachmentError('媒体节点缺少下载地址，请重新发送原文件。')
        result['media'].append({'kind':'image' if kind=='sticker' else kind,'url':url,
            'name':value.get('name') or {'image':'图片','sticker':'贴纸','video':'视频','voice':'语音','file':'附件'}[kind],
            'size':info.get('size') or info.get('resourceSize') or value.get('size') or value.get('resourceSize'),
            'allow_unknown_size':True})
        return ''
    def card(value):
        if not isinstance(value,dict):raise AttachmentError('卡片结构异常。')
        entity=value.get('entity') or value
        if isinstance(entity,str):
            try:entity=json.loads(entity)
            except ValueError:raise AttachmentError('卡片实例信息格式异常。') from None
        eid=entity.get('entityId') if isinstance(entity,dict) else None
        if not eid:raise AttachmentError('卡片缺少实例 ID，无法只凭标题读取正文。')
        result['cards'].append(ident(eid))
        return '[卡片内容将在读取实例后提供]'
    def forward(value):
        if not isinstance(value,dict):raise AttachmentError('转发消息结构异常。')
        result['forwards'].append(ident(value.get('forwardGroupId')))
        return '[合并转发内容将在读取后提供]'
    def stream(value):
        if not isinstance(value,dict):raise AttachmentError('流式消息结构异常。')
        chunks=value.get('chunks')
        if chunks is not None:
            if not isinstance(chunks,list) or len(chunks)>MAX_NODES:raise AttachmentError('流式消息片段过多或格式异常。')
            parts={};seen_codes=set();finished=False
            for c in chunks:
                if not isinstance(c,dict):continue
                code=c.get('operateCode')
                if code and code in seen_codes:continue
                if code:seen_codes.add(code)
                kind=c.get('type');pid=str(c.get('partId') or 'text')
                if kind=='textStart':parts.setdefault(pid,'')
                elif kind=='textDelta':parts[pid]=parts.get(pid,'')+string(c.get('delta'))
                elif kind=='controlFinish':finished=True
                elif kind in ('controlAbort','controlError'):result['warnings'].append('流式消息已中断，以下仅为已收到部分。')
            if not finished:result['warnings'].append('流式消息尚未确认结束，以下为当前快照。')
            return '\n'.join(parts.values())
        for key in ('markdown','plainText'):
            v=value.get(key)
            if isinstance(v,dict):
                t=string(v.get('text')) or string(v.get('value'))
                if t:return t
        for key in ('text','content'):
            if isinstance(value.get(key),str):return value[key]
        raise AttachmentError('流式消息尚无可读正文或结构未支持，请结束生成后引用重试。')
    def leaf(kind,value):
        if not isinstance(value,dict):raise AttachmentError('消息节点内容格式异常。')
        if kind in ('image','sticker','file','video','voice'):return media(kind,value)
        if kind=='card':return card(value)
        if kind=='forward':return forward(value)
        if kind=='stream':return stream(value)
        if kind=='emoji':return string(value.get('name')) or string(value.get('code')) or '[表情]'
        if kind in ('text','markdown'):
            t=string(value.get('text')) or string(value.get('value'))
            if kind=='text':
                if value.get('bold'):t='**'+t+'**'
                if value.get('italic'):t='*'+t+'*'
                if value.get('strikethrough'):t='~~'+t+'~~'
            return t
        return None
    if typ==9:
        rich=data.get('richText') or {};elements={e.get('id'):e for e in rich.get('elements',[]) if isinstance(e,dict)}
        def walk(node,depth=0):
            nonlocal count
            count+=1
            if count>MAX_NODES or depth>32:raise AttachmentError('富文本节点过多或嵌套过深。')
            if not isinstance(node,dict):return ''
            e=elements.get(node.get('id'),{});kind=e.get('type') or node.get('type') or 'container'
            if kind in QUOTES and not include_quotes:return ''
            if node.get('id') and node['id'] in seen:return ''
            if node.get('id'):seen.add(node['id'])
            value=e.get({'numbered-list':'numberedList','bulleted-list':'bulletedList'}.get(kind,kind)) or {}
            if kind=='mention':return '' if value.get('mentionId')==bot else '@'+str(value.get('mentionName') or value.get('mentionId') or '')+' '
            if kind=='link':
                child=''.join(walk(x,depth+1) for x in node.get('children',[])).strip()
                href=string(value.get('href'));label=child or string(value.get('title')) or href
                return '['+label+']('+href+')' if href else label
            found=leaf(kind,value)
            if found is not None:return found
            children=''.join(walk(x,depth+1) for x in node.get('children',[]))
            if kind=='paragraph':return children.rstrip()+'\n'
            if kind in ('numbered-list','bulleted-list'):
                try:level=max(1,min(10,int(value.get('level') or 1)));index=max(1,int(value.get('index') or 1))
                except (TypeError,ValueError):level,index=1,1
                marker=str(index)+'. ' if kind=='numbered-list' else '- '
                return '  '*(level-1)+marker+children.rstrip()+'\n'
            if kind in QUOTES:return '\n[引用资料]\n'+children+'\n'
            if kind not in ('root','columns','column','section','container'):
                result['warnings'].append('未识别的富文本节点：'+str(kind)[:60])
            return children
        result['text']=walk(rich.get('root') or {}).strip()
    elif typ==1:
        plain=data.get('plainText') or data
        result['text']=string(plain.get('text')) or string(plain.get('value'))
    elif typ in TYPES:
        kind=TYPES[typ];value=data.get(kind) or data
        result['text']=leaf(kind,value) or ''
    else:
        result['warnings'].append('未支持的消息类型：'+str(typ))
        result['text']='[未支持的消息类型 '+str(typ)+']'
    result['text']=result['text'].strip()
    return result

def media_of(message):
    records=normalize(message)['media']
    if len(records)>10:raise AttachmentError('单条任务最多接收 10 个附件，请分批发送。')
    return records
