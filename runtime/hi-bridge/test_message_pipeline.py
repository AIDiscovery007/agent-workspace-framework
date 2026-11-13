import io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from PIL import Image
import bridge as b
import attachments as a
import context_reader as c
from test_attachments import message,response
from test_message_content import msg,URL

class Tests(unittest.TestCase):
 def test_referenced_media_types_download_before_model(self):
  payloads={2:{'image':{'url':URL}},3:{'video':{'videoInfo':{'url':URL}}},4:{'voice':{'url':URL}},5:{'sticker':{'url':URL}},6:{'file':{'url':URL,'name':'sample.csv'}}}
  stream=io.BytesIO();Image.new('RGB',(4,4),'blue').save(stream,format='PNG')
  for typ,payload in payloads.items():
   current=message();data=json.loads(current['data']);data['richText']['root']['children']=[{'id':'m'},{'id':'t'}];current['data']=json.dumps(data);current['refId']='ref'
   original=msg(typ,payload);original.update(id='ref',category='1',recallFlag='0')
   content=stream.getvalue() if typ in (2,5) else b'abc'
   with self.subTest(typ=typ),tempfile.TemporaryDirectory() as d,patch.object(b,'read_message',side_effect=[current,original]),patch.object(b,'require_current_member'),patch.object(a.urllib.request,'build_opener',return_value=Mock(open=Mock(return_value=response(content)))),patch.object(b.media_input,'enrich',return_value=[{'type':'image','url':'data:image/jpeg;base64,test'}]) as media:
    prompt=b.prepare_input('sample','处理附件',Path(d))
    stored=json.loads((Path(d)/'input-context.json').read_text())
    self.assertEqual(len(stored['attachments']),1)
    self.assertEqual(Path(stored['attachments'][0]['path']).read_bytes(),content)
    self.assertEqual(media.called,typ in (3,4))
    if typ in (2,3,4,5):self.assertTrue(prompt.images)
 def test_transcript_and_frame_observations_reach_model(self):
  current=message();data=json.loads(current['data']);data['richText']['elements'][0]={'id':'f','type':'voice','voice':{'url':URL}};current['data']=json.dumps(data)
  def enrich(records,directory):records[0]['analysis']={'audio':{'text':'统计一二三的和','duration':3}};return []
  with tempfile.TemporaryDirectory() as d,patch.object(b,'read_message',return_value=current),patch.object(b,'require_current_member'),patch.object(a.urllib.request,'build_opener',return_value=Mock(open=Mock(return_value=response()))),patch.object(b.media_input,'enrich',side_effect=enrich):
   prompt=b.prepare_input('sample','处理附件',Path(d));self.assertIn('统计一二三的和',prompt)
 def test_referenced_card_is_expanded_without_invoking_actions(self):
  current=message();data=json.loads(current['data']);data['richText']['root']['children']=[{'id':'m'},{'id':'t'}];current['data']=json.dumps(data);current['refId']='ref'
  ref=msg(13,{'card':{'entity':json.dumps({'entityId':'10'})}});ref.update(id='ref',category='1')
  with tempfile.TemporaryDirectory() as d,patch.object(b,'read_message',side_effect=[current,ref]),patch.object(b,'require_current_member'),patch.object(c,'query',return_value={'cards':[{'entityId':'10','status':'resolved','elements':{'button':{'type':'button','title':'提交','ext':{'disabled':False}}}}]}) as query:
   prompt=b.prepare_input('sample','处理附件',Path(d));self.assertIn('提交',prompt);self.assertIn('未点击或提交',prompt);self.assertEqual(query.call_count,1)
 def test_downloader_rejects_voice_outside_trusted_origin(self):
  with tempfile.TemporaryDirectory() as d:
   with self.assertRaises(a.AttachmentError):a.download_files([{'kind':'voice','url':'https://localhost/private','allow_unknown_size':True}],d)

if __name__=='__main__':unittest.main()

class AnimatedStickerTests(unittest.TestCase):
 def test_animated_sticker_uses_verified_png_samples(self):
  stream=io.BytesIO();frames=[Image.new('RGB',(8,8),c) for c in ['red','green','blue','yellow']]
  frames[0].save(stream,format='GIF',save_all=True,append_images=frames[1:],duration=100,loop=0)
  descriptor={'kind':'image','name':'贴纸','url':URL,'allow_unknown_size':True}
  with tempfile.TemporaryDirectory() as d,patch.object(a.urllib.request,'build_opener',return_value=Mock(open=Mock(return_value=response(stream.getvalue(),'image/gif')))):
   r=a.download_files([descriptor],d)[0];self.assertEqual(r['sampled_frames'],[0,1,2,3]);self.assertEqual(len(r.additional_images),3);self.assertTrue(r.image_input['url'].startswith('data:image/png;base64,'))
