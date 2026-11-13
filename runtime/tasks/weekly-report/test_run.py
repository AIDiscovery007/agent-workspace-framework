import unittest,tempfile,json
from pathlib import Path
from datetime import datetime,date
from unittest.mock import patch
import run as w

class WeeklyTests(unittest.TestCase):
 def test_schedule(self):
  for t,result in [('2026-11-13T20:59:00',False),('2026-11-13T21:00:00',True),('2026-11-14T10:00:00',True),('2026-11-16T21:00:00',False)]:
   self.assertEqual(w.eligible(datetime.fromisoformat(t)),result)
  self.assertEqual(w.friday_of(datetime(2027,1,2)),date(2027,1,1))
 def test_friday_requires_real_receipt(self):
  with tempfile.TemporaryDirectory() as td,patch.object(w,'DAILY',Path(td)):
   p=Path(td)/'logs/a';p.mkdir(parents=True)
   f=p/'receipt.json';state={'entries':{'2026-11-13':{'url':'https://example/friday'}}}
   r={'date':'2026-11-13','complete':True,'early':True,'count':1,'url':'https://example/friday'}
   f.write_text(json.dumps(r));self.assertIsNone(w.friday_receipt(date(2026,11,13),state))
   r.pop('early');f.write_text(json.dumps(r));self.assertIsNotNone(w.friday_receipt(date(2026,11,13),state))
   r['url']='wrong';f.write_text(json.dumps(r));self.assertIsNone(w.friday_receipt(date(2026,11,13),state))
   r.update(count=0,url=None);f.write_text(json.dumps(r));self.assertIsNotNone(w.friday_receipt(date(2026,11,13),{}))
 def test_source_validation(self):
  b={'sources':[{'day':'2026-11-13','url':'https://example/source','xml':'<p>真实来源</p>'}]}
  r={'sections':[{'heading':'本周核心推进事项','projects':[{'name':'项目','points':[{'text':'完成交付','source_days':['2026-11-13'],'links':[]}]}]}]}
  w.validate(r,b)
  point=r['sections'][0]['projects'][0]['points'][0];point['source_days']=['2026-11-12']
  with self.assertRaises(ValueError):w.validate(r,b)
  point['source_days']=['2026-11-13'];point['links']=[{'title':'伪造','url':'https://fake'}]
  with self.assertRaises(ValueError):w.validate(r,b)
 def test_preserve_human_edit(self):
  f=date(2026,11,13);state={'weeks':{str(f):{'id':'owned','body_hash':w.fingerprint('<p>previous</p>')}}}
  with patch.object(w.d,'children',return_value=[{'shortcutId':'owned','parentId':w.d.ARCHIVE}]),patch.object(w.d,'getdoc',return_value=({'hash':'x'},'<p>human edit</p>')),patch.object(w.d,'cli') as cli:
   with self.assertRaisesRegex(RuntimeError,'手动修改'):w.publish(f,'<p>new</p>',state)
   cli.assert_not_called()
 def test_cloud_delivery_links_use_stable_daily_reference(self):
  b={'monday':'2026-11-09','friday':'2026-11-13','initial':True,'no_items':[], 'sources':[{'day':'2026-11-13','url':'https://docs.xiaohongshu.com/doc/source'}]}
  r={'sections':[{'heading':'进展','projects':[{'name':'交付','points':[{'text':'已交付','source_days':['2026-11-13'],'links':[{'title':'数据','url':'https://hi.xiaohongshu.com/clouddrive/dist/v1/f/file'}]}]}]}]}
  xml=w.render(r,b)
  self.assertNotIn('/clouddrive/',xml)
  self.assertIn('https://docs.xiaohongshu.com/doc/source',xml)
 def test_recover_lost_create_response_without_duplicate(self):
  f=date(2026,11,13);body='<p>published</p>';state={'weeks':{str(f):{'operate_code':'persisted','pending_hash':w.fingerprint(body)}}}
  nodes=[{'shortcutId':'recovered','parentId':w.d.ARCHIVE,'title':'2026.11.09—11.13｜乔超周报'}]
  with tempfile.TemporaryDirectory() as td,patch.object(w,'BASE',Path(td)),patch.object(w.d,'children',return_value=nodes),patch.object(w.d,'getdoc',return_value=({'hash':'x'},body)),patch.object(w.d,'verified'),patch.object(w.d,'cli') as cli:
   self.assertTrue(w.publish(f,body,state).endswith('recovered'));cli.assert_not_called()
if __name__=='__main__':unittest.main()
