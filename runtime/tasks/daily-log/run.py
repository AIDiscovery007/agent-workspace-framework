#!/usr/bin/env python3
"""Weekday digest: local evidence -> read-only Codex -> bounded REDoc publication."""
import argparse
from datetime import datetime,timedelta,time as day_time
import fcntl,json,re,sqlite3,subprocess,sys,time
from pathlib import Path
from urllib.parse import urlsplit,unquote
from zoneinfo import ZoneInfo
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET
import urllib.request

BASE=Path(__file__).resolve().parent;ROOT=BASE.parents[2]
SETTINGS=json.loads((ROOT/'.codex/workspace.json').read_text())
TZ=ZoneInfo(SETTINGS['timezone']);ARCHIVE=SETTINGS.get('archive_document_id','')
HI=SETTINGS['binaries']['hi'];CODEX=SETTINGS['binaries']['codex']
sys.path.insert(0,str(BASE))
import session_runner
sys.path.insert(0,str(ROOT/'.codex/notifications'))
from hi_format import format_reply

def notify(message):
 if not SETTINGS.get('notifications_enabled', False): return
 config=json.loads((ROOT/'通知配置/webhooks.json').read_text())
 url=config['hi']['codex_scheduled_task_complete']['url']
 for part in format_reply(message,status='error' if '异常' in message else 'done'):
  req=urllib.request.Request(url,data=json.dumps({'msgtype':'markdown','markdown':{'content':part}}).encode(),headers={'Content-Type':'application/json'})
  with urllib.request.urlopen(req,timeout=25) as response:result=json.load(response)
  if result.get('errcode')!=0:raise RuntimeError('Hi 定时通知发送失败')

def save(path,obj):
 temp=path.with_suffix('.tmp');temp.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n');temp.replace(path)
def stamp(raw):return datetime.fromisoformat(raw.replace('Z','+00:00')).astimezone(TZ)
def due(now,state):return now.weekday()<5 and now.hour>=20 and state.get('completed_date')!=now.date().isoformat()
def opted_out(text):return bool(re.search(r'(?:不要|禁止|不得|无需|不)(?:记录|归档|沉淀)',text))
def redact(text):
 text=re.sub(r'https?://[^\s<>"\)]+(?:webhook|token=|key=|signature=)[^\s<>"\)]*','[敏感链接已省略]',str(text),flags=re.I)
 text=re.sub(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b','[邮箱已省略]',text,flags=re.I)
 return text

# Diagnostics are a separate, lossy view; never feed them back into validate/publish.
DIAGNOSTIC_SECRET=r'(?:authorization|proxy[-_ ]?authorization|(?:access|refresh|id|auth)[-_ ]?token|token|(?:api|secret|access|private)[-_ ]?key|client[-_ ]?secret|password|passwd|secret|signature|cookie|set-cookie)'
DIAGNOSTIC_EMAIL=r'(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b'
def diagnostic_text(value,limit=1200):
 text=str(value)
 def safe_url(match):
  raw=match.group(0)
  if '://' not in raw:return raw
  try:
   decoded=unquote(raw)
   # A diagnostic token may include an error prefix before the URL. Classify
   # its complete authority/path, including credentials, without dropping it.
   parts=urlsplit('https://'+decoded.split('://',1)[1])
   if parts.query or parts.fragment or parts.username is not None or re.search(DIAGNOSTIC_EMAIL,decoded,re.I) or re.search(r'webhook|token|secret|signature|api[-_]?key',decoded,re.I):
    return '[敏感链接已省略]'
  except ValueError:return '[异常链接已省略]'
  return raw
 # Consume disjoint complete tokens, then inspect the literal URL delimiter.
 # A greedy scheme search retries every suffix of a long non-URL. Tokenizing
 # has no required suffix and does not miss URLs embedded after error prefixes.
 # Scan complete values before truncating; never expose a raw secret suffix.
 text=re.sub(r'[^\s<>"\'\)]+',safe_url,text)
 text=re.sub(DIAGNOSTIC_EMAIL,'[邮箱已省略]',text,flags=re.I)
 # Consume whole unquoted values/lines: retaining a suffix of a secret is unsafe.
 text=re.sub(r'(?i)(?<![\w])('+DIAGNOSTIC_SECRET+r')["\']?\s*[:=]\s*(?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\r\n]+)',
             lambda m:m.group(1)+'=[敏感值已省略]',text)
 text=re.sub(r'(?i)\b(?:Bearer|Basic)\s+[^\s,"\'<>]+','[认证值已省略]',text)
 text=re.sub(r'(?i)(?<![\w])('+DIAGNOSTIC_SECRET+r')\s+[^\r\n]+',lambda m:m.group(1)+' [敏感值已省略]',text)
 # Conservatively omit a whole key-like token, including malformed/long JWTs,
 # rather than retrying the JWT grammar at every internal hyphen boundary.
 text=re.sub(r'[A-Za-z0-9_.-]+',lambda m:'[密钥已省略]' if re.search(r'(?:^|[.-])(?:sk-|eyJ)',m.group(0)) else m.group(0),text)
 text=re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]','',text)
 marker='…[已截断]'
 return text if len(text)<=limit else text[:max(0,limit-len(marker))]+marker[:limit]

def diagnostic_digest(digest):
 """Bounded JSON-shaped snapshot, with paths retained for ordinary rejected digests."""
 nodes=600;truncated=False
 def visit(value,depth=0):
  nonlocal nodes,truncated
  nodes-=1
  if nodes<0 or depth>12:
   truncated=True;return '[诊断已截断]'
  if isinstance(value,dict):
   result={}
   for key,item in value.items():
    if nodes<=0:
     truncated=True;result['[诊断已截断]']=True;break
    nodes-=1
    if len(str(key))>160:truncated=True
    safe_key=diagnostic_text(key,160)
    # Structured credentials can be numbers, lists or objects, not only strings.
    normalized=re.sub(r'[^a-z0-9]','',str(key).lower())
    sensitive=normalized.endswith(('authorization','token','apikey','secretkey','accesskey','privatekey','clientsecret','password','passwd','secret','signature','cookie'))
    result[safe_key]='[敏感值已省略]' if sensitive else visit(item,depth+1)
   return result
  if isinstance(value,list):
   result=[]
   for item in value:
    if nodes<=0:
     truncated=True;result.append('[诊断已截断]');break
    result.append(visit(item,depth+1))
   return result
  if isinstance(value,str):
   if len(value)>2048:truncated=True
   return diagnostic_text(value,2048)
  if value is None or isinstance(value,(bool,int,float)):return value
  return '[非 JSON 类型：'+type(value).__name__+']'
 snapshot=visit(digest)
 # Cap the final serialized artifact too, including indentation and scalar values.
 if len(json.dumps(snapshot,ensure_ascii=False,indent=2).encode('utf-8'))>60000:
  snapshot={'preview':diagnostic_text(json.dumps(snapshot,ensure_ascii=False),8000)};truncated=True
 return {'status':'unvalidated','diagnostic_only':True,'truncated':truncated,'digest':snapshot}

def collect(start,end):
 events=[]
 db=sqlite3.connect(f'file:{ROOT}/.codex/hi-bridge/state.sqlite3?mode=ro',uri=True)
 try:
  rows=db.execute('select id,prompt,status,result,created from inbox where created>=? and created<? and sender=? order by created',(start.timestamp(),end.timestamp(),SETTINGS['owner'])).fetchall()
  for ident,prompt,status,result,created in rows:
   if opted_out(prompt):continue
   events.append({'id':'hi:'+ident,'time':datetime.fromtimestamp(created,TZ).isoformat(),'task':redact(prompt)[:1500],'status':status,'result':redact(result or '')[:4500]})
 finally:db.close()
 # Long-lived sessions may have been created before the reporting date.
 for p in (Path.home()/'.codex/sessions').rglob('*.jsonl'):
  if p.stat().st_mtime<start.timestamp():continue
  try:
   with p.open() as f:
    first=json.loads(next(f));meta=first.get('payload',{})
    cwd=Path(meta.get('cwd') or '/')
    if not cwd.is_relative_to(ROOT) or cwd.is_relative_to(BASE/'logs') or cwd.is_relative_to(ROOT/'成员工作区'):continue
    if isinstance(meta.get('source'),dict):continue  # avoid duplicate delegated sessions
    task_text=''
    for line in f:
     try:e=json.loads(line)
     except ValueError:continue
     payload=e.get('payload',{})
     if e.get('type')=='response_item' and payload.get('role')=='user':
      parts=[c.get('text','') for c in payload.get('content',[]) if c.get('type')=='input_text']
      text='\n'.join(parts)
      if not text.startswith('# AGENTS.md') and '<environment_context>' not in text:task_text=text.split('\n\n以下是')[0]
     if e.get('type')!='event_msg' or not e.get('timestamp'):continue
     when=stamp(e['timestamp']);v=e.get('payload',{})
     if not start<=when<end or v.get('type')!='task_complete':continue
     if opted_out(task_text):continue
     text=redact(v.get('last_agent_message') or '')
     if not text:continue
     ident='session:'+str(meta.get('id') or p.stem)+':'+str(v.get('turn_id') or e.get('ordinal',e['timestamp']))
     events.append({'id':ident,'time':when.isoformat(),'task':redact(task_text)[:1500],'status':'conversation_complete_not_business_proof','result':text[:6000]})
  except (ValueError,OSError,StopIteration):continue
 for p in (ROOT/'.codex/tasks/organize/logs').glob('*/report.json'):
  try:
   data=json.loads(p.read_text());when=stamp(data['started'])
   if start<=when<end and not data.get('dry_run'):
    events.append({'id':'organize:'+p.parent.name,'time':when.isoformat(),'status':data.get('status'),'result':redact(json.dumps({'moved':data.get('moved',[]),'skipped':data.get('skipped',[])},ensure_ascii=False))[:6000]})
  except (ValueError,KeyError):continue
 events.sort(key=lambda x:(x['time'],x['id']))
 if len(json.dumps(events,ensure_ascii=False).encode())>500000:
  raise RuntimeError('当日证据超过分析预算，未截断、未发布；请拆分核对')
 return events

def analyze(events,run,start,end):
 cfg=json.loads((ROOT/'.codex/hi-bridge/config.json').read_text())
 instructions=(BASE/'prompt.md').read_text().replace('SOP/小跟班关键事项归档SOP.md',str(ROOT/'SOP/小跟班关键事项归档SOP.md'))
 return session_runner.analyze(run,ROOT,cfg['model'],instructions,
  json.loads((BASE/'schema.json').read_text()),
  {'start':start.isoformat(),'end':end.isoformat(),'evidence':events})

def validate(digest,events):
 ids={x['id'] for x in events};source='\n'.join(json.dumps(e,ensure_ascii=False) for e in events)
 if not isinstance(digest,dict) or set(digest)!={'summary','items'}:raise ValueError('摘要结构不符')
 if not isinstance(digest.get('summary'),str) or len(digest['summary'])>120 or not isinstance(digest.get('items'),list):raise ValueError('摘要格式不符')
 for item in digest['items']:
  if not isinstance(item,dict) or set(item)!={'type','action','result','evidence_ids','links'}:raise ValueError('事项结构不符')
  if item['type'] not in ('创建','更新','处理','配置','决策','检查','排障','交付') or not all(isinstance(item[k],str) for k in ('action','result')):raise ValueError('事项字段不符')
  if not isinstance(item['evidence_ids'],list) or not all(isinstance(i,str) for i in item['evidence_ids']) or not isinstance(item['links'],list):raise ValueError('事项来源/链接格式不符')
  if not item['evidence_ids'] or not set(item['evidence_ids'])<=ids:raise ValueError('缺少有效结果依据')
  if len(item['action'])>60 or len(item['result'])>240 or len(item['links'])>2:raise ValueError('日志内容过长')
  for link in item['links']:
   if not isinstance(link,dict) or set(link)!={'title','url'} or not all(isinstance(v,str) for v in link.values()):raise ValueError('链接结构不符')
   url=link['url'];p=urlsplit(url)
   local=Path(url)
   is_local=not p.scheme and local.is_absolute() and local.resolve().is_relative_to(ROOT)
   if (p.scheme!='https' and not is_local) or url not in source or redact(url)!=url or p.query:raise ValueError('产物链接不在证据中或含敏感参数')
  if redact(item['action']+item['result'])!=item['action']+item['result']:raise ValueError('日志包含敏感信息')

def cli(*args,content=None):
 r=subprocess.run([HI,*args],input=content,capture_output=True,text=True,timeout=150)
 if r.returncode:
  command=diagnostic_text(args[0],80) if args and re.fullmatch(r'[a-z][a-z0-9-]*:[a-z][a-z0-9-]*',args[0]) else '[未知子命令]'
  def stream(value):
   # Some tools echo stdin; never include the submitted document in diagnostics.
   value=value or ''
   if content:
    for echo in (content,json.dumps(content,ensure_ascii=False)[1:-1],json.dumps(content)[1:-1]):
     value=value.replace(echo,'[输入正文已省略]')
   return diagnostic_text(value or '').strip() or '[空]'
  raise RuntimeError(f'Hi 文档命令失败：{command}，退出码 {r.returncode}；stdout: {stream(r.stdout)}；stderr: {stream(r.stderr)}')
 d=json.loads(r.stdout)
 if d.get('error'):raise RuntimeError('Hi 文档接口返回错误')
 return d

def getdoc(ident):
 data=cli('docs:get','--shortcut-id',ident,'--format','xml','--mode','common')
 return data,Path(data['xmlPath']).read_text()
def plain(xml):return re.sub(r'\s+','',''.join(ET.fromstring('<root>'+re.sub(r'<title>.*?</title>','',xml,flags=re.S)+'</root>').itertext()))
def children():return cli('docs:menu-list','--shortcut-id',ARCHIVE,'--all')['list']
def verified(ident,expected):
 for _ in range(3):
  meta,body=getdoc(ident)
  if plain(body)==plain(expected):return
  time.sleep(2)
 raise RuntimeError('文档回读未匹配，保留回执，不重复创建')
def ensure_doc(key,title,content,state):
 nodes=children();rec=state.setdefault('documents',{}).setdefault(key,{})
 if not rec.get('id'):
  matches=[n for n in nodes if n['title']==title and n['parentId']==ARCHIVE]
  if len(matches)>1:raise RuntimeError('存在同名日报，需要核对；未自行选择')
  if matches:rec['id']=matches[0]['shortcutId']
 if rec.get('id'):
  if not any(n['shortcutId']==rec['id'] and n['parentId']==ARCHIVE for n in nodes):raise RuntimeError('日志不在指定档案树内，停止写入')
  meta,old=getdoc(rec['id'])
  if plain(old)!=plain(content):cli('docs:edit','--shortcut-id',rec['id'],'--hash',meta['hash'],'--format','xml','--content','-',content=content)
 else:
  if not rec.get('operate_code'):rec['operate_code']=cli('utils:generate-operate-code')['operateCode'];save(BASE/'state.json',state)
  result=cli('docs:create','--parent-id',ARCHIVE,'--title',title,'--format','xml','--content','-','--operate-code',rec['operate_code'],content=content)
  rec['id']=result['shortcutId'];save(BASE/'state.json',state)
 verified(rec['id'],content)
 if not any(n['shortcutId']==rec['id'] and n['parentId']==ARCHIVE for n in children()):raise RuntimeError('创建后父子关系未通过核验')
 save(BASE/'state.json',state)
 return 'https://docs.xiaohongshu.com/doc/'+rec['id']

def publish(digest,day,start,end,state):
 content=f'<p>日期：{day}｜截止：{end.strftime("%m-%d %H:%M")}（北京时间）</p><p>记录键：daily:{day}</p><p>{escape(digest["summary"])}</p><h2>关键事项</h2>'
 for item in digest['items']:
  content+=f'<h3>{escape(item["type"])}｜{escape(item["action"])}</h3><p>{escape(item["result"])}</p>'
  for link in item['links']:
   if link['url'].startswith('/'):
    local=Path(link['url']);label='本机' if local.is_file() else '本机历史路径（现存位置待核对）'
    content+=f'<p>{label}：{escape(str(local.relative_to(ROOT)))}</p>'
   else:content+=f'<p><a href="{escape(link["url"],{chr(34):"&quot;"})}">{escape(link["title"])}</a></p>'
 content+=f'<p>依据：本项目可核验的会话结果与任务记录；覆盖 {start.strftime("%m-%d %H:%M")}—{end.strftime("%m-%d %H:%M")}。此前未沉淀内容标为补记。</p>'
 url=ensure_doc('daily:'+day,day+'｜工作日志',content,state)
 entries=state.setdefault('entries',{});entries[day]={'url':url,'count':len(digest['items'])};save(BASE/'state.json',state)
 older=sorted(entries,reverse=True)[20:];month_urls={}
 for month in sorted({d[:7] for d in older}):
  dates=[d for d in older if d.startswith(month)]
  xml=''.join(f'<p>{d}｜日报｜{entries[d]["count"]} 项关键事项｜<a href="{entries[d]["url"]}">详情</a></p>' for d in sorted(dates,reverse=True))
  month_urls[month]=ensure_doc('month:'+month,month+'｜日志索引',xml,state)
 # Preserve preexisting historical links and user-added blocks; own indices use a marker.
 meta,old=getdoc(ARCHIVE)
 root=ET.fromstring('<root>'+old+'</root>')
 owned_urls={e['url'] for e in entries.values()}|set(month_urls.values())
 for node in list(root):
  if node.tag=='p' and ('｜日报｜' in ''.join(node.itertext()) or '｜月索引｜' in ''.join(node.itertext())) and any(a.get('href') in owned_urls for a in node.iter('a')):root.remove(node)
 body=''.join(ET.tostring(node,encoding='unicode') for node in root if node.tag!='title')
 additions=''
 for d in sorted(entries,reverse=True)[:20]:additions+=f'<p>{d}｜日报｜{entries[d]["count"]} 项关键事项｜<a href="{entries[d]["url"]}">详情</a></p>'
 for month,url_month in sorted(month_urls.items(),reverse=True):additions+=f'<p>{month}｜月索引｜历史日报｜<a href="{url_month}">查看</a></p>'
 body+=additions
 cli('docs:edit','--shortcut-id',ARCHIVE,'--hash',meta['hash'],'--format','xml','--content','-',content=body)
 verified(ARCHIVE,body)
 return url

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--scheduled',action='store_true');ap.add_argument('--dry-run',action='store_true');args=ap.parse_args()
 if not args.scheduled and not args.dry_run:ap.error('仅允许 --scheduled 或 --dry-run')
 now=datetime.now(TZ);day=now.date().isoformat()
 statepath=BASE/'state.json';state=json.loads(statepath.read_text()) if statepath.exists() else {}
 with (BASE/'run.lock').open('w') as lock:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return
  session_runner.cleanup_pending(BASE/'logs')
  state=json.loads(statepath.read_text()) if statepath.exists() else {}
  if args.scheduled and not due(now,state):return
  attempts=state.setdefault('attempts',{})
  if args.scheduled and attempts.get(day,0)>=3:return
  end=now if args.dry_run else datetime.combine(now.date(),day_time(20),TZ)
  start=stamp(state['last_cutoff']) if state.get('last_cutoff') else datetime.combine(now.date(),day_time.min,TZ)
  run=BASE/'logs'/now.strftime('%Y%m%d-%H%M%S-%f');run.mkdir(parents=True,exist_ok=True)
  try:
   if args.scheduled:attempts[day]=attempts.get(day,0)+1;save(statepath,state)
   events=collect(start,end);save(run/'evidence.json',events)
   digest=analyze(events,run,start,end)
   diagnostic=diagnostic_digest(digest);save(run/'digest-diagnostic.json',diagnostic)
   try:validate(digest,events)
   except Exception as exc:
    # urlsplit and other format checks can put rejected values in their messages.
    error=diagnostic_text(str(exc))
    diagnostic.update(status='validation_failed',error_type=type(exc).__name__,error=error)
    save(run/'digest-diagnostic.json',diagnostic)
    raise ValueError('摘要校验失败 ['+type(exc).__name__+']：'+error) from None
   save(run/'digest.json',digest)
   if args.dry_run:print(json.dumps({'dry_run':True,'evidence_count':len(events),'digest':digest,'run':str(run)},ensure_ascii=False));return
   url=publish(digest,day,start,end,state) if digest['items'] else None
   state.update(completed_date=day,last_cutoff=end.isoformat());save(statepath,state)
   save(run/'receipt.json',{'date':day,'url':url,'count':len(digest['items']),'complete':True})
   if url:
    try:notify(f'**工作日志 · {day[5:]}**\n\n沉淀 {len(digest["items"])} 项关键事项。\n\n[查看日报]({url})')
    except Exception as exc:save(run/'notification-error.json',{'error':str(exc),'business_complete':True})
  except Exception as exc:
   save(run/'error.json',{'error':str(exc),'date':day});print(str(exc),file=sys.stderr)
   if args.scheduled and attempts.get(day)==3:
    try:notify('**工作日志 · 异常**\n\n今日归档未完成，已保留本机记录待核对。')
    except Exception:pass
   raise
  finally:
   try:session_runner.cleanup(run)
   except Exception as exc:
    save(run/'cleanup-error.json',{'error':str(exc),'business_complete':state.get('completed_date')==day})
    print('日报会话清理失败，将在下次调度检查时单独重试：'+str(exc),file=sys.stderr)

if __name__=='__main__':main()
