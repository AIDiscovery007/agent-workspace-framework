#!/usr/bin/env python3
"""Friday 21:00 weekly report, gated on verified Friday daily publication."""
import argparse,fcntl,hashlib,importlib.util,json,re,sys
from datetime import datetime,timedelta,time
from pathlib import Path
from xml.sax.saxutils import escape,quoteattr
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo
BASE=Path(__file__).resolve().parent;ROOT=BASE.parents[2];DAILY=BASE.parent/'daily-log';TZ=ZoneInfo(json.loads((ROOT/'.codex/workspace.json').read_text())['timezone'])
# Load existing daily read/publish helpers without modifying the daily workflow.
sys.path.insert(0,str(DAILY))
spec=importlib.util.spec_from_file_location('daily_helpers',DAILY/'run.py');d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)
spec=importlib.util.spec_from_file_location('weekly_sessions',BASE/'session_runner.py');sessions=importlib.util.module_from_spec(spec);spec.loader.exec_module(sessions)
save=d.save

def read(path,default=None):return json.loads(path.read_text()) if path.exists() else ({} if default is None else default)
def fingerprint(xml):return hashlib.sha256(d.plain(xml).encode()).hexdigest()
def eligible(now):return now.weekday()==4 and now.hour>=21 or now.weekday() in (5,6)
def friday_of(now):return now.date()+timedelta(days=4-now.weekday())

def friday_receipt(friday,state,initial=False):
 day=str(friday)
 if initial:
  r=read(BASE/'setup/early-daily-receipt.json')
  if r.get('date')==day and r.get('complete') and r.get('early'):return r
  raise RuntimeError('提前日报尚未成功，禁止生成初版周报')
 for p in sorted((DAILY/'logs').glob('*/receipt.json'),reverse=True):
  r=read(p)
  if r.get('date')==day and r.get('complete') and not r.get('early'):
   entry=state.get('entries',{}).get(day)
   if r.get('count',0)>0 and (not entry or entry.get('url')!=r.get('url')):continue
   return r
 return None

def collect(friday,initial=False):
 # Shared lock blocks a snapshot while the original daily writer publishes.
 with (DAILY/'run.lock').open('r') as lock:
  try:fcntl.flock(lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
  except BlockingIOError:return None,'周五日报正在执行'
  state=read(DAILY/'state.json');receipt=friday_receipt(friday,state,initial)
  if not receipt:return None,'等待周五工作日日志成功回执（无新增事项也须完成）'
  nodes=d.children();sources=[];missing=[]
  for offset in range(5):
   day=str(friday-timedelta(days=4-offset));entry=state.get('entries',{}).get(day)
   if not entry:
    complete=[read(p) for p in (DAILY/'logs').glob('*/receipt.json')]
    if any(r.get('date')==day and r.get('complete') and r.get('count')==0 for r in complete):
     missing.append(day+'：日报完成，无新增关键事项');continue
    raise RuntimeError(day+' 缺少日报及无事项完成回执，停止生成以免遗漏')
   ident=entry['url'].rsplit('/',1)[-1]
   if not any(n['shortcutId']==ident and n['parentId']==d.ARCHIVE for n in nodes):raise RuntimeError(day+' 日报父目录不匹配')
   meta,xml=d.getdoc(ident)
   if f'daily:{day}' not in xml:raise RuntimeError(day+' 日报记录键不匹配')
   if day==str(friday) and not initial and f'截止：{friday:%m-%d} 20:00' not in xml:
    raise RuntimeError('周五日报云端截止时间尚未达到正式20:00，停止生成')
   sources.append({'day':day,'url':entry['url'],'title':meta['title'],'hash':meta['hash'],'xml':xml})
  return {'friday':str(friday),'monday':str(friday-timedelta(days=4)),'initial':initial,'daily_receipt':receipt,'sources':sources,'no_items':missing},None

def validate(report,bundle):
 if not isinstance(report,dict) or set(report)!={'sections'} or not report['sections']:raise ValueError('周报结构为空或不符')
 days={s['day'] for s in bundle['sources']};urls={s['url'] for s in bundle['sources']}
 for s in bundle['sources']:
  urls.update(a.get('href') for a in ET.fromstring('<root>'+s['xml']+'</root>').iter('a'))
 for section in report['sections']:
  if set(section)!={'heading','projects'} or not section['projects']:raise ValueError('模块结构错误')
  for project in section['projects']:
   if set(project)!={'name','points'} or not project['points']:raise ValueError('项目结构错误')
   for point in project['points']:
    if set(point)!={'text','source_days','links'} or not point['text'] or not point['source_days'] or not set(point['source_days'])<=days:raise ValueError('缺少日报依据')
    if d.redact(point['text'])!=point['text']:raise ValueError('正文包含敏感信息')
    for link in point['links']:
     if set(link)!={'title','url'} or link['url'] not in urls or not link['url'].startswith('https://') or d.redact(link['url'])!=link['url']:raise ValueError('无依据或敏感链接')

def render(report,bundle):
 sources={s['day']:s for s in bundle['sources']};parts=[]
 mode='截至提前日报；周五正式日报完成后更新' if bundle['initial'] else '已包含周五正式工作日志'
 parts.append('<p>周期：'+bundle['monday']+'—'+bundle['friday']+'｜'+mode+'。</p>')
 if bundle['no_items']:parts.append('<p>'+escape('；'.join(bundle['no_items']))+'</p>')
 for section in report['sections']:
  parts.append('<p><b>'+escape(section['heading'])+'</b></p><ol>')
  for project in section['projects']:
   parts.append('<li><b>'+escape(project['name'])+'</b><ol>')
   for point in project['points']:
    # REDoc can drop Hi cloud-drive anchors inside nested lists after save.
    # The source daily document retains the delivery link and is stable here.
    links=[l for l in point['links'] if '/clouddrive/' not in l['url']] or [{'title':day[5:]+'工作日志','url':sources[day]['url']} for day in dict.fromkeys(point['source_days'])]
    parts.append('<li>'+escape(point['text'])+' '+ ' · '.join('<a href='+quoteattr(l['url'])+'>'+escape(l['title'])+'</a>' for l in links)+'</li>')
   parts.append('</ol></li>')
  parts.append('</ol>')
 return ''.join(parts)

def publish(friday,body,state):
 key=str(friday);rec=state.setdefault('weeks',{}).setdefault(key,{})
 title=f'{friday-timedelta(days=4):%Y.%m.%d}—{friday:%m.%d}｜乔超周报'
 nodes=d.children()
 if not rec.get('id'):
  matches=[n for n in nodes if n['title']==title and n['parentId']==d.ARCHIVE]
  if matches:
   if len(matches)!=1 or not rec.get('operate_code') or not rec.get('pending_hash'):
    raise RuntimeError('存在未登记同名周报，停止创建避免重复或覆盖')
   _,old=d.getdoc(matches[0]['shortcutId'])
   if fingerprint(old)!=rec['pending_hash']:raise RuntimeError('同名周报与待发布内容不一致')
   rec['id']=matches[0]['shortcutId'];save(BASE/'state.json',state)
 if rec.get('id'):
  if not any(n['shortcutId']==rec['id'] and n['parentId']==d.ARCHIVE for n in nodes):raise RuntimeError('周报已移出指定档案，停止更新')
  meta,old=d.getdoc(rec['id']);old_hash=fingerprint(old)
  if old_hash!=fingerprint(body):
   if old_hash not in (rec.get('body_hash'),rec.get('pending_hash')):raise RuntimeError('周报有人手动修改，保留原文并停止自动覆盖')
   rec['pending_hash']=fingerprint(body);save(BASE/'state.json',state)
   d.cli('docs:edit','--shortcut-id',rec['id'],'--hash',meta['hash'],'--format','xml','--content','-',content=body)
 else:
  if not rec.get('operate_code'):
   rec['operate_code']=d.cli('utils:generate-operate-code')['operateCode'];save(BASE/'state.json',state)
  rec['pending_hash']=fingerprint(body);save(BASE/'state.json',state)
  # A retry reuses the same persisted operate code, even if the response was lost.
  result=d.cli('docs:create','--parent-id',d.ARCHIVE,'--title',title,'--format','xml','--content','-','--operate-code',rec['operate_code'],content=body)
  rec['id']=result['shortcutId'];save(BASE/'state.json',state)
 d.verified(rec['id'],body)
 if not any(n['shortcutId']==rec['id'] and n['parentId']==d.ARCHIVE for n in d.children()):raise RuntimeError('周报创建后目录核验失败')
 rec['body_hash']=fingerprint(body);rec.pop('pending_hash',None);save(BASE/'state.json',state)
 return 'https://docs.xiaohongshu.com/doc/'+rec['id']

def main():
 ap=argparse.ArgumentParser();mode=ap.add_mutually_exclusive_group(required=True);mode.add_argument('--scheduled',action='store_true');mode.add_argument('--initial',action='store_true');args=ap.parse_args()
 now=datetime.now(TZ)
 friday=friday_of(now)
 if args.initial and now.weekday()!=4:raise RuntimeError('初版仅用于本次周五提前生成')
 BASE.mkdir(exist_ok=True)
 with (BASE/'run.lock').open('w') as lock:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return
  sessions.cleanup_pending(BASE/'logs')
  if args.scheduled and not eligible(now):return
  state=read(BASE/'state.json');rec=state.setdefault('weeks',{}).setdefault(str(friday),{})
  if rec.get('complete') or args.initial and rec.get('initial_complete'):return
  bundle,reason=collect(friday,args.initial)
  if bundle is None:
   rec['waiting']=reason;save(BASE/'state.json',state);print(reason);return
  rec.pop('waiting',None)
  if rec.get('attempts',0)>=3 and not args.initial:return
  run=BASE/'logs'/now.strftime('%Y%m%d-%H%M%S-%f');run.mkdir(parents=True)
  save(run/'sources.json',bundle)
  if not args.initial:rec['attempts']=rec.get('attempts',0)+1
  save(BASE/'state.json',state)
  try:
   cfg=read(ROOT/'.codex/hi-bridge/config.json')
   schema={'sections':[{'heading':'模块标题','projects':[{'name':'项目名','points':[{'text':'事实与判断','source_days':['YYYY-MM-DD'],'links':[{'title':'产物','url':'来源链接'}]}]}]}]}
   instructions=(BASE/'prompt.md').read_text()
   style=ROOT/'writing-style.md'
   if style.is_file():instructions+='\n\n项目写作约定：\n'+style.read_text()
   report=sessions.analyze(run,ROOT,cfg['model'],instructions,schema,bundle)
   validate(report,bundle);save(run/'report.json',report);body=render(report,bundle);(run/'report.xml').write_text(body)
   url=publish(friday,body,state)
   rec.update(url=url,initial_complete=True,complete=not args.initial,updated_at=now.isoformat(),source_hashes={s['day']:s['hash'] for s in bundle['sources']})
   save(BASE/'state.json',state);save(run/'receipt.json',{'url':url,'friday':str(friday),'complete':True,'initial':args.initial});print(url,flush=True)
  except Exception as exc:
   save(run/'error.json',{'error':str(exc)});raise
  finally:
   try:sessions.cleanup(run)
   except Exception as exc:
    save(run/'cleanup-error.json',{'error':str(exc)});raise
if __name__=='__main__':main()
