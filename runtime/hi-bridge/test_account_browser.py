import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import bridge as b
import member_browser as browser
import member_access as access


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.owner = 'owner@xiaohongshu.com'
        self.member = browser.LEGACY_SENDER
        self.peer = 'peer@xiaohongshu.net'
        self.config = {'sender_policy':'group_members', 'browser_domain': 'evaluation.example.test',
            'workspace': str(self.root), 'allowed_sender': self.owner,
            'member_workspaces': {self.member: '成员工作区/示例成员', self.peer: '成员工作区/对照'},
            'member_browser': {self.owner: True, self.member: True},
            'browser_platform_usernames': {self.owner: '示例用户'},
        }
        for entry in self.config['member_workspaces'].values():
            (self.root/entry).mkdir(parents=True)
        self.own = self.root/self.config['member_workspaces'][self.member]
        p = patch.object(browser, 'BASE', self.root/'trusted-bridge')
        p.start(); self.addCleanup(p.stop)

    def test_browser_follows_exact_account_across_groups(self):
        for sender, own in ((self.owner, self.root), (self.member, self.own)):
            envs = [browser.environment({**self.config, 'chat_id': chat}, sender, own)
                    for chat in ('group-one', 'group-two')]
            self.assertEqual(envs[0], envs[1])
            with patch.dict(b.CONFIG, self.config), patch.object(b, 'checked_chat', side_effect=lambda x:x):
                self.assertNotEqual(b.session_key(sender, 'group-one'), b.session_key(sender, 'group-two'))
        owner_env = browser.environment(self.config, self.owner, self.root)
        member_env = browser.environment(self.config, self.member, self.own)
        for key in ('AGENT_BROWSER_SESSION', 'AGENT_BROWSER_PROFILE', 'AGENT_BROWSER_SOCKET_DIR', 'AGENT_BROWSER_CONFIG'):
            self.assertNotEqual(owner_env[key], member_env[key])

    def test_account_browser_uses_hashed_storage(self):
        env = browser.environment(self.config, self.member, self.own)
        self.assertEqual(env['AGENT_BROWSER_SESSION'], browser.session(self.config,self.member))
        self.assertEqual(env['AGENT_BROWSER_PROFILE'], str(browser.runtime_dir(self.config,self.member,self.own)/'profile'))
        self.assertEqual(env['AGENT_BROWSER_SOCKET_DIR'], str(browser.runtime_dir(self.config,self.member,self.own)/'sockets'))
        self.assertEqual(env['AGENT_BROWSER_CONFIG'], str(browser.policy_dir(self.config,self.member)/'config.json'))

    def test_unconfigured_and_unbound_accounts_cannot_gain_browser(self):
        for sender in (self.peer, 'unknown@xiaohongshu.com', None):
            self.assertFalse(browser.enabled(self.config, sender))
        bad = {**self.config, 'member_browser': {'unknown@xiaohongshu.com': True}}
        self.assertFalse(browser.enabled(bad, 'unknown@xiaohongshu.com'))
        with self.assertRaises(ValueError):
            browser.environment(self.config, self.peer, self.root)
        with patch.object(browser.subprocess, 'run') as run:
            browser.ensure(self.config, self.peer, self.root)
            run.assert_not_called()

    def test_workspace_mismatch_and_symlink_refused(self):
        with self.assertRaises(ValueError):
            browser.environment(self.config, self.member, self.root)
        runtime = browser.runtime_dir(self.config, self.owner, self.root)
        runtime.parent.mkdir(); runtime.symlink_to(self.own, target_is_directory=True)
        with patch.object(browser, 'CLI', Path('/bin/echo')), patch.object(browser, 'ENGINE_DIR', self.root), \
             patch.object(browser.subprocess, 'run') as run:
            (self.root/'chrome-headless-shell').touch()
            with self.assertRaisesRegex(RuntimeError, '符号链接'):
                browser.ensure(self.config, self.owner, self.root)
            run.assert_not_called()

    def test_bootstrap_uses_account_paths_and_sandbox(self):
        (self.root/'chrome-headless-shell').touch()
        with patch.object(browser, 'CLI', Path('/bin/echo')), patch.object(browser, 'ENGINE_DIR', self.root), \
             patch.dict(os.environ, {'AGENT_BROWSER_CDP': 'untrusted-endpoint'}), \
             patch.object(browser.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            browser.ensure(self.config, self.owner, self.root)
        args, kwargs = run.call_args
        self.assertEqual(args[0][:2], ['/usr/bin/sandbox-exec', '-f'])
        self.assertNotIn('AGENT_BROWSER_CDP', kwargs['env'])
        self.assertEqual(kwargs['env']['AGENT_BROWSER_AUTO_CONNECT'], 'false')
        self.assertEqual(kwargs['env']['AGENT_BROWSER_ALLOWED_DOMAINS'], browser.domain(self.config))
        self.assertTrue(Path(kwargs['env']['AGENT_BROWSER_SOCKET_DIR']).is_dir())
        self.assertIn('(deny file-read* file-write*)', Path(args[0][2]).read_text())

    def test_appserver_owner_gets_environment_without_expanding_model_permissions(self):
        proc = Mock(); proc.stdout = iter([])
        with patch.dict(b.CONFIG, self.config), patch.object(browser, 'ensure') as ensure, \
             patch.object(b.subprocess, 'Popen', return_value=proc) as popen, \
             patch.object(b.AppServer, 'call', return_value={}):
            rpc = b.AppServer(self.root/'app.log', sender=self.owner)
        rpc.reader_thread.join(); rpc.logfile.close()
        command = popen.call_args.args[0]
        ensure.assert_called_once_with(b.CONFIG, self.owner, self.root)
        self.assertIn('shell_environment_policy.set.AGENT_BROWSER_SESSION=', ' '.join(command))
        self.assertIn(browser.session(self.config, self.owner), ' '.join(command))
        self.assertNotIn('danger-full-access', ' '.join(command))
        self.assertNotIn('approval_policy=never', ' '.join(command))

    def test_new_and_resumed_threads_keep_account_browser_and_permission_policy(self):
        for sender, isolated in ((self.owner, False), (self.member, True)):
            for chat, existing in (('group-one', False), ('group-two', True)):
                rpc = b.AppServer.__new__(b.AppServer)
                rpc.isolated_sender = sender if isolated else None
                rpc.pending = [
                    {'method':'item/completed', 'params':{'threadId':'t', 'item':{'id':'a','type':'agentMessage','text':'ok'}}},
                    {'method':'turn/completed', 'params':{'threadId':'t','turn':{'id':'turn','status':'completed'}}},
                ]
                first = {'thread':{'id':'t'}, 'activePermissionProfile':{'id':access.PROFILE}}
                # Avoid reading real project instructions for a synthetic member workspace.
                (self.root/'AGENTS.md').write_text('test workspace')
                with patch.dict(b.CONFIG, self.config), patch.object(b, 'ROOT', self.root), \
                     patch.object(b, 'checked_chat', return_value=chat), \
                     patch.object(b, 'get_meta', return_value='t' if existing else None), \
                     patch.object(b, 'set_meta'), \
                     patch.object(rpc, 'call', side_effect=[first, {'turn':{'id':'turn'}}]) as call:
                    self.assertEqual(rpc.run('test', sender=sender, chat_id=chat), 'ok')
                start, turn = call.call_args_list
                self.assertEqual(start.args[0], 'thread/resume' if existing else 'thread/start')
                self.assertIn(browser.session(self.config, sender), start.args[1]['developerInstructions'])
                self.assertIn('按真实账号绑定', start.args[1]['developerInstructions'])
                self.assertEqual('"示例用户"' in start.args[1]['developerInstructions'], not isolated)
                expected = 'never' if isolated else 'on-request'
                self.assertEqual(start.args[1]['approvalPolicy'], expected)
                self.assertEqual(turn.args[1]['approvalPolicy'], expected)
                if isolated:
                    self.assertEqual(turn.args[1]['permissions'], access.PROFILE)
                else:
                    self.assertEqual(turn.args[1]['sandboxPolicy']['type'], 'workspaceWrite')


if __name__ == '__main__':
    unittest.main()
