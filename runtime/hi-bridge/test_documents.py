import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import documents as d
import bridge as b
from test_attachments import message

URL='https://docs.xiaohongshu.com/doc/abc123'

def card():
    m=message();m.update(id='card',data=json.dumps({'richText':{'root':{'children':[{'id':'link'}]},'elements':[{'id':'link','type':'link','link':{'href':URL,'title':'评测标准'}}]}}))
    return m

class Tests(unittest.TestCase):
    def setUp(self):
        policy = patch.dict(b.CONFIG, {"sender_policy":"owner"})
        policy.start(); self.addCleanup(policy.stop)

    def test_links_exact_host_dedup_query(self):
        self.assertEqual(d.document_links(f'[{URL}]({URL}?x=1) {URL} https://docs.xiaohongshu.com.evil/doc/no https://docs.xiaohongshu.com/sheet/no'),[URL])

    def test_card_text_visible_nodes_only(self):
        m=card();self.assertEqual(b.text_of(m),f'[评测标准]({URL})')
        data=json.loads(m['data']);data['richText']['root']['children']=[];m['data']=json.dumps(data)
        self.assertEqual(b.text_of(m),'')

    def test_reference_card_fetches_real_body_path_into_input(self):
        current=message();current['refId']='card'
        data=json.loads(current['data']);data['richText']['root']['children']=[{'id':'m'},{'id':'t'}];current['data']=json.dumps(data)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.xml';source.write_text('<document><p>真实评测标准</p></document>')
            response=subprocess.CompletedProcess([],0,json.dumps({'shortcutId':'abc123','title':'评测标准','format':'xml','xmlPath':str(source),'hash':'v1'}),'')
            with patch.object(b,'read_message',side_effect=[current,card()]),patch.object(d.subprocess,'run',return_value=response) as run:
                prompt=b.prepare_input('sample','处理附件',root)
            manifest=json.loads((root/'input-context.json').read_text())
            document=manifest['documents'][0]
            self.assertEqual(Path(document['path']).read_text(),source.read_text())
            self.assertIn(document['path'],prompt);self.assertIn(URL,prompt)
            self.assertEqual(run.call_args.args[0],[b.HI,'docs:get','--shortcut-id',URL,'--format','xml','--mode','common'])

    def test_failed_read_does_not_produce_title_only_summary(self):
        for response in [subprocess.CompletedProcess([],1,'','denied'),subprocess.CompletedProcess([],0,'{}','')]:
            with tempfile.TemporaryDirectory() as tmp,patch.object(d.subprocess,'run',return_value=response):
                with self.assertRaises(b.AttachmentError):d.fetch_documents([URL],tmp,b.HI)
                self.assertFalse((Path(tmp)/'manifest.json').exists())

    def test_document_count_limit_before_network(self):
        with patch.object(d.subprocess,'run') as run:
            with self.assertRaises(b.AttachmentError):d.fetch_documents([URL+str(i) for i in range(6)],'/unused',b.HI)
            run.assert_not_called()

if __name__=='__main__':unittest.main()
