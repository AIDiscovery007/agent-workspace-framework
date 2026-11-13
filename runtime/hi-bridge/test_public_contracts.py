"""Public deployment invariants beyond the inherited compatibility fixtures."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import bridge as b
import member_access as access

class PublicContracts(unittest.TestCase):
    def test_unbound_account_denied_even_if_on_old_extra_list(self):
        cfg={'sender_policy':'group_members','require_member_workspace':True,
             'member_workspaces':{'bound@example.test':'成员工作区/bound'},
             'additional_allowed_senders':['unbound@example.test']}
        with patch.dict(b.CONFIG,cfg):
            self.assertFalse(b.sender_candidate('unbound@example.test'))
            self.assertTrue(b.sender_candidate('bound@example.test'))
            self.assertFalse(b.sender_candidate('someone@xiaohongshu.com'))
    def test_readonly_probe_enforces_both_thread_and_turn(self):
        rpc=b.AppServer.__new__(b.AppServer);rpc.isolated_sender=None
        rpc.pending=[{'method':'item/completed','params':{'threadId':'probe','item':{'id':'a','type':'agentMessage','text':'ok'}}},
                     {'method':'turn/completed','params':{'threadId':'probe','turn':{'id':'turn','status':'completed'}}}]
        with patch.object(b,'get_meta',return_value=None),patch.object(b,'set_meta'), \
             patch.object(b,'checked_chat',return_value=b.CONFIG['chat_id']), \
             patch.object(rpc,'call',side_effect=[{'thread':{'id':'probe'}},{'turn':{'id':'turn'}}]) as call:
            self.assertEqual(rpc.run('readonly test',read_only=True),'ok')
        self.assertEqual(call.call_args_list[0].args[1]['sandbox'],'read-only')
        self.assertEqual(call.call_args_list[1].args[1]['sandboxPolicy']['type'],'readOnly')
        self.assertEqual(call.call_args_list[1].args[1]['approvalPolicy'],'never')
    def test_bound_workspace_symlink_escape_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'成员工作区').mkdir();(root/'outside').mkdir()
            (root/'成员工作区/member').symlink_to(root/'outside',target_is_directory=True)
            cfg={'workspace':str(root),'allowed_sender':'owner@example.test','member_workspaces':{'m@example.test':'成员工作区/member'}}
            with self.assertRaises(ValueError):access.workspace(cfg,'m@example.test')

if __name__=='__main__':unittest.main()
