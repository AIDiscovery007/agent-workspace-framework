"""Send Markdown as the configured Hi application bot, never via a Webhook."""
import json
import re
import time
import urllib.request
from pathlib import Path


class HiAppError(RuntimeError):
    pass


class HiAppSender:
    def __init__(self, credentials=None):
        self.credentials = credentials or Path.home()/'.config/agent-workspace/credentials.json'
        self.token = None
        self.expires = 0

    def _post(self, base, path, payload):
        req = urllib.request.Request(base+path, data=json.dumps(payload).encode(),
            headers={'Content-Type':'application/json'},method='POST')
        with urllib.request.urlopen(req,timeout=20) as response:
            return json.load(response,parse_int=str)

    def send(self, chat_id, content, operate_code, expected_sender, mention_contact_id=None):
        if not operate_code:
            raise ValueError('A persisted operateCode is required')
        if mention_contact_id is not None and (not isinstance(mention_contact_id, str)
                or not re.fullmatch(r'[A-Za-z0-9._+\-]+@[A-Za-z0-9.\-]+', mention_contact_id)
                or mention_contact_id.endswith('@bot.com')):
            raise ValueError('A single employee contactId is required for mentions')
        c = json.loads(self.credentials.read_text())['hi']
        if not self.token or time.time() >= self.expires:
            body = self._post(c['baseUrl'],'/openapis/open/token/createAppAccessToken/v2',
                {'appId':c['appId'],'appSecret':c['appSecret']})
            data = body.get('data') or {}
            self.token = data.get('appAccessToken') or body.get('appAccessToken')
            if body.get('success') is False or not self.token:
                self.token = None
                raise HiAppError('应用令牌获取失败')
            self.expires = time.time()+max(1,min(7000,int(data.get('expiresIn') or body.get('expiresIn') or 7200)-60))
        body = self._post(c['baseUrl'],'/openapis/open/api/call/v2', {
            'appId':c['appId'],'appAccessToken':self.token,'userAccessToken':c['userAccessToken'],
            'apiAlias':'im:message.sendMarkdownToChat:v1',
            'bizParams':json.dumps({'chatId':chat_id,'type':10,'data':content,'operateCode':operate_code,
                'mentionContactIdList':[mention_contact_id] if mention_contact_id else [],
                'mentionAll':False,'refId':''},ensure_ascii=False)})
        if body.get('success') is not True:
            # Never include credentials, raw URLs, or arbitrary response data in logs.
            self.token = None
            raise HiAppError('应用发消息失败，错误码 '+str(body.get('errorCode')))
        data = body.get('data') or {}
        if isinstance(data,str): data = json.loads(data,parse_int=str)
        result = data.get('result') or {}
        if not result.get('id') or result.get('chatId') != chat_id or result.get('creatorContactId') != expected_sender:
            raise HiAppError('发送回执缺失或群/机器人身份不匹配，需回读核实')
        return result
