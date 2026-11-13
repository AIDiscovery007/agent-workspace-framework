import tempfile
from pathlib import Path
import unittest
import queue
from unittest.mock import Mock, patch
import bridge as b
import notebook_maintenance as m

class Tests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.base=Path(tmp.name)
        (self.base/'baseline.json').write_text('{"seq":"0"}');(self.base/'logs').mkdir()
        for p in (patch.object(b,'BASE',self.base),patch.object(b,'DB',self.base/'state.sqlite3'),patch.dict(b.CONFIG,{'additional_chats':{}})):
            p.start();self.addCleanup(p.stop)
        b.initialize();b.set_meta('thread_id','original-session')
        with b.connect() as c:
            c.execute('INSERT INTO notebook_maintenance VALUES (?,?,?,?,?,?,NULL)',('scan',b.CONFIG['chat_id'],b.CONFIG['allowed_sender'],'original-session','queued',1))

    def test_same_session_no_group_output(self):
        rpc=Mock();rpc.run.return_value='无新增'
        with patch.object(b,'AppServer',return_value=rpc),patch.object(b,'require_current_member') as member:
            self.assertTrue(m.step(b))
        self.assertEqual(rpc.run.call_args.kwargs['sender'],b.CONFIG['allowed_sender'])
        member.assert_called_once()
        with b.connect() as c:
            self.assertEqual(c.execute('SELECT status FROM notebook_maintenance').fetchone()[0],'complete')
            self.assertEqual(c.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
        self.assertFalse(m.step(b))

    def test_binding_changed_never_runs(self):
        b.set_meta('thread_id','changed')
        with patch.object(b,'AppServer') as rpc:self.assertTrue(m.step(b));rpc.assert_not_called()
        with b.connect() as c:self.assertEqual(c.execute('SELECT status FROM notebook_maintenance').fetchone()[0],'failed')

    def test_authorized_local_task_same_binding_and_no_group_reply(self):
        with b.connect() as c:c.execute('DELETE FROM notebook_maintenance')
        result=m.enqueue(b,'local-test','original-session','只读验收任务','所有者本机明确要求')
        self.assertEqual(result['status'],'queued')
        self.assertTrue(m.enqueue(b,'local-test','original-session','只读验收任务','所有者本机明确要求')['duplicate'])
        rpc=Mock();rpc.run.return_value='已核验'
        with patch.object(b,'AppServer',return_value=rpc),patch.object(b,'require_current_member'),patch.object(b,'say') as say:
            self.assertTrue(m.step(b))
            self.assertIn('只读验收任务',rpc.run.call_args.args[0])
            self.assertNotIn('最多回扫5项',rpc.run.call_args.args[0])
            self.assertTrue(rpc.local_only)
            rpc.close.assert_called_once()
            say.assert_not_called()
        with b.connect() as c:
            self.assertEqual(c.execute('SELECT status FROM notebook_maintenance').fetchone()[0],'complete')
            self.assertEqual(c.execute('SELECT count(*) FROM outbox').fetchone()[0],0)

    def test_local_enqueue_rejects_unbound_and_changed_payload(self):
        with self.assertRaises(ValueError):m.enqueue(b,'local-test','unknown','task','authorization')
        with self.assertRaises(ValueError):m.enqueue(b,'../bad','original-session','task','authorization')
        with self.assertRaises(ValueError):m.enqueue(b,'local-test','original-session','task','')
        m.enqueue(b,'local-test','original-session','task','authorization')
        with self.assertRaises(ValueError):m.enqueue(b,'local-test','original-session','changed','authorization')

    def test_restart_preserves_queued_but_not_replay_running(self):
        with b.connect() as c:c.execute("UPDATE notebook_maintenance SET status='running'")
        b.initialize()
        with b.connect() as c:self.assertEqual(c.execute('SELECT status FROM notebook_maintenance').fetchone()[0],'interrupted')

    def test_maintenance_approval_declined_without_group_notification(self):
        rpc=b.AppServer.__new__(b.AppServer);rpc.events=queue.Queue();rpc.local_only=True
        rpc.events.put({'id':8,'method':'item/commandExecution/requestApproval'})
        with patch.object(rpc,'send') as send,patch.object(b,'say') as say:
            rpc.take(1)
            self.assertEqual(send.call_args.args[0]['result'],{'decision':'decline'})
            say.assert_not_called()

if __name__=='__main__':unittest.main()
