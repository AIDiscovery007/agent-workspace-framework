import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import reply_artifacts as a

SVG = '<svg xmlns="http://www.w3.org/2000/svg"><title>骑车鹈鹕</title><circle r="10"/></svg>'
URL = 'https://hi.xiaohongshu.com/clouddrive/dist/v1/f/test'


class Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name); self.run = self.root/'message123'; self.run.mkdir()
        (self.run/'result.txt').write_text(SVG)
        self.commands = []

    def cli(self, cmd, **kwargs):
        self.commands.append(cmd)
        name = '骑车鹈鹕-message1.zip'
        if cmd[1] == 'utils:generate-operate-code':
            data = {'operateCode':'persistent-op'}
        elif cmd[1] == 'pan:write':
            data = {'entryId':'file-id', 'shareUrl':URL, 'fileName':name}
        else:
            data = {'entryId':'file-id','fileName':name,'fileSize':(self.root/'Hi绘图'/name).stat().st_size,'status':'ACTIVE'}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(data), '')

    def deliver(self):
        return a.deliver_svg(SVG, self.run, self.root, 'hi', 'Codex/小跟班产物')

    def test_file_bytes_preserved_and_verified_receipt_reused(self):
        with patch.object(a.subprocess, 'run', side_effect=self.cli):
            reply = self.deliver()
            self.assertEqual(self.deliver(), reply)
        self.assertIn(URL, reply); self.assertNotIn('<svg', reply)
        record = json.loads((self.run/'reply-artifact.json').read_text())
        self.assertTrue(record['verified'])
        self.assertEqual(Path(record['path']).read_bytes(), SVG.encode())
        with zipfile.ZipFile(record['upload_path']) as archive:
            self.assertEqual(archive.read(Path(record['path']).name), SVG.encode())
        self.assertEqual([x[1] for x in self.commands].count('pan:write'), 1)
        self.assertEqual([x[1] for x in self.commands].count('utils:generate-operate-code'), 1)

    def test_uncertain_upload_reuses_original_operation(self):
        original = self.cli
        def fail_upload(cmd, **kwargs):
            if cmd[1] == 'pan:write':
                self.commands.append(cmd)
                raise subprocess.TimeoutExpired(cmd, 120)
            return original(cmd, **kwargs)
        with patch.object(a.subprocess, 'run', side_effect=fail_upload):
            failed = self.deliver()
        self.assertNotIn(URL, failed); self.assertIn('尚未完成分享', failed)
        with patch.object(a.subprocess, 'run', side_effect=self.cli): self.deliver()
        writes = [cmd for cmd in self.commands if cmd[1] == 'pan:write']
        self.assertEqual(len(writes), 2)
        self.assertEqual([cmd[-1] for cmd in writes], ['persistent-op','persistent-op'])

    def test_metadata_mismatch_does_not_publish_link(self):
        def wrong_meta(cmd, **kwargs):
            if cmd[1] == 'pan:readmeta':
                return subprocess.CompletedProcess(cmd, 0, json.dumps({'status':'ACTIVE','fileSize':1}), '')
            return self.cli(cmd, **kwargs)
        with patch.object(a.subprocess, 'run', side_effect=wrong_meta): reply = self.deliver()
        self.assertNotIn(URL, reply)
        self.assertFalse(json.loads((self.run/'reply-artifact.json').read_text())['verified'])

    def test_regular_text_no_network_and_fenced_svg_parses(self):
        with patch.object(a.subprocess, 'run') as cli:
            self.assertEqual(a.deliver_svg('普通回复', self.run, self.root, 'hi', 'folder'), '普通回复')
            cli.assert_not_called()
        self.assertEqual(a.svg_source('```svg\n'+SVG+'\n```')[0], SVG)

    def test_invalid_svg_is_not_uploaded(self):
        with patch.object(a.subprocess, 'run') as cli:
            reply = a.deliver_svg('<svg><broken></svg>', self.run, self.root, 'hi', 'folder')
            cli.assert_not_called()
        self.assertIn('尚未完成分享', reply)


if __name__ == '__main__': unittest.main()
