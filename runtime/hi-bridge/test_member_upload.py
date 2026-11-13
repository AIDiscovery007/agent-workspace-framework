import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import member_upload as m

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.own=self.root/'own';self.own.mkdir()
        self.config={'workspace':str(self.root),'member_workspaces':{m.SENDER:'own'},'member_upload_folders':{m.SENDER:'test/member'}}
        self.b=m.Broker(self.config,m.SENDER,self.own,self.root/'records');self.calls=[]
        def cli(*args):
            self.calls.append(args)
            if args[0]=='utils:generate-operate-code':return {'operateCode':'test-op'}
            if args[0]=='pan:write':return {'entryId':'file1','shareUrl':'https://example.test/share','fileName':'result.xlsx'}
            return {'entryId':'file1','fileName':'result.xlsx','fileSize':3,'status':'ACTIVE'}
        mock=patch.object(m,'cli',side_effect=cli);mock.start();self.addCleanup(mock.stop)
    def write(self,path,folder='test/member'):
        return self.b.dispatch(['pan:write','--file-path',str(path),'--folder-path',folder,'--operate-code','test-op'])
    def test_upload_verified_and_retry_reuses_receipt(self):
        p=self.own/'result.xlsx';p.write_bytes(b'abc');self.b.dispatch(['utils:generate-operate-code'])
        self.assertEqual(self.write(p)['entryId'],'file1');self.write(p)
        self.assertEqual(sum(c[0]=='pan:write' for c in self.calls),1)
        self.assertTrue(json.loads(self.b.index.read_text())['test-op']['verified'])
    def test_paths_and_cloud_scope_fail_closed(self):
        p=self.root/'outside';p.write_text('abc');link=self.own/'link.xlsx';link.symlink_to(p)
        hidden=self.own/'.state';hidden.write_text('abc');self.b.dispatch(['utils:generate-operate-code'])
        for path in (p,link,hidden,self.own/'../outside'):
            with self.assertRaises((ValueError,OSError)):self.write(path)
        for args in [['pan:search','--query','x'],['pan:readmeta','--entry-id','other'],['pan:config','--set-default-folder','x']]:
            with self.assertRaises(ValueError):self.b.dispatch(args)
        self.assertFalse(any(c[0]=='pan:write' for c in self.calls))
    def test_other_member_not_enabled_and_folder_cannot_change(self):
        self.assertFalse(m.enabled(self.config,'unapproved@example.invalid'))
        p=self.own/'result.xlsx';p.write_text('abc');self.b.dispatch(['utils:generate-operate-code'])
        with self.assertRaises(ValueError):self.write(p,'other')
    def test_reused_code_cannot_change_file(self):
        p=self.own/'result.xlsx';p.write_text('abc');self.b.dispatch(['utils:generate-operate-code']);self.write(p)
        p.write_text('changed')
        with self.assertRaises(ValueError):self.write(p)
        self.assertEqual((self.b.storage/'test-op/result.xlsx').read_bytes(),b'abc')

    def test_account_binding_required(self):
        sender='member-b@example.invalid'
        self.config['member_upload_folders'][sender]='test/chen'
        self.assertFalse(m.enabled(self.config,sender))
        self.config['member_workspaces'][sender]='chen'
        with self.assertRaises(ValueError):m.Broker(self.config,sender,self.own)

    def test_two_accounts_keep_receipts_and_files_isolated(self):
        sender='member-b@example.invalid';other=self.root/'chen';other.mkdir()
        self.config['member_workspaces'][sender]='chen'
        self.config['member_upload_folders'][sender]='test/chen'
        with patch.object(m,'BASE',self.root/'bridge'):
            first=m.Broker(self.config,m.SENDER,self.own)
            second=m.Broker(self.config,sender,other)
            self.assertEqual(first.storage,m.storage_for(m.SENDER))
            self.assertNotEqual(first.storage,second.storage)
            (self.own/'result.xlsx').write_bytes(b'abc')
            first.dispatch(['utils:generate-operate-code'])
            first.dispatch(['pan:write','--file-path',str(self.own/'result.xlsx'),'--folder-path','test/member','--operate-code','test-op'])
            second.dispatch(['utils:generate-operate-code'])
            self.assertNotIn('receipt',second.records['test-op'])
            with self.assertRaises(ValueError):second.dispatch(['pan:readmeta','--entry-id','file1'])
            with self.assertRaises(ValueError):second.dispatch(['pan:write','--file-path',str(self.own/'result.xlsx'),'--folder-path','test/chen','--operate-code','test-op'])
            self.assertTrue(m.Broker(self.config,m.SENDER,self.own).records['test-op']['verified'])
