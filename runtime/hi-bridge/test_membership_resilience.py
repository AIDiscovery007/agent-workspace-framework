import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import bridge as b


class Tests(unittest.TestCase):
    def setUp(self):
        self.sender = 'membership-test@xiaohongshu.net'
        self.chat = b.CONFIG['chat_id']
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        (self.base/'baseline.json').write_text('{"seq":"0"}')
        (self.base/'logs').mkdir()
        for change in (patch.object(b, 'BASE', self.base),
                       patch.object(b, 'DB', self.base/'state.sqlite3'),
                       patch.dict(b.CONFIG, {'sender_policy':'group_members',
                                            'additional_allowed_senders':[self.sender]}),
                       patch.object(b.STOP, 'wait', return_value=False)):
            change.start(); self.addCleanup(change.stop)
        b.initialize()

    def response(self, code=None, member=True, **extra):
        data = ({'success':True, 'memberChatIdList':[self.chat] if member else []}
                if code is None else {'success':False, 'code':code})
        data.update(extra)
        return subprocess.CompletedProcess([], 0 if code is None else 1, json.dumps(data), '')

    def test_transient_errors_retry_then_succeed(self):
        for code in (1002005, 1002001, 1002006, 1001005, 1001001, 99990000):
            with self.subTest(code=code), patch.object(b.subprocess, 'run',
                    side_effect=[self.response(code), self.response()]) as run:
                b.require_current_member(self.sender, self.chat)
                self.assertEqual(run.call_count, 2)
                self.assertEqual(run.call_args_list[0], run.call_args_list[1])

    def test_python_timeout_retries_and_exhaustion_fails_closed(self):
        with patch.object(b.subprocess, 'run', side_effect=subprocess.TimeoutExpired('secret-command', 45)) as run:
            with self.assertRaisesRegex(b.AttachmentError, '3 次.*未执行'):
                b.require_current_member(self.sender, self.chat)
            self.assertEqual(run.call_count, 3)
            self.assertEqual(b.STOP.wait.call_args_list, [call(2), call(5)])

    def test_real_cli_stderr_error_code_is_classified(self):
        failed = subprocess.CompletedProcess([], 1, '', '错误 [1002005]: 请求超时')
        with patch.object(b.subprocess, 'run', side_effect=[failed, self.response()]) as run:
            b.require_current_member(self.sender, self.chat)
            self.assertEqual(run.call_count, 2)

    def test_nested_error_code_and_error_code_alias(self):
        for data in ({'success':False, 'error':{'code':'1002005'}},
                     {'success':False, 'errorCode':1002005}):
            failed = subprocess.CompletedProcess([], 1, json.dumps(data), '')
            with patch.object(b.subprocess, 'run', side_effect=[failed, self.response()]) as run:
                b.require_current_member(self.sender, self.chat)
                self.assertEqual(run.call_count, 2)

    def test_auth_and_permission_errors_never_retry_even_if_message_mentions_timeout(self):
        for code, expected in ((1001002,'登录'), (99990120,'登录'), (99990121,'登录'),
                               (99990110,'登录'), (99990041,'登录'), (1001003,'权限'),
                               (99990071,'权限'), (99990122,'权限'), (99990131,'权限')):
            with self.subTest(code=code), patch.object(b.subprocess, 'run',
                    return_value=self.response(code, msg='timeout accessToken=secret')) as run:
                with self.assertRaisesRegex(b.AttachmentError, expected):
                    b.require_current_member(self.sender, self.chat)
                self.assertEqual(run.call_count, 1)
        b.STOP.wait.assert_not_called()

    def test_not_member_and_unauthorized_never_retry(self):
        with patch.object(b.subprocess, 'run', return_value=self.response(member=False)) as run:
            with self.assertRaisesRegex(b.AttachmentError, '已不在指定群'):
                b.require_current_member(self.sender, self.chat)
            self.assertEqual(run.call_count, 1)
            run.reset_mock()
            with self.assertRaisesRegex(b.AttachmentError, '未获授权'):
                b.require_current_member('outsider@example.com', self.chat)
            run.assert_not_called()
        b.STOP.wait.assert_not_called()

    def test_malformed_response_and_cli_error_fail_without_retry(self):
        for raw in ('invalid secret', '[]', '{}', '{"success":true,"memberChatIdList":null}',
                    '{"success":true,"memberChatIdList":[123]}'):
            with patch.object(b.subprocess, 'run', return_value=subprocess.CompletedProcess([],0,raw,'')) as run:
                with self.assertRaises(b.AttachmentError): b.require_current_member(self.sender, self.chat)
                self.assertEqual(run.call_count, 1)
        with patch.object(b.subprocess, 'run', side_effect=OSError('private-path secret')) as run:
            with self.assertRaisesRegex(b.AttachmentError, '本机'):
                b.require_current_member(self.sender, self.chat)
            self.assertEqual(run.call_count, 1)
        b.STOP.wait.assert_not_called()

    def test_logs_capture_safe_reason_and_recovery_without_raw_output(self):
        fail = self.response(1002005, msg='https://private?accessToken=TOP_SECRET')
        fail.stderr = 'credential TOP_SECRET'
        with patch.object(b.subprocess, 'run', side_effect=[fail,self.response()]), self.assertLogs(b.log, 'INFO') as logs:
            b.require_current_member(self.sender, self.chat)
        output = '\n'.join(logs.output)
        self.assertIn('kind=timeout', output)
        self.assertIn('code=1002005', output)
        self.assertIn('recovered', output)
        self.assertNotIn('TOP_SECRET', output)
        self.assertNotIn('https://', output)

    def test_shutdown_interrupts_retry(self):
        with patch.object(b.STOP, 'wait', return_value=True), patch.object(b.subprocess, 'run',
                return_value=self.response(1002005)) as run:
            with self.assertRaisesRegex(b.AttachmentError, '停止'):
                b.require_current_member(self.sender, self.chat)
            self.assertEqual(run.call_count, 1)

    def worker_fixture(self):
        from test_bridge import mentioned
        item = mentioned(id='membership-task', chatId=self.chat,
                         creatorContactId=self.sender, creatorName='测试成员')
        with b.connect() as db:
            self.assertTrue(b.enqueue_message(db,item,'poll'))
        return item

    def run_worker(self, responses):
        item = self.worker_fixture()
        rpc = Mock(); rpc.run.return_value = '完成'
        def task_finished():
            with b.connect() as db:
                return db.execute('select status from inbox').fetchone()[0] in {'complete','failed','interrupted'}
        with patch.object(b.STOP, 'is_set', side_effect=task_finished), \
             patch.object(b, 'read_message', return_value=item), \
             patch.object(b, 'prepare_input', return_value='检查文件'), \
             patch.object(b, 'deliver_svg', return_value=None), \
             patch.object(b.subprocess, 'run', side_effect=responses), \
             patch.object(b, 'AppServer', return_value=rpc) as server:
            b.worker()
        with b.connect() as db:
            status = db.execute('select status from inbox').fetchone()[0]
        return rpc, server, status

    def test_worker_temporary_failure_runs_task_exactly_once(self):
        rpc, server, status = self.run_worker([
            self.response(1002005), self.response(), self.response(1002001), self.response()])
        self.assertEqual(status, 'complete')
        server.assert_called_once()
        rpc.run.assert_called_once()

    def test_worker_persistent_failure_never_starts_model(self):
        rpc, server, status = self.run_worker([self.response(1002005)]*3)
        self.assertEqual(status, 'failed')
        server.assert_not_called()
        rpc.run.assert_not_called()

    def test_worker_departure_after_preparation_still_blocks_model(self):
        rpc, server, status = self.run_worker([self.response(),self.response(1002005),self.response(member=False)])
        self.assertEqual(status, 'failed')
        server.assert_not_called()
        rpc.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
