import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import bridge as b

class Tests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        root=Path(tmp.name);(root/'baseline.json').write_text('{"seq":"0"}')
        for change in (patch.object(b,'BASE',root),patch.object(b,'DB',root/'db'),
            patch.dict(b.CONFIG,{'reply_channel':'app','additional_chats':{'CHAT123':{'since_seq':'0'}}})):
            change.start();self.addCleanup(change.stop)
        b.initialize();self.chat=b.CONFIG['chat_id']
        self.error=b.HiReadError('timeout','9000001',1)

    def notices(self):
        with b.connect() as db:return [dict(r) for r in db.execute('select * from outbox order by id')]

    def failure(self,now):b.record_poll_health(self.chat,self.error,now)
    def success(self,now):b.record_poll_health(self.chat,now=now)

    def test_single_fail_recover_and_repeated_flaps_stay_silent(self):
        for now in range(0,1000,40):self.failure(now);self.success(now+20)
        self.assertEqual(self.notices(),[])

    def test_threshold_requires_duration_and_count(self):
        for t in (0,15,30):self.failure(t)
        self.assertEqual(self.notices(),[])
        self.failure(120);self.failure(140)
        self.assertEqual(len(self.notices()),1)
        self.assertIn('Hi 接口持续超时',self.notices()[0]['content'])
        self.assertEqual(self.notices()[0]['chat_id'],self.chat)
        self.success(160);self.assertEqual(len(self.notices()),1)
        self.failure(180);self.success(200);self.assertEqual(len(self.notices()),1)
        self.success(220);self.assertEqual(len(self.notices()),2)
        self.assertIn('补漏已恢复',self.notices()[1]['content'])

    def test_restart_preserves_incident_and_no_repeated_alert(self):
        for t in (0,60,120):self.failure(t)
        b.initialize();self.failure(200);self.assertEqual(len(self.notices()),1)
        self.success(220);b.initialize();self.success(240)
        self.assertEqual(len(self.notices()),2)

    def test_cooldown_suppresses_second_incident_and_unpaired_recovery(self):
        for t in (0,60,120):self.failure(t)
        self.success(140);self.success(160)
        for t in (180,240,300):self.failure(t)
        self.success(320);self.success(340);self.assertEqual(len(self.notices()),2)
        for t in (600,660,720):self.failure(t)
        self.assertEqual(len(self.notices()),3)

    def test_groups_health_is_independent(self):
        for t in (0,60,120):self.failure(t)
        b.record_poll_health('CHAT123',now=200)
        self.assertEqual(len(self.notices()),1)
        self.assertTrue(json.loads(b.get_meta('poll_health'))['alerted'])
        self.assertFalse(json.loads(b.get_meta('poll_health:CHAT123'))['alerted'])

    def test_service_timeout_retries_identical_page_and_preserves_large_seq(self):
        failed=subprocess.CompletedProcess([],1,'','[9000001] Request is timeout after 3000ms')
        ok=subprocess.CompletedProcess([],0,'{"success":true,"result":[{"seq":9007199254740993}]}','')
        with patch.object(b.subprocess,'run',side_effect=[failed,ok]) as run,patch.object(b.STOP,'wait',return_value=False) as wait:
            rows=b.read_messages(end='9007199254740995')
        self.assertEqual(rows[0]['seq'],'9007199254740993')
        self.assertEqual(run.call_args_list[0],run.call_args_list[1]);wait.assert_called_once_with(2)
        self.assertEqual(self.notices(),[])

    def test_three_failures_bounded_and_no_cursor_change(self):
        failed=subprocess.CompletedProcess([],1,'','[9000001] timeout')
        before=b.get_meta('cursor')
        with patch.object(b.subprocess,'run',return_value=failed) as run,patch.object(b.STOP,'wait',return_value=False) as wait:
            with self.assertRaises(b.HiReadError) as caught:b.poll()
        self.assertEqual(run.call_count,3);self.assertEqual([c.args[0] for c in wait.call_args_list],[2,5])
        self.assertEqual(caught.exception.code,'9000001');self.assertEqual(b.get_meta('cursor'),before)

    def test_process_timeout_retries_and_shutdown_interrupts_backoff(self):
        with patch.object(b.subprocess,'run',side_effect=subprocess.TimeoutExpired('hi',45)),patch.object(b.STOP,'wait',return_value=True):
            with self.assertRaises(b.HiReadError) as caught:b.read_messages()
        self.assertEqual(caught.exception.kind,'stopped')

    def test_auth_failure_not_blindly_retried(self):
        r=subprocess.CompletedProcess([],1,'','unauthorized token expired')
        with patch.object(b.subprocess,'run',return_value=r) as run,patch.object(b.STOP,'wait') as wait:
            with self.assertRaises(b.HiReadError) as caught:b.read_messages()
        self.assertEqual(caught.exception.kind,'authentication');self.assertEqual(run.call_count,1);wait.assert_not_called()

    def test_diagnostics_do_not_include_raw_credentials_or_message_text(self):
        r=subprocess.CompletedProcess([],1,'{"code":9000001,"msg":"timeout","secret":"private-content"}',
            'Bearer secret-token https://private.example/?key=secret')
        error=b.read_error(r,json.loads(r.stdout))
        self.assertEqual(error.kind,'timeout');self.assertEqual(error.code,'9000001')
        for secret in ('private-content','secret-token','private.example','key=secret'):
            self.assertNotIn(secret,str(error))
        self.assertEqual(b.read_error(subprocess.CompletedProcess([],1,'','too many requests'),{}).kind,'rate_limit')

if __name__=='__main__':unittest.main()
