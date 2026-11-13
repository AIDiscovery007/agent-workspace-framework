import json
from pathlib import Path
import queue
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import bridge as b
from test_bridge import mentioned

NEW = 'CHAT-test-second'


class Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        (self.base/'baseline.json').write_text('{"seq":"0"}')
        (self.base/'logs').mkdir()
        for change in (patch.object(b,'BASE',self.base), patch.object(b,'DB',self.base/'state.sqlite3'),
                       patch.dict(b.CONFIG,{'additional_chats':{NEW:{'since_seq':'1'}},'reply_channel':'app','sender_policy':'group_members'})):
            change.start(); self.addCleanup(change.stop)
        b.initialize()

    def item(self, **changes):
        values = {'id':'second-task','seq':'2','chatId':NEW}
        values.update(changes)
        return mentioned('检查文件', **values)

    def test_new_group_uses_own_baseline_and_cursor(self):
        b.set_meta('cursor','500')
        self.assertEqual(b.command_of(self.item()), '检查文件')
        self.assertIsNone(b.command_of(self.item(seq='1')))
        with patch.object(b,'read_messages',side_effect=[[self.item()],[]]) as read:
            b.poll(NEW)
        self.assertEqual(read.call_args_list[0].kwargs['chat_id'], NEW)
        self.assertEqual(b.get_meta('cursor'), '500')
        self.assertEqual(b.get_meta(b.chat_key('cursor',NEW)), '2')
        with b.connect() as db:
            self.assertEqual(db.execute('select chat_id from inbox').fetchone()[0], NEW)

    def test_websocket_and_poll_deduplicate_with_correct_chat(self):
        msg = self.item()
        event = {'type':'EVENT','eventId':'second-event','eventType':'asn_bot_callback_msg',
                 'payload':{'imChat':{'chatId':NEW},'imMessage':msg}}
        self.assertEqual(b.receive_event(event)['type'], 'ACK_EVENT')
        with patch.object(b,'read_message',return_value=msg) as read: b.resolve_event()
        read.assert_called_once_with(msg['id'],chat_id=NEW)
        with patch.object(b,'read_messages',side_effect=[[msg],[]]): b.poll(NEW)
        with b.connect() as db:
            row=db.execute('select * from inbox').fetchone()
            self.assertEqual(row['source'],'websocket'); self.assertEqual(row['chat_id'],NEW)
            self.assertEqual(db.execute('select count(*) from inbox').fetchone()[0],1)

    def test_unknown_or_conflicting_chat_is_not_accepted(self):
        self.assertIsNone(b.command_of(self.item(chatId='unconfigured')))
        event = {'type':'EVENT','eventId':'wrong-chat','eventType':'asn_bot_callback_msg',
                 'payload':{'imChat':{'chatId':NEW},'imMessage':self.item(chatId=b.CONFIG['chat_id'])}}
        b.receive_event(event)
        with b.connect() as db:
            self.assertEqual(db.execute('select status from ws_events').fetchone()[0],'ignored')
        with self.assertRaises(ValueError): b.session_key(chat_id='unconfigured')

    def test_poll_rejects_wrong_group_without_advancing_cursor(self):
        with patch.object(b,'read_messages',return_value=[self.item(chatId=b.CONFIG['chat_id'])]):
            with self.assertRaises(ValueError):b.poll(NEW)
        self.assertEqual(b.get_meta(b.chat_key('cursor',NEW)),'1')

    def test_same_sender_has_separate_threads_and_original_is_preserved(self):
        owner=b.CONFIG['allowed_sender'];b.set_meta('thread_id','original-thread')
        for chat, thread, resume in ((NEW,'new-group-thread',False),(b.CONFIG['chat_id'],'original-thread',True),(NEW,'new-group-thread',True)):
            rpc=b.AppServer.__new__(b.AppServer)
            rpc.pending=[{'method':'turn/completed','params':{'threadId':thread,'turn':{'id':'turn','status':'completed'}}}]
            with patch.object(rpc,'call',side_effect=[{'thread':{'id':thread}},{'turn':{'id':'turn'}}]) as call:
                rpc.run('只读检查',sender=owner,chat_id=chat)
            first=call.call_args_list[0]
            self.assertEqual(first.args[0],'thread/resume' if resume else 'thread/start')
            if resume:self.assertEqual(first.args[1]['threadId'],thread)
            self.assertIn(chat,first.args[1]['developerInstructions'])
        self.assertEqual(b.get_meta('thread_id'),'original-thread')
        self.assertEqual(b.get_meta(b.session_key(owner,NEW)),'new-group-thread')

    def test_reference_cannot_cross_groups_even_when_both_allowed(self):
        item=self.item(refId='referenced')
        reference=self.item(id='referenced',chatId=b.CONFIG['chat_id'])
        with patch.object(b,'read_message',side_effect=[item,reference]) as read, \
             patch.object(b,'require_current_member'),patch.object(b,'download_files') as download:
            with self.assertRaises(b.AttachmentError):b.prepare_input(item['id'],'检查文件',self.base,chat_id=NEW)
        self.assertEqual([call.kwargs['chat_id'] for call in read.call_args_list],[NEW,NEW])
        download.assert_not_called()

    def test_membership_is_checked_in_request_group(self):
        response=subprocess.CompletedProcess([],0,json.dumps({'success':True,'memberChatIdList':[NEW]}),'')
        with patch.object(b.subprocess,'run',return_value=response) as run:
            b.require_current_member(b.CONFIG['allowed_sender'],chat_id=NEW)
        self.assertEqual(run.call_args.args[0][-1],NEW)

    def test_worker_routes_model_and_all_replies_to_source_group(self):
        item=self.item()
        with b.connect() as db:b.enqueue_message(db,item,'poll')
        rpc=Mock();rpc.run.return_value='已检查'
        with patch.object(b.STOP,'is_set',side_effect=[False,True]), \
             patch.object(b,'read_message',return_value=item),patch.object(b,'require_current_member'), \
             patch.object(b,'prepare_input',return_value='检查文件') as prepare,patch.object(b,'AppServer',return_value=rpc):
            b.worker()
        rpc.run.assert_called_once_with('检查文件',sender=b.CONFIG['allowed_sender'],chat_id=NEW)
        self.assertEqual(prepare.call_args.kwargs['chat_id'],NEW)
        with b.connect() as db:
            rows=db.execute('select chat_id,mention_contact_id from outbox order by id').fetchall()
            self.assertEqual([r[0] for r in rows],[NEW,NEW])
            self.assertEqual([r[1] for r in rows],[None,b.CONFIG['allowed_sender']])

    def test_retry_and_restart_preserve_reply_destination(self):
        b.say('第二群结果',status='info',chat_id=NEW,mention_contact_id=b.CONFIG['allowed_sender'])
        with b.connect() as db:op=db.execute('select operate_code from outbox').fetchone()[0]
        with patch.object(b.APP_SENDER,'send',side_effect=RuntimeError('temporary')) as send:
            with self.assertRaises(RuntimeError):b.send_one()
            self.assertEqual(send.call_args.args[0],NEW)
        b.initialize()
        with b.connect() as db:db.execute('update outbox set next_attempt=0')
        with patch.object(b.APP_SENDER,'send',return_value={'id':'reply','creatorContactId':b.CONFIG['bot_contact_id']}) as send:
            b.send_one()
            self.assertEqual(send.call_args.args[0],NEW);self.assertEqual(send.call_args.args[2],op)
        with patch.dict(b.CONFIG,{'reply_channel':'webhook'}):
            with self.assertRaises(ValueError):b.say('不能误发原群',chat_id=NEW)

    def test_interrupted_task_notice_stays_in_its_group(self):
        with b.connect() as db:
            b.enqueue_message(db,self.item(),'poll')
            db.execute("update inbox set status='running'")
        b.initialize()
        with b.connect() as db:
            self.assertEqual(db.execute('select chat_id from outbox').fetchone()[0],NEW)
            self.assertEqual(db.execute('select status from inbox').fetchone()[0],'interrupted')

    def test_approval_notice_stays_in_request_group(self):
        rpc=b.AppServer.__new__(b.AppServer);rpc.requester=b.CONFIG['allowed_sender'];rpc.request_chat_id=NEW
        rpc.events=queue.Queue();rpc.events.put({'id':1,'method':'item/commandExecution/requestApproval'})
        with patch.object(rpc,'send') as send:rpc.take(1)
        self.assertEqual(send.call_args.args[0]['result']['decision'],'decline')
        with b.connect() as db:self.assertEqual(db.execute('select chat_id from outbox').fetchone()[0],NEW)

    def test_one_failed_poll_does_not_skip_other_group(self):
        with patch.object(b.STOP,'is_set',side_effect=[False,False,False,True]), \
             patch.object(b.STOP,'wait'),patch.object(b,'poll',side_effect=[RuntimeError('first group down'),None]) as poll, \
             patch.object(b,'say') as say:
            b.polling_loop()
        self.assertEqual([c.args[0] for c in poll.call_args_list],[b.CONFIG['chat_id'],NEW])
        say.assert_not_called()
        self.assertEqual(json.loads(b.get_meta('poll_health'))['failures'],1)


if __name__=='__main__':unittest.main()
