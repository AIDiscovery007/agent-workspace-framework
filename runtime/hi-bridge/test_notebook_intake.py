import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import notebook_intake as m

class Tests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
        self.root=Path(t.name).resolve();self.own=self.root/'成员工作区/member';self.box=self.own/'.notebook-outbox';self.box.mkdir(parents=True)
        code=self.root/'.codex/gravekeeper';code.mkdir(parents=True)
        shutil.copy2(Path(__file__).resolve().parents[1]/'gravekeeper/notebook.py',code/'notebook.py')
        self.data=dict(title='登录识别失败',trigger='已授权任务',expected='识别本人登录信息',actual='返回识别失败',attempts='未绕过权限',conclusion='根因待定位',impact='任务无法完成',evidence=['本任务返回识别失败'],scope=dict(domain='bridge',component='成员身份识别',observed_evidence='任务返回失败',reusable_reason='同类成员任务'))
    def test_report_submission_receipt_and_dedup(self):
        p=self.box/'event.json';p.write_text(json.dumps(self.data))
        result=m.collect(self.root,self.own,'task','session')
        self.assertTrue(result[0]['accepted']);self.assertTrue((self.box/'event.json.receipt').exists())
        self.assertEqual(m.collect(self.root,self.own,'task2','session'),[])
        (self.box/'event.json.receipt').unlink()
        self.assertTrue(m.collect(self.root,self.own,'task','session')[0]['duplicate'])
        c=sqlite3.connect(self.root/'错题本/.state/state.sqlite3')
        try:
            data=json.loads(c.execute('select payload from incidents').fetchone()[0]);self.assertEqual(data['session'],'session');self.assertEqual(c.execute('select admission from incidents').fetchone()[0],'candidate')
        finally:c.close()
    def test_reject_secret_and_symlink_without_reading_target(self):
        secret=self.root/'private';secret.write_text('private')
        (self.box/'link.json').symlink_to(secret)
        with patch.object(m.subprocess,'run') as run:
            self.assertFalse(m.collect(self.root,self.own,'task','session')[0]['accepted']);run.assert_not_called()
        (self.box/'secret.json').write_text(json.dumps(self.data|{'evidence':['https://example.test/?token=abcdefghijk123']}))
        self.assertFalse(m.collect(self.root,self.own,'task','session')[0]['accepted'])
        self.assertEqual(secret.read_text(),'private')
    def test_reject_linked_outbox_and_keep_business_files_untouched(self):
        self.box.rmdir();self.box.symlink_to(self.root)
        with self.assertRaises(OSError):m.collect(self.root,self.own,'task','session')

if __name__=='__main__':unittest.main()
