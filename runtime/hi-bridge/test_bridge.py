import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import subprocess
import sys
import threading
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('bridge',Path(__file__).with_name('bridge.py'))
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

def message(seq='9007199254740993', **changes):
    value={'id':'sample','seq':seq,'chatId':m.CONFIG['chat_id'],'creatorContactId':m.CONFIG['allowed_sender'], 'type':'1','category':'1','recallFlag':'0','updateFlag':'0','data':json.dumps({'plainText':{'text':'/codex 测试'}})}
    value.update(changes)
    return value


def mentioned(text='检查文件', contact=None, name='小跟班', **changes):
    data={'richText':{'root':{'children':[{'type':'paragraph','children':[{'id':'mention','type':'mention'},{'id':'text','type':'text'}]}]},
        'elements':[{'id':'mention','type':'mention','mention':{'mentionType':'bot','mentionId':contact or m.CONFIG['bot_contact_id'],'mentionName':name}},
                    {'id':'text','type':'text','text':{'text':text}}]}}
    return message(type='9',data=json.dumps(data),**changes)

class Tests(unittest.TestCase):
    def setUp(self):
        policy = patch.dict(m.CONFIG, {"sender_policy":"owner"})
        policy.start(); self.addCleanup(policy.stop)
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base/'baseline.json').write_text(json.dumps({'seq':'0'}))
        self.base_patch = patch.object(m,'BASE',base)
        self.db_patch = patch.object(m,'DB',base/'db')
        self.base_patch.start(); self.db_patch.start()
        m.initialize()

    def tearDown(self):
        self.db_patch.stop(); self.base_patch.stop(); self.tmp.cleanup()

    def event(self, **changes):
        event = {'type':'EVENT','eventType':'asn_bot_callback_msg','eventId':'event1','deliveryId':'delivery1',
            'payload':{'imChat':{'chatId':m.CONFIG['chat_id']},'imMessage':message()}}
        event.update(changes)
        return event

    def test_only_authorized_plain_commands(self):
        self.assertEqual(m.command_of(message()),'测试')
        for changes in [{'creatorContactId':'someone-else'},{'chatId':'other'},{'recallFlag':'1'},{'updateFlag':'1'},{'category':'2'},{'type':'1113'},{'data':json.dumps({'plainText':{'text':'普通消息 /codex 测试'}})}]:
            self.assertIsNone(m.command_of(message(**changes)))

    def test_rich_text_order_and_prefix(self):
        data={'richText':{'root':{'children':[{'type':'paragraph','children':[{'id':'b'},{'id':'a'}]}]},'elements':[{'id':'a','type':'text','text':{'text':'检查文件'}},{'id':'b','type':'text','text':{'text':'/codex '}}]}}
        self.assertEqual(m.command_of(message(type='9',data=json.dumps(data))), '检查文件')

    def test_leading_mention(self):
        self.assertEqual(m.command_of(message(data=json.dumps({'plainText':{'text':'@小跟班\u00a0/codex 检查文件'}}))), '检查文件')
        self.assertIsNone(m.command_of(message(data=json.dumps({'plainText':{'text':'@别的机器人 /codex 检查文件'}}))))

    def test_natural_mention_requires_real_bot_id(self):
        self.assertEqual(m.command_of(mentioned()),'检查文件')
        self.assertEqual(m.command_of(mentioned(name='改名后的小跟班')),'检查文件')
        self.assertEqual(m.command_of(mentioned('/codex 检查文件')),'检查文件')
        self.assertEqual(m.command_of(mentioned('')),'')
        self.assertIsNone(m.command_of(mentioned(contact='another@bot.com')))
        for text in ('普通聊天','@小跟班 检查文件'):
            self.assertIsNone(m.command_of(message(data=json.dumps({'plainText':{'text':text}}))))
        for kwargs in ({'creatorContactId':'other'},{'chatId':'other'},{'recallFlag':'1'},{'updateFlag':'1'},{'category':'2'}):
            self.assertIsNone(m.command_of(mentioned(**kwargs)))
        self.assertIsNone(m.command_of(mentioned(seq=m.CONFIG['natural_mention_since_seq'])))

    def test_orphan_or_quoted_mention_does_not_trigger(self):
        item=mentioned()
        data=json.loads(item['data'])
        data['richText']['root']['children'][0]['children']=[{'id':'text','type':'text'}]
        item['data']=json.dumps(data)
        self.assertIsNone(m.command_of(item))
        item=mentioned()
        data=json.loads(item['data']);data['richText']['root']['children'][0]['type']='blockquote'
        item['data']=json.dumps(data)
        self.assertIsNone(m.command_of(item))

    def test_ios_user_mention_with_exact_bot_account(self):
        # Real iOS messages use mentionType=user plus contactId, unlike Mac.
        item = mentioned(' 看下项目结构')
        data = json.loads(item['data'])
        mention = data['richText']['elements'][0]['mention']
        mention.update(mentionType='user', contactId=m.CONFIG['bot_contact_id'])
        item['data'] = json.dumps(data)
        item['extList'] = [{'key':'_clientPlatform','value':'IOS'}]
        self.assertTrue(m.mentions_bot(item))
        self.assertEqual(m.command_of(item), '看下项目结构')
        for target in ('another@bot.com', 'colleague@xiaohongshu.com', '', None):
            mention['mentionId'] = target
            item['data'] = json.dumps(data)
            self.assertIsNone(m.command_of(item))

    def test_ios_mention_must_be_visible_and_not_quoted(self):
        for hidden in ('orphan', 'blockquote'):
            item = mentioned()
            data = json.loads(item['data'])
            data['richText']['elements'][0]['mention']['mentionType'] = 'user'
            if hidden == 'orphan':
                data['richText']['root']['children'][0]['children'] = [{'id':'text'}]
            else:
                data['richText']['root']['children'][0]['type'] = hidden
            item['data'] = json.dumps(data)
            self.assertIsNone(m.command_of(item))

    def test_natural_mention_uses_shared_dedup(self):
        item=mentioned()
        with m.connect() as db:
            self.assertTrue(m.enqueue_message(db,item,'websocket'))
            self.assertFalse(m.enqueue_message(db,item,'poll'))
            self.assertEqual(db.execute('select count(*) from inbox').fetchone()[0],1)

    def test_app_outbox_retries_keep_identity_and_no_webhook_fallback(self):
        with patch.dict(m.CONFIG,{'reply_channel':'app'}):
            m.say('测试',title='测试')
        with m.connect() as db:
            row=db.execute('select * from outbox').fetchone()
            operation=row['operate_code']
            self.assertEqual(row['channel'],'app')
            self.assertTrue(operation)
        with patch.object(m.APP_SENDER,'send',side_effect=RuntimeError('permission pending')) as send,patch.object(m.urllib.request,'urlopen') as webhook:
            with self.assertRaises(RuntimeError):m.send_one()
            webhook.assert_not_called()
            self.assertEqual(send.call_args.args[2],operation)
        with m.connect() as db:
            row=db.execute('select * from outbox').fetchone()
            self.assertEqual(row['status'],'pending')
            self.assertEqual(row['attempts'],1)
            db.execute('update outbox set next_attempt=0')
        receipt={'id':'sent','creatorContactId':m.CONFIG['bot_contact_id']}
        with patch.object(m.APP_SENDER,'send',return_value=receipt) as send:
            m.send_one();self.assertEqual(send.call_args.args[2],operation)
        with m.connect() as db:
            row=db.execute('select * from outbox').fetchone()
            self.assertEqual(row['status'],'sent');self.assertEqual(row['message_id'],'sent')

    def test_default_pending_outbox_remains_webhook(self):
        with patch.dict(m.CONFIG,{'reply_channel':'webhook'}):m.say('定时通道保持不变')
        with m.connect() as db:self.assertEqual(db.execute('select channel from outbox').fetchone()[0],'webhook')

    def test_durable_before_ack_and_shared_dedup(self):
        event = self.event()
        ack = m.receive_event(event)
        self.assertEqual(ack,{'type':'ACK_EVENT','eventId':'event1','eventType':'asn_bot_callback_msg','deliveryId':'delivery1'})
        with m.connect() as db:
            self.assertEqual(db.execute('select status from ws_events').fetchone()[0], 'pending')
            self.assertEqual(db.execute('select count(*) from inbox').fetchone()[0],0)
        with patch.object(m,'read_message',return_value=message()):
            m.resolve_event()
        m.receive_event(event)
        with patch.object(m,'read_messages',side_effect=[[message()],[]]):
            m.poll()
        with m.connect() as db:
            row = db.execute('select seq,source from inbox').fetchone()
            self.assertEqual(tuple(row),('9007199254740993','websocket'))
            self.assertEqual(db.execute('select count(*) from inbox').fetchone()[0],1)
        with patch.object(m,'connect',side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.OperationalError): m.receive_event(self.event(eventId='event2'))

    def test_media_callback_does_not_bypass_task_trigger(self):
        event=self.event(eventId='media-callback')
        event['payload']['imMessage']['type']='6'
        m.receive_event(event)
        value=message(type='6',data=json.dumps({'file':{'url':'https://ep-redim-i1.xhscdn.com/redim_file/test','name':'test','size':3}}))
        with patch.object(m,'read_message',return_value=value):m.resolve_event()
        with m.connect() as db:
            self.assertEqual(db.execute('select count(*) from inbox').fetchone()[0],0)
            self.assertEqual(db.execute("select status from ws_events where event_id='media-callback'").fetchone()[0],'processed')

    def test_thread_read_fallback_validates_same_chat_and_thread(self):
        chat=m.CONFIG['chat_id'];m.set_meta('message_thread:'+chat+':sample','topic')
        value=message();value['threadContext']={'threadId':'topic'}
        proc=subprocess.CompletedProcess([],0,json.dumps({'success':True,'messageDtoList':[]}),'')
        with patch.object(m.subprocess,'run',return_value=proc),patch.object(m.context_reader,'query',return_value={'messageDtoList':[value]}) as query:
            self.assertEqual(m.read_message('sample'),value)
            self.assertEqual(query.call_args.args,(m.HI,'im:thread-message-detail','--chat-id',chat,'--thread-id','topic','--message-id','sample'))
        value['chatId']='another'
        with patch.object(m.subprocess,'run',return_value=proc),patch.object(m.context_reader,'query',return_value={'messageDtoList':[value]}):
            with self.assertRaises(RuntimeError):m.read_message('sample')

    def test_out_of_scope_and_recalled(self):
        for i, changes in enumerate([{'chatId':'private'},{'creatorContactId':'other'},{'recallFlag':'1'},{'type':'999'}]):
            event = self.event(eventId=str(i))
            event['payload']['imMessage'].update(changes)
            if 'chatId' in changes: event['payload']['imChat']['chatId']=changes['chatId']
            m.receive_event(event)
        with m.connect() as db:
            self.assertEqual(db.execute("select count(*) from ws_events where status='pending'").fetchone()[0],0)
        m.receive_event(self.event(eventId='canonical-recalled'))
        with patch.object(m,'read_message',return_value=message(recallFlag='1')):
            m.resolve_event()
        with m.connect() as db:
            self.assertEqual(db.execute('select count(*) from inbox').fetchone()[0],0)

    def test_retry_survives_restart_and_poll_first(self):
        m.receive_event(self.event())
        with patch.object(m,'read_message',side_effect=RuntimeError('not yet visible')):
            m.resolve_event()
        with m.connect() as db:
            self.assertEqual(db.execute('select attempts from ws_events').fetchone()[0],1)
            db.execute('update ws_events set next_attempt=0')
        m.initialize()
        with patch.object(m,'read_messages',side_effect=[[message()],[]]): m.poll()
        with patch.object(m,'read_message',return_value=message()): m.resolve_event()
        with m.connect() as db:
            self.assertEqual(db.execute('select count(*) from inbox').fetchone()[0],1)
            self.assertEqual(db.execute('select source from inbox').fetchone()[0],'poll')
            self.assertEqual(db.execute('select status from ws_events').fetchone()[0],'processed')

    def test_unsafe_message_id_and_old_baseline(self):
        with m.connect() as db:
            self.assertFalse(m.enqueue_message(db,message(id='../outside'),'websocket'))
            self.assertFalse(m.enqueue_message(db,message(seq='0'),'websocket'))

    def test_connection_closed_after_commit_and_rollback(self):
        with m.connect() as db:
            db.execute("insert into meta values ('committed','yes')")
        with self.assertRaises(sqlite3.ProgrammingError): db.execute('select 1')
        with self.assertRaises(RuntimeError):
            with m.connect() as failed:
                failed.execute("insert into meta values ('rolled_back','no')")
                raise RuntimeError('rollback')
        with self.assertRaises(sqlite3.ProgrammingError): failed.execute('select 1')
        self.assertEqual(m.get_meta('committed'),'yes')
        self.assertIsNone(m.get_meta('rolled_back'))

    def test_many_db_operations_under_fd_limit_without_gc(self):
        # Run with GC disabled and the macOS launchd-sized descriptor limit.
        # This reliably exposes leaked SQLite handles instead of relying on GC.
        script = '''
import gc,importlib.util,resource,tempfile
from pathlib import Path
spec=importlib.util.spec_from_file_location('bridge',__import__('sys').argv[1])
b=importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE,(min(128,soft),hard))
gc.disable()
with tempfile.TemporaryDirectory() as d:
 b.BASE=Path(d); b.DB=b.BASE/'db'
 (b.BASE/'baseline.json').write_text('{"seq":"0"}')
 b.initialize()
 for i in range(2000):
  b.set_meta('stress',i)
  assert b.get_meta('stress')==str(i)
print('4000 DB operations passed with GC disabled and <=128 file descriptors')
'''
        result=subprocess.run([sys.executable,'-c',script,str(Path(__file__).with_name('bridge.py'))],capture_output=True,text=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_appserver_close_releases_pipes(self):
        rpc=m.AppServer.__new__(m.AppServer)
        rpc.close_lock=threading.Lock()
        rpc.logfile=(m.BASE/'fake.log').open('w')
        rpc.proc=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=rpc.logfile,text=True,start_new_session=True)
        rpc.reader_thread=threading.Thread(target=lambda: rpc.proc.stdout.read(),daemon=True)
        rpc.reader_thread.start()
        rpc.close(); rpc.close()
        self.assertIsNotNone(rpc.proc.poll())
        self.assertFalse(rpc.reader_thread.is_alive())
        self.assertTrue(rpc.proc.stdin.closed)
        self.assertTrue(rpc.proc.stdout.closed)
        self.assertTrue(rpc.logfile.closed)

    def test_independent_model_overrides_new_and_resumed_thread(self):
        # Even a saved thread must receive the bridge-specific values each turn.
        for existing in (False, True):
            with self.subTest(existing=existing):
                m.set_meta('thread_id', 'saved-thread' if existing else '')
                thread = 'saved-thread' if existing else 'new-thread'
                rpc = m.AppServer.__new__(m.AppServer)
                rpc.pending = [
                    {'method':'item/completed','params':{'threadId':thread,'item':{'id':'answer','type':'agentMessage','text':'ok'}}},
                    {'method':'turn/completed','params':{'threadId':thread,'turn':{'id':'turn','status':'completed'}}},
                ]
                with patch.dict(m.CONFIG,{'model':'bridge-model','reasoning_effort':'high','network_access':existing}), patch.object(rpc,'call',side_effect=[{'thread':{'id':thread}},{'turn':{'id':'turn'}}]) as call:
                    prompt=m.PreparedInput('只回复 ok',[{'type':'image','url':'data:image/png;base64,verified'}])
                    self.assertEqual(rpc.run(prompt),'ok')
                first, second = call.call_args_list
                self.assertEqual(first.args[0],'thread/resume' if existing else 'thread/start')
                self.assertEqual(first.args[1]['model'],'bridge-model')
                self.assertEqual(first.args[1]['config']['model_reasoning_effort'],'high')
                self.assertEqual(second.args[1]['model'],'bridge-model')
                self.assertEqual(second.args[1]['effort'],'high')
                self.assertEqual(second.args[1]['input'],[{'type':'text','text':'只回复 ok'},{'type':'image','url':'data:image/png;base64,verified'}])
                self.assertIs(second.args[1]['sandboxPolicy']['networkAccess'],existing)
                self.assertEqual(second.args[1]['sandboxPolicy']['type'],'workspaceWrite')
                self.assertEqual(second.args[1]['approvalPolicy'],'on-request')
                self.assertEqual(m.get_meta('thread_id'),thread)

    def test_empty_model_setting_fails_before_execution(self):
        rpc = m.AppServer.__new__(m.AppServer)
        with patch.dict(m.CONFIG,{'reasoning_effort':''}),patch.object(rpc,'call') as call:
            with self.assertRaises(ValueError):rpc.run('测试')
            call.assert_not_called()

    def test_busy_session_never_starts_turn_or_changes_binding(self):
        m.set_meta('thread_id','saved-thread')
        rpc=m.AppServer.__new__(m.AppServer)
        with patch.object(rpc,'call',side_effect=RuntimeError('thread saved-thread already has an active writer')) as call:
            with self.assertRaises(m.SessionBusyError):rpc.run('任务')
            self.assertEqual(call.call_count,1)
            self.assertEqual(call.call_args.args[0],'thread/resume')
        self.assertEqual(m.get_meta('thread_id'),'saved-thread')

    def test_paging_dedup_restart(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d)
            (base/'baseline.json').write_text(json.dumps({'seq':'9007199254740990','ids':[]}))
            with patch.object(m,'BASE',base),patch.object(m,'DB',base/'db'):
                m.initialize()
                first=[message(id='b'),message(seq='9007199254740992',id='a')]
                second=[message(seq='9007199254740991',id='older')]
                with patch.object(m,'read_messages',side_effect=[first,second,[]]):
                    m.poll()
                with patch.object(m,'read_messages',return_value=first):
                    m.poll()
                with m.connect() as db:
                    self.assertEqual(db.execute('select count(*) from inbox').fetchone()[0],3)
                    db.execute("update inbox set status='running' where id='a'")
                m.initialize()
                with m.connect() as db:
                    self.assertEqual(db.execute("select status from inbox where id='a'").fetchone()[0],'interrupted')
                self.assertEqual(m.get_meta('cursor'),'9007199254740993')

if __name__=='__main__': unittest.main()
