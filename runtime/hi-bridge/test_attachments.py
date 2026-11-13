import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import attachments as a
import bridge as b

URL='https://ep-redim-i1.xhscdn.com/redim_file/test.xlsx'

def message():
    return {'id':'sample','type':'9','category':'1','seq':'9999','chatId':b.CONFIG['chat_id'],
            'creatorContactId':b.CONFIG['allowed_sender'],'data':json.dumps({'richText':{
        'root':{'children':[{'id':'f'},{'id':'m'},{'id':'t'}]},
        'elements':[{'id':'f','type':'file','file':{'url':URL,'name':'../数据.xlsx','size':3}},
                    {'id':'m','type':'mention','mention':{'mentionType':'bot','mentionId':b.CONFIG['bot_contact_id']}},
                    {'id':'t','type':'text','text':{'text':'处理附件'}}]}})}

def response(content=b'abc',content_type='application/octet-stream'):
    r=io.BytesIO(content);r.status=200;r.headers={'Content-Type':content_type}
    return r

class Tests(unittest.TestCase):
    def setUp(self):
        policy = patch.dict(b.CONFIG, {"sender_policy":"owner"})
        policy.start(); self.addCleanup(policy.stop)

    def image_message(self):
        m=message();data=json.loads(m['data'])
        data['richText']['elements'][0]={'id':'f','type':'image','image':{'url':URL.replace('test.xlsx','test.png'),'width':380,'height':320,'ext':'png','key':'image-key'}}
        m['data']=json.dumps(data);return m

    def image_bytes(self):
        from PIL import Image
        stream=io.BytesIO();Image.new('RGB',(8,6),'red').save(stream,format='PNG');return stream.getvalue()

    def test_visible_image_without_size_reaches_visual_input(self):
        m=self.image_message();content=self.image_bytes()
        with tempfile.TemporaryDirectory() as d,patch.object(b,'read_message',return_value=m),patch.object(a.urllib.request,'build_opener',return_value=Mock(open=Mock(return_value=response(content,'image/png')))):
            prompt=b.prepare_input('sample','处理附件',Path(d))
            self.assertEqual(len(getattr(prompt,'images',())),1,'可见图片必须进入视觉输入')
            manifest=json.loads((Path(d)/'attachments/manifest.json').read_text())
            path=Path(manifest[0]['path']);self.assertEqual(path.read_bytes(),content)
            self.assertEqual(a.base64.b64decode(prompt.images[0]['url'].split(',',1)[1]),content)
            # A member changing their downloaded file cannot change privileged model input.
            path.unlink();path.symlink_to('/etc/passwd')
            self.assertEqual(a.base64.b64decode(prompt.images[0]['url'].split(',',1)[1]),content)
            self.assertIn(str(path),prompt)

    def test_direct_reference_image_reaches_visual_input(self):
        current,original=self.reference_pair();original=self.image_message();original['id']='original'
        with tempfile.TemporaryDirectory() as d,patch.object(b,'read_message',side_effect=[current,original]),patch.object(a.urllib.request,'build_opener',return_value=Mock(open=Mock(return_value=response(self.image_bytes(),'image/png')))):
            prompt=b.prepare_input('sample','处理附件',Path(d))
            self.assertEqual(len(getattr(prompt,'images',())),1)

    def test_image_bytes_must_decode_and_stay_bounded(self):
        for content,mime in [(b'not an image','image/png'),(b'<html>error</html>','text/html')]:
            with tempfile.TemporaryDirectory() as d,patch.object(a.urllib.request,'build_opener',return_value=Mock(open=Mock(return_value=response(content,mime)))):
                with self.assertRaises(a.AttachmentError):a.download_files(a.files_of(self.image_message()),d)
        with tempfile.TemporaryDirectory() as d,patch.object(a,'MAX_IMAGE_BYTES',8),patch.object(a.urllib.request,'build_opener',return_value=Mock(open=Mock(return_value=response(self.image_bytes(),'image/png')))):
            with self.assertRaises(a.AttachmentError):a.download_files(a.files_of(self.image_message()),d)

    def test_hidden_or_nested_quote_images_are_not_collected(self):
        m=self.image_message();data=json.loads(m['data'])
        data['richText']['root']['children'][0]={'type':'blockquote','children':[{'id':'f'}]}
        m['data']=json.dumps(data);self.assertEqual(a.files_of(m),[])
        data['richText']['root']['children'][0]={'id':'unused'}
        m['data']=json.dumps(data);self.assertEqual(a.files_of(m),[])

    def reference_pair(self):
        current=message();current['refId']='original'
        data=json.loads(current['data'])
        data['richText']['root']['children']=[{'id':'m'},{'id':'t'}]
        current['data']=json.dumps(data)
        original=message();original.update(id='original',updateFlag='1',creatorContactId='another-group-member')
        return current,original

    def test_edited_reference_text_and_file_reach_input(self):
        current,original=self.reference_pair()
        with tempfile.TemporaryDirectory() as d,patch.object(b,'read_message',side_effect=[current,original]) as read,patch.object(a.urllib.request,'build_opener',return_value=Mock(open=Mock(return_value=response()))):
            prompt=b.prepare_input('sample','处理附件',Path(d))
            context=json.loads((Path(d)/'input-context.json').read_text())
            self.assertTrue(context['reference']['edited'])
            self.assertEqual(context['reference']['text'],'处理附件')
            self.assertIn(context['attachments'][0]['path'],prompt)
            self.assertEqual(read.call_count,2)

    def test_reference_is_not_recursively_executed(self):
        current,original=self.reference_pair();original['refId']='earlier'
        data=json.loads(original['data']);data['richText']['root']['children']=[{'id':'t'}]
        original['data']=json.dumps(data)
        with tempfile.TemporaryDirectory() as d,patch.object(b,'read_message',side_effect=[current,original]) as read,patch.object(b,'download_files') as download:
            prompt=b.prepare_input('sample','处理附件',Path(d))
            self.assertIn('不展开更早的引用链',prompt)
            self.assertEqual(read.call_count,2);download.assert_not_called()

    def test_missing_revoked_cross_group_reference_fails(self):
        for changes in [{'recallFlag':'1'},{'chatId':'another'},{'id':'wrong'},{'category':'2'}]:
            current,original=self.reference_pair();original.update(changes)
            with patch.object(b,'read_message',side_effect=[current,original]),patch.object(b,'download_files') as download:
                with self.assertRaises(a.AttachmentError):b.prepare_input('sample','处理附件',Path('/unused'))
                download.assert_not_called()
        current,_=self.reference_pair()
        with patch.object(b,'read_message',side_effect=[current,RuntimeError('unavailable')]):
            with self.assertRaisesRegex(a.AttachmentError,'无法读取被引用消息'):b.prepare_input('sample','处理附件',Path('/unused'))

    def test_visible_only(self):
        m=message();self.assertEqual(len(a.files_of(m)),1)
        d=json.loads(m['data']);d['richText']['root']['children'][0]={'type':'blockquote','children':[{'id':'f'}]}
        m['data']=json.dumps(d);self.assertEqual(a.files_of(m),[])
        d['richText']['root']['children'][0]={'id':'unused'}
        m['data']=json.dumps(d);self.assertEqual(a.files_of(m),[])

    def test_complete_attachment_reaches_model_input(self):
        m=message()
        with tempfile.TemporaryDirectory() as d,patch.object(b,'read_message',return_value=m),patch.object(a.urllib.request,'build_opener',return_value=Mock(open=Mock(return_value=response()))):
            prompt=b.prepare_input('sample','处理附件',Path(d))
            manifest=json.loads((Path(d)/'attachments/manifest.json').read_text())
            self.assertIn(manifest[0]['path'],prompt)
            self.assertEqual(Path(manifest[0]['path']).read_bytes(),b'abc')
            self.assertEqual(Path(manifest[0]['path']).parent,(Path(d)/'attachments').resolve())
            self.assertEqual(len(manifest[0]['sha256']),64)

    def test_partial_and_html_are_not_passed_to_codex(self):
        for content,mime in [(b'ab','application/octet-stream'),(b'abc','text/html'),(b'abcd','application/octet-stream')]:
            with self.subTest(content=content,mime=mime),tempfile.TemporaryDirectory() as d,patch.object(a.urllib.request,'build_opener',return_value=Mock(open=Mock(return_value=response(content,mime)))):
                with self.assertRaises(a.AttachmentError):a.download_files(a.files_of(message()),d)
                self.assertEqual(list(Path(d).iterdir()),[])

    def test_untrusted_urls_and_redirects(self):
        for url in ['http://ep-redim-i1.xhscdn.com/redim_file/a','https://localhost/redim_file/a','https://ep-redim-i1.xhscdn.com.evil.test/redim_file/a','https://ep-redim-i1.xhscdn.com/not-file/a']:
            with self.assertRaises(a.AttachmentError):a.checked_url(url)
        with self.assertRaises(a.AttachmentError):a.CheckedRedirect().redirect_request(None,None,302,'',{},'http://127.0.0.1/a')

    def test_changed_or_revoked_task_cannot_download(self):
        for changes in [{'recallFlag':'1'},{'updateFlag':'1'},{'creatorContactId':'other'},{'chatId':'other'}]:
            m=message();m.update(changes)
            with patch.object(b,'read_message',return_value=m),patch.object(b,'download_files') as download:
                with self.assertRaises(a.AttachmentError):b.prepare_input('sample','处理附件',Path('/unused'))
                download.assert_not_called()

    def test_size_limit_before_network(self):
        f=a.files_of(message())[0];f['size']=a.MAX_BYTES+1
        with tempfile.TemporaryDirectory() as d,patch.object(a.urllib.request,'build_opener') as opener:
            with self.assertRaises(a.AttachmentError):a.download_files([f],d)
            opener.return_value.open.assert_not_called()

if __name__=='__main__':unittest.main()
