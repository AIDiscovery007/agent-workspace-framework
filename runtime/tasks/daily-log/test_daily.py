import importlib.util
import json
import os
import sqlite3
from pathlib import Path
from datetime import datetime
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('daily',Path(__file__).with_name('run.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Tests(unittest.TestCase):
 def test_public_digest_excludes_member_messages_and_sessions(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp).resolve()/'work';home=Path(tmp)/'home';base=root/'.codex/tasks/daily-log'
   base.mkdir(parents=True);dbpath=root/'.codex/hi-bridge/state.sqlite3';dbpath.parent.mkdir()
   start=datetime(2026,11,9,0,tzinfo=m.TZ);end=datetime(2026,11,10,0,tzinfo=m.TZ)
   with sqlite3.connect(dbpath) as db:
    db.execute('create table inbox (id text,prompt text,status text,result text,created real,sender text)')
    db.executemany('insert into inbox values (?,?,?,?,?,?)',[
     ('owner','owner task','complete','owner result',start.timestamp()+1,'owner@example.test'),
     ('member','private task','complete','private result',start.timestamp()+1,'member@example.test')])
   sessions=home/'.codex/sessions';sessions.mkdir(parents=True)
   for ident,cwd in [('owner-session',root/'project'),('member-session',root/'成员工作区/member')]:
    records=[{'payload':{'id':ident,'cwd':str(cwd),'source':'cli'}},
     {'type':'event_msg','timestamp':start.isoformat(),'payload':{'type':'task_complete','last_agent_message':ident}}]
    path=sessions/(ident+'.jsonl');path.write_text('\n'.join(json.dumps(r) for r in records))
    os.utime(path,(start.timestamp()+1,start.timestamp()+1))
   with patch.object(m,'ROOT',root),patch.object(m,'BASE',base),patch.dict(m.SETTINGS,{'owner':'owner@example.test'}),patch.object(Path,'home',return_value=home):
    events=m.collect(start,end)
   self.assertEqual(len(events),2)
   self.assertTrue(any(e['id']=='hi:owner' for e in events))
   self.assertTrue(any(e['id'].startswith('session:owner-session:') for e in events))
   self.assertNotIn('private',json.dumps(events));self.assertNotIn('member-session',json.dumps(events))
 def test_weekday_time_and_duplicate_guard(self):
  self.assertFalse(m.due(datetime(2026,11,9,19,59,tzinfo=m.TZ),{}))
  self.assertTrue(m.due(datetime(2026,11,9,20,0,tzinfo=m.TZ),{}))
  self.assertFalse(m.due(datetime(2026,11,14,20,0,tzinfo=m.TZ),{}))
  self.assertFalse(m.due(datetime(2026,11,9,20,0,tzinfo=m.TZ),{'completed_date':'2026-11-09'}))
 def test_secret_links_removed(self):
  for url in ['https://example.com/webhook/send?key=SECRET','https://example.com/a?token=SECRET','https://example.com/a?signature=SECRET']:
   self.assertNotIn('SECRET',m.redact(url))
 def test_empty_evidence_also_uses_fresh_agent(self):
  now=datetime(2026,11,9,20,tzinfo=m.TZ)
  with patch.object(m.session_runner,'analyze',return_value={'summary':'无新增关键事项','items':[]}) as agent:
   self.assertEqual(m.analyze([],Path('/unused'),now,now)['items'],[])
   self.assertEqual(agent.call_args.args[-1]['evidence'],[])
 def test_more_than_six_key_items_are_accepted(self):
  events=[{'id':str(i)} for i in range(12)]
  items=[{'type':'交付','action':f'交付独立事项{i}','result':'已核验','evidence_ids':[str(i)],'links':[]} for i in range(12)]
  m.validate({'summary':'完成12项独立事项','items':items},events)
 def test_unfounded_links_and_evidence_rejected(self):
  evidence=[{'id':'valid','result':'完成，无链接'}]
  item={'type':'配置','action':'完成配置','result':'已经验证','evidence_ids':['valid'],'links':[]}
  m.validate({'summary':'已配置','items':[item]},evidence)
  item['evidence_ids']=['invented']
  with self.assertRaises(ValueError):m.validate({'summary':'已配置','items':[item]},evidence)
  item['evidence_ids']=['valid'];item['links']=[{'title':'虚构','url':'https://example.com/fake'}]
  with self.assertRaises(ValueError):m.validate({'summary':'已配置','items':[item]},evidence)
 def test_same_day_document_reused(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(m,'BASE',Path(tmp)),patch.object(m,'children',return_value=[{'title':'2026-11-09｜工作日志','shortcutId':'existing','parentId':m.ARCHIVE}]),patch.object(m,'getdoc',return_value=({'hash':'h'},'<p>旧内容</p>')),patch.object(m,'verified'),patch.object(m,'cli',return_value={}) as cli:
   state={};m.ensure_doc('daily:2026-11-09','2026-11-09｜工作日志','<p>新内容</p>',state)
   self.assertEqual(state['documents']['daily:2026-11-09']['id'],'existing')
   self.assertEqual(cli.call_args.args[0],'docs:edit')
 def test_outside_archive_refused(self):
  with patch.object(m,'children',return_value=[]),patch.object(m,'cli') as cli:
   with self.assertRaises(RuntimeError):m.ensure_doc('daily:d','d','<p>x</p>',{'documents':{'daily:d':{'id':'outside'}}})
   cli.assert_not_called()
 def test_publish_no_empty_caller(self):
  m.validate({'summary':'无新增','items':[]},[])
 def test_explicit_opt_out(self):
  self.assertTrue(m.opted_out('这个内容不要归档'))
  self.assertTrue(m.opted_out('仅回复，不记录'))
  self.assertFalse(m.opted_out('普通任务，只读检查数据'))
 def test_rollup_preserves_existing_root_content(self):
  now=datetime(2026,11,9,20,tzinfo=m.TZ)
  state={'entries':{f'2026-10-{i:02d}':{'url':f'https://docs.xiaohongshu.com/doc/d{i}','count':1} for i in range(1,22)}}
  root='<title>档案</title><p>用户核心入口</p><p>2026-10-01｜日报｜1 项关键事项｜<a href="https://docs.xiaohongshu.com/doc/d1">详情</a></p>'
  digest={'summary':'有进展','items':[{'type':'配置','action':'启用日汇总','result':'已核验','evidence_ids':['e'],'links':[]}]}
  with tempfile.TemporaryDirectory() as tmp,patch.object(m,'BASE',Path(tmp)),patch.object(m,'ensure_doc',side_effect=lambda key,*args:'https://docs.xiaohongshu.com/doc/'+key.replace(':','-')) as ensure,patch.object(m,'getdoc',return_value=({'hash':'v'},root)),patch.object(m,'cli') as cli,patch.object(m,'verified'):
   m.publish(digest,'2026-11-09',now,now,state)
   xml=cli.call_args.kwargs['content']
   self.assertIn('用户核心入口',xml)
   self.assertEqual(xml.count('｜日报｜'),20)
   self.assertIn('｜月索引｜',xml)
   self.assertNotIn('href="https://docs.xiaohongshu.com/doc/d1"',xml)
   self.assertEqual(ensure.call_count,2)
if __name__=='__main__':unittest.main()
