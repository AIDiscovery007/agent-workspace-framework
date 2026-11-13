import json
import unittest
from unittest.mock import patch
import message_content as m
import context_reader as c
from attachments import AttachmentError
import bridge as b

URL='https://ep-redim-i1.xhscdn.com/redim_file/example'
def msg(typ,data):return {'type':str(typ),'data':json.dumps(data),'chatId':b.CONFIG['chat_id']}
def rich(elements,nodes=None):
 return msg(9,{'richText':{'root':{'type':'root','children':nodes or [{'id':e['id'],'type':e['type']} for e in elements]},'elements':elements}})

class Tests(unittest.TestCase):
 def test_all_twelve_top_level_types(self):
  bodies={1:{'plainText':{'text':'hello'}},2:{'image':{'url':URL}},3:{'video':{'videoInfo':{'url':URL}}},4:{'voice':{'url':URL}},5:{'sticker':{'url':URL}},6:{'file':{'url':URL,'name':'f'}},9:{'richText':{'root':{'children':[{'id':'t'}]},'elements':[{'id':'t','type':'text','text':{'text':'hi'}}]}},10:{'markdown':{'text':'# hi'}},11:{'forward':{'forwardGroupId':'9007199254740993'}},12:{'emoji':{'name':'微笑'}},13:{'card':{'entity':json.dumps({'entityId':'9007199254740994'})}},14:{'stream':{'chunks':[{'type':'textStart','partId':'p'},{'type':'textDelta','partId':'p','delta':'正文'},{'type':'controlFinish'}]}}}
  for typ,data in bodies.items():
   with self.subTest(typ=typ):
    result=m.normalize(msg(typ,data));self.assertTrue(result['text'] or result['media'])
  self.assertEqual(m.normalize(msg(5,bodies[5]))['media'][0]['kind'],'image')
 def test_styles_lists_links_and_containers(self):
  elements=[{'id':'a','type':'text','text':{'text':'第一项','bold':True,'italic':True,'strikethrough':True}}, {'id':'b','type':'text','text':{'text':'第二项'}},{'id':'l1','type':'numbered-list','numberedList':{'index':2,'level':1}},{'id':'l2','type':'bulleted-list','bulletedList':{'level':2}}]
  tree=[{'id':'container','type':'columns','children':[{'id':'l1','type':'numbered-list','children':[{'id':'a'}]},{'id':'l2','type':'bulleted-list','children':[{'id':'b'}]}]}]
  text=m.normalize(rich(elements,tree))['text'];self.assertEqual(text,'2. ~~***第一项***~~\n  - 第二项')
  link=rich([{'id':'l','type':'link','link':{'href':'https://example.test','title':'fallback'}},{'id':'t','type':'text','text':{'text':'标签'}}],[{'id':'l','children':[{'id':'t'}]}])
  self.assertEqual(m.normalize(link)['text'],'[标签](https://example.test)')
 def test_inline_media_and_unknown_nodes_are_not_silent(self):
  value=rich([{'id':'p','type':'paragraph'},{'id':'i','type':'sticker','sticker':{'url':URL}},{'id':'u','type':'futureWidget'}],[{'id':'p','children':[{'id':'i'},{'id':'u'}]}])
  r=m.normalize(value);self.assertEqual(len(r['media']),1);self.assertIn('futureWidget',r['warnings'][0])
 def test_quoted_mentions_never_trigger_task(self):
  value=rich([{'id':'m','type':'mention','mention':{'mentionId':b.CONFIG['bot_contact_id'],'mentionType':'bot'}}],[{'type':'blockquote','children':[{'id':'m'}]}])
  self.assertFalse(b.mentions_bot(value))
 def test_stream_deduplicates_and_excludes_reasoning(self):
  chunks=[{'type':'textDelta','partId':'p','operateCode':'x','delta':'正文'}]*2+[{'type':'reasoningDelta','delta':'private reasoning'}]
  r=m.normalize(msg(14,{'stream':{'chunks':chunks}}));self.assertEqual(r['text'],'正文');self.assertTrue(r['warnings'])
 def test_missing_media_or_unknown_stream_fails_explicitly(self):
  for value in [msg(2,{'image':{}}),msg(14,{'stream':{'unsupported':'x'}})]:
   with self.assertRaises(AttachmentError):m.normalize(value)
 def test_node_depth_limit(self):
  node={'id':'x'}
  for _ in range(35):node={'children':[node]}
  with self.assertRaises(AttachmentError):m.normalize(rich([], [node]))
 def test_forward_reads_only_shared_bundle_and_nested_reference(self):
  value=msg(11,{'forward':{'forwardGroupId':'9007199254740993'}})
  item={'forwardGroupId':'9007199254740993','originMsgCategory':'1','originMsgType':'1','originMsgData':json.dumps({'plainText':{'text':'资料'}}),'originRefMsgType':'2','originRefMsgData':json.dumps({'image':{'url':URL}}),'originChatId':'another-chat'}
  with patch.object(c,'query',return_value={'success':True,'result':[item]}) as query:
   contexts,media=c.expand([value],'/hi');self.assertEqual(len(media),1);self.assertEqual(contexts[1]['text'],'资料');self.assertEqual(query.call_args.args,('/hi','im:forward-messages','--forward-group-id','9007199254740993'))
 def test_forward_cycle_stops_and_wrong_bundle_rejected(self):
  value=msg(11,{'forward':{'forwardGroupId':'1'}})
  item={'forwardGroupId':'1','originMsgType':'11','originMsgData':value['data']}
  with patch.object(c,'query',return_value={'result':[item]}) as query:
   contexts,_=c.expand([value],'/hi');self.assertEqual(query.call_count,1);self.assertTrue(contexts[-1]['warnings'])
  item['forwardGroupId']='other'
  with patch.object(c,'query',return_value={'result':[item]}):
   with self.assertRaises(AttachmentError):c.expand([value],'/hi')
 def test_all_card_components_are_read_only(self):
  kinds=['text','markdown','richText','image','header','divider','userList','button','action','input','select','checkbox']
  elements={k:{'type':k,'title':k,'ext':{'value':'v','disabled':True,'visibility':'unknown','callbackToken':'secret'}} for k in kinds}
  value=msg(13,{'entity':json.dumps({'entityId':'42'})})
  with patch.object(c,'query',return_value={'cards':[{'entityId':'42','status':'partial','warnings':['VISIBILITY_NOT_EVALUATED'],'elements':elements}]}) as query:
   contexts,media=c.expand([value],'/hi');self.assertEqual(set(contexts[-1]['elements']),set(kinds));self.assertNotIn('secret',json.dumps(contexts));self.assertEqual(media,[]);self.assertEqual(query.call_count,1)
 def test_topic_cross_chat_rejected(self):
  value=msg(1,{'plainText':{'text':'任务'}});value['threadContext']={'threadId':'t'}
  with patch.object(c,'query',return_value={'result':[{'chatId':'other','threadContext':{'threadId':'t'}}]}):
   with self.assertRaises(AttachmentError):c.thread_context(value,'/hi')
 def test_media_only_mention_has_payload(self):
  value=rich([{'id':'i','type':'image','image':{'url':URL}},{'id':'m','type':'mention','mention':{'mentionId':b.CONFIG['bot_contact_id'],'mentionType':'bot'}}])
  self.assertTrue(b.has_payload(value));self.assertTrue(b.mentions_bot(value));self.assertEqual(b.text_of(value),'')
 def test_standalone_media_cannot_bypass_real_mention(self):
  value=msg(2,{'image':{'url':URL}});value.update(category='1',creatorContactId=b.CONFIG['allowed_sender'],seq='9999999999999999999')
  self.assertIsNone(b.command_of(value))

if __name__=='__main__':unittest.main()
