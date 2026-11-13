import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from hi_app_sender import HiAppSender, HiAppError


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'credentials'
        self.path.write_text(json.dumps({'hi':{'baseUrl':'https://example.invalid','appId':'fake','appSecret':'secret','userAccessToken':'token'}}))
        self.sender=HiAppSender(self.path)

    def tearDown(self): self.tmp.cleanup()

    def test_send_identity_and_persistent_operation_code(self):
        result={'id':'9007199254740993','chatId':'chat','creatorContactId':'bot'}
        with patch.object(self.sender,'_post',side_effect=[{'data':{'appAccessToken':'fake'}},{'success':True,'data':json.dumps({'result':result})}]) as post:
            self.assertEqual(self.sender.send('chat','**内容**','stable','bot'),result)
            payload=post.call_args.args[2]
            self.assertEqual(payload['apiAlias'],'im:message.sendMarkdownToChat:v1')
            self.assertEqual(json.loads(payload['bizParams'])['operateCode'],'stable')
            self.assertEqual(json.loads(payload['bizParams'])['mentionContactIdList'], [])

    def test_mentions_use_structured_recipient_and_never_everyone(self):
        result={'id':'id','chatId':'chat','creatorContactId':'bot'}
        with patch.object(self.sender,'_post',side_effect=[{'data':{'appAccessToken':'fake'}},{'success':True,'data':{'result':result}}]) as post:
            self.sender.send('chat','请求人：同名成员','stable','bot',mention_contact_id='member@xiaohongshu.net')
            params=json.loads(post.call_args.args[2]['bizParams'])
            self.assertEqual(params['mentionContactIdList'], ['member@xiaohongshu.net'])
            self.assertFalse(params['mentionAll'])
            self.assertEqual(params['operateCode'], 'stable')

    def test_invalid_mention_rejected_before_network(self):
        for target in ('@all','*','bot@bot.com',['one@xiaohongshu.com'], 'x\ny@xiaohongshu.com'):
            with patch.object(self.sender,'_post') as post:
                with self.assertRaises(ValueError):self.sender.send('chat','text','op','bot',mention_contact_id=target)
                post.assert_not_called()

    def test_permission_failure_is_not_success(self):
        with patch.object(self.sender,'_post',side_effect=[{'data':{'appAccessToken':'fake'}},{'success':False,'errorCode':99990131}]):
            with self.assertRaisesRegex(HiAppError,'99990131'):
                self.sender.send('chat','内容','stable','bot')

    def test_wrong_sender_is_rejected(self):
        with patch.object(self.sender,'_post',side_effect=[{'data':{'appAccessToken':'fake'}},{'success':True,'data':{'result':{'id':'id','chatId':'chat','creatorContactId':'someone'}}}]):
            with self.assertRaisesRegex(HiAppError,'身份不匹配'):
                self.sender.send('chat','内容','stable','bot')

if __name__=='__main__': unittest.main()
