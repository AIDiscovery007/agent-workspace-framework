import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import bridge as b
from test_bridge import mentioned

NEW = 'CHAT9876543210123456789'

class Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name); (self.base/'baseline.json').write_text('{"seq":"0"}')
        (self.base/'logs').mkdir()
        for change in (patch.object(b,'BASE',self.base), patch.object(b,'DB',self.base/'db'),
            patch.dict(b.CONFIG,{'additional_chats':{},'auto_register_enabled':True,
                'auto_register_since_ms':'1000','reply_channel':'app','sender_policy':'group_members'})):
            change.start(); self.addCleanup(change.stop)
        b.initialize()
        self.msg = mentioned('hi',id='enroll-task',chatId=NEW,seq='23',sendTime='2000')

    def event(self, msg=None):
        return {'type':'EVENT','eventId':'register-event','eventType':'asn_bot_callback_msg',
            'payload':{'imChat':{'chatId':NEW,'chatType':'2'},'imMessage':msg or self.msg}}

    def api(self, args, **kwargs):
        if args[1]=='im:group-chat':
            result={'success':True,'groupChat':{'chatId':NEW,'chatType':2,'name':'新群'}}
        else:result={'success':True,'memberChatIdList':[NEW]}
        return subprocess.CompletedProcess(args,0,json.dumps(result),'')

    def enroll(self):
        b.receive_event(self.event())
        with patch.object(b,'read_message',return_value=self.msg),patch.object(b.subprocess,'run',side_effect=self.api):
            b.resolve_event()

    def test_register_persists_floor_and_first_task_and_survives_restart(self):
        b.set_meta('thread_id','old-thread');b.set_meta('cursor','999')
        self.enroll(); b.initialize()
        self.assertIn(NEW,b.chat_ids()); self.assertEqual(b.chat_floor('baseline',NEW),22)
        self.assertEqual(b.get_meta('cursor:'+NEW),'22');self.assertEqual(b.get_meta('cursor'),'999')
        self.assertEqual(b.get_meta('thread_id'),'old-thread')
        with b.connect() as db:
            task=db.execute('select * from inbox').fetchone()
            self.assertEqual(task['id'],self.msg['id']); self.assertEqual(task['chat_id'],NEW)
            self.assertEqual(task['source'],'websocket')
        self.assertIsNone(b.command_of({**self.msg,'id':'older','seq':'22'}))

    def test_duplicate_event_and_poll_create_one_registration_and_task(self):
        self.enroll();self.enroll()
        with patch.object(b,'read_messages',side_effect=[[self.msg],[]]):b.poll(NEW)
        with b.connect() as db:
            self.assertEqual(db.execute('select count(*) from registered_chats').fetchone()[0],1)
            self.assertEqual(db.execute('select count(*) from inbox').fetchone()[0],1)

    def test_only_owner_can_start_registration(self):
        b.receive_event(self.event({**self.msg,'creatorContactId':'colleague@xiaohongshu.com'}))
        with b.connect() as db:self.assertEqual(db.execute('select status from ws_events').fetchone()[0],'ignored')
        with patch.object(b.subprocess,'run') as run:
            self.assertFalse(b.register_from_message({**self.msg,'creatorContactId':'colleague@xiaohongshu.com'}))
            run.assert_not_called()

    def test_invalid_or_old_or_fake_mentions_never_register(self):
        variants=[{'sendTime':'999'},{'sendTime':None},{'seq':'0'},{'updateFlag':'1'},
                  {'recallFlag':'1'},{'category':'2'},{'chatId':'SINGLE123'},
                  {'data':json.dumps({'plainText':{'text':'@小跟班 hi'}})},
                  {'creatorContactId':'stranger@xiaohongshu.com'}]
        with patch.object(b.subprocess,'run') as run:
            for changes in variants:
                with self.subTest(changes=changes):self.assertFalse(b.register_from_message({**self.msg,**changes}))
            run.assert_not_called()

    def test_private_chat_or_envelope_mismatch_rejected(self):
        e=self.event();e['payload']['imChat']['chatType']='1';b.receive_event(e)
        with b.connect() as db:self.assertEqual(db.execute('select status from ws_events').fetchone()[0],'ignored')
        with patch.object(b.subprocess,'run',return_value=subprocess.CompletedProcess([],0,json.dumps({'success':True,'groupChat':{'chatId':NEW,'chatType':1}}),'')):
            self.assertFalse(b.register_from_message(self.msg))

    def test_both_owner_and_bot_must_be_current_members(self):
        for missing in (b.CONFIG['allowed_sender'],b.CONFIG['bot_contact_id']):
            def api(args,**kwargs):
                if '--contact-id' in args and args[args.index('--contact-id')+1]==missing:
                    return subprocess.CompletedProcess(args,0,'{"success":true,"memberChatIdList":[]}','')
                return self.api(args,**kwargs)
            with patch.object(b.subprocess,'run',side_effect=api):
                self.assertFalse(b.register_from_message(self.msg))
        self.assertNotIn(NEW,b.chat_ids())

    def test_canonical_sender_and_edit_rechecked(self):
        b.receive_event(self.event())
        with patch.object(b,'read_message',return_value={**self.msg,'creatorContactId':'other@xiaohongshu.com'}):b.resolve_event()
        self.assertNotIn(NEW,b.chat_ids())
        with patch.object(b.subprocess,'run',side_effect=self.api),patch.object(b,'read_message',return_value={**self.msg,'updateFlag':'1'}):
            self.assertFalse(b.register_from_message(self.msg))

    def test_api_failure_is_durable_and_retryable(self):
        b.receive_event(self.event())
        with patch.object(b,'read_message',return_value=self.msg),patch.object(b.subprocess,'run',side_effect=RuntimeError('offline')):b.resolve_event()
        self.assertNotIn(NEW,b.chat_ids())
        with b.connect() as db:
            row=db.execute('select * from ws_events').fetchone()
            self.assertEqual(row['status'],'pending');self.assertEqual(row['attempts'],1)
            db.execute('update ws_events set next_attempt=0')
        with patch.object(b,'read_message',return_value=self.msg),patch.object(b.subprocess,'run',side_effect=self.api):b.resolve_event()
        self.assertIn(NEW,b.chat_ids())

    def test_crash_after_registration_before_enqueue_recovers_pending_event(self):
        b.receive_event(self.event())
        with patch.object(b,'read_message',return_value=self.msg),patch.object(b.subprocess,'run',side_effect=self.api):
            self.assertTrue(b.register_from_message(self.msg))
        b.initialize()
        with patch.object(b,'read_message',return_value=self.msg):b.resolve_event()
        with b.connect() as db:self.assertEqual(db.execute('select count(*) from inbox').fetchone()[0],1)

    def test_disabled_registration_and_webhook_channel_do_not_enroll(self):
        for settings in ({'auto_register_enabled':False},{'reply_channel':'webhook'}):
            with patch.dict(b.CONFIG,settings):self.assertFalse(b.register_from_message(self.msg))
        b.receive_event(self.event())
        with patch.dict(b.CONFIG,{'auto_register_enabled':False}),patch.object(b,'read_message') as read:
            b.resolve_event();read.assert_not_called()
        self.assertNotIn(NEW,b.chat_ids())

    def test_registered_group_starts_new_thread_and_resumes_after_restart(self):
        self.enroll();b.set_meta('thread_id','original-owner-session')
        for method in ('thread/start','thread/resume'):
            rpc=b.AppServer.__new__(b.AppServer)
            rpc.pending=[{'method':'turn/completed','params':{'threadId':'auto-new-session',
                'turn':{'id':'turn','status':'completed'}}}]
            with patch.object(rpc,'call',side_effect=[{'thread':{'id':'auto-new-session'}},{'turn':{'id':'turn'}}]) as call:
                rpc.run('hi',sender=b.CONFIG['allowed_sender'],chat_id=NEW)
            self.assertEqual(call.call_args_list[0].args[0],method)
            if method=='thread/resume':
                self.assertEqual(call.call_args_list[0].args[1]['threadId'],'auto-new-session')
            self.assertEqual(b.get_meta('thread_id'),'original-owner-session')
            b.initialize()
        self.assertEqual(b.get_meta(b.session_key(b.CONFIG['allowed_sender'],NEW)),'auto-new-session')

    def test_even_empty_first_mention_starts_session_and_announces_ready(self):
        self.msg=mentioned('',id='enroll-task',chatId=NEW,seq='23',sendTime='2000')
        self.enroll();rpc=Mock();rpc.run.return_value='你好'
        with patch.object(b.STOP,'is_set',side_effect=[False,True]),patch.object(b,'read_message',return_value=self.msg), \
             patch.object(b,'require_current_member'),patch.object(b,'AppServer',return_value=rpc):b.worker()
        rpc.run.assert_called_once()
        self.assertEqual(rpc.run.call_args.kwargs['chat_id'],NEW)
        with b.connect() as db:
            rows=db.execute('select * from outbox order by id').fetchall()
            self.assertEqual(len(rows),2)
            self.assertIn('会话已就绪',rows[-1]['content'])
            self.assertEqual(rows[-1]['mention_contact_id'],b.CONFIG['allowed_sender'])
            self.assertTrue(all(row['chat_id']==NEW for row in rows))

if __name__=='__main__':unittest.main()
