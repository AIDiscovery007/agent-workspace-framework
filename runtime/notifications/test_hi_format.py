import unittest
from hi_format import format_reply, normalize_body


class FormatTests(unittest.TestCase):
    def test_table_keeps_labels_units_empty_cells_and_escaped_pipes(self):
        text='| 数据组 | 条数（去重） | 备注 |\n|---|---:|---|\n| 乘风\\|v3 | 1,500 | |\n| 聚光 | 2,000 | 已校验 |'
        result=normalize_body(text)
        self.assertIn('**数据组：乘风|v3**',result)
        self.assertIn('• 条数（去重）：1,500',result)
        self.assertIn('• 备注：（空）',result)
        self.assertNotIn('|---',result)

    def test_table_link_and_code_pipe_are_not_split(self):
        text='| 项目 | 入口 |\n|---|---|\n| `a|b` | [数据](https://example.com/a?q=x|y#part) |'
        result=normalize_body(text)
        self.assertIn('项目：a|b',result)
        self.assertIn('[数据](https://example.com/a?q=x|y#part)',result)

    def test_named_links_preserved_and_naked_links_labeled(self):
        named='[标注平台](https://example.com/a_(b)?token=x%2Fy&x=1#part)'
        url='https://hi.xiaohongshu.com/clouddrive/dist/v1/f/abc?x=1&y=2#view'
        result=normalize_body(named+'\n'+url+'。')
        self.assertIn(named,result)
        self.assertIn('[打开 Hi 网盘文件]('+url+')。',result)
        escaped=r'[标注\[终版\]](https://example.com/a?x=1#view)'
        self.assertEqual(normalize_body(escaped),escaped)

    def test_local_path_not_disguised_as_download_link(self):
        result=normalize_body('[报告](/Users/a/报告.csv)')
        self.assertEqual(result,'报告（本机：/Users/a/报告.csv）')

    def test_code_fences_unchanged(self):
        code='```python\n# heading\nprint("https://example.com")\n\n\n# kept\n```'
        self.assertEqual(normalize_body(code),code)

    def test_raw_svg_is_protected_before_url_rewriting(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg">\n<title>鹈鹕</title>\n</svg>'
        result = normalize_body('请求人：星星\n\n'+svg)
        self.assertIn('```xml\n'+svg+'\n```', result)
        self.assertNotIn('打开链接', result)

    def test_large_svg_cannot_be_split_into_empty_markup_messages(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg">\n' + '<path d="M0 0L10 10"/>\n'*300 + '</svg>'
        parts = format_reply('请求人：星星\n\n'+svg, detail_hint='logs/result.txt')
        self.assertTrue(all(part.split('\n\n',1)[-1].strip() for part in parts))
        self.assertNotIn('<path', '\n'.join(parts))
        self.assertIn('完整内容', '\n'.join(parts))
        self.assertIn('logs/result.txt', '\n'.join(parts))

    def test_long_reply_no_truncated_link_or_missing_rows(self):
        lines=[f'第{i}项：'+('数据'*30)+f' [查看{i}](https://example.com/{i}?a=1&b=2#details)' for i in range(80)]
        result=format_reply('**检查结果**\n\n'+'\n\n'.join(lines))
        self.assertGreater(len(result),1)
        combined='\n'.join(result)
        for i in range(80): self.assertEqual(combined.count(f'https://example.com/{i}?a=1&b=2#details'),1)
        for part in result: self.assertLessEqual(len(part.encode()),2800)

    def test_oversized_atomic_link_has_explicit_local_reference(self):
        result=format_reply('[文件](https://example.com/'+('a'*5000)+')',detail_hint='.codex/hi-bridge/logs/test/result.txt')
        self.assertIn('完整内容',''.join(result))
        self.assertIn('.codex/hi-bridge/logs/test/result.txt',''.join(result))
        self.assertNotIn('https://example.com/',''.join(result))

    def test_title_and_status(self):
        result=format_reply('**笔记概览**\n\n共 11 个文件。')
        self.assertEqual(result,['✅ **笔记概览**\n\n共 11 个文件。'])
        self.assertTrue(format_reply('请核对结果。',status='error',title='任务中断')[0].startswith('⚠️ **任务中断**'))
        table='| 数据 | 条数 |\n|---|---|\n| A | 5 |'
        self.assertTrue(format_reply(table)[0].startswith('✅ **处理完成**'))


if __name__=='__main__': unittest.main()
