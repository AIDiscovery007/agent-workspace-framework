import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('organizer', Path(__file__).with_name('run.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class OrganizeTests(unittest.TestCase):
    def test_scheduled_weekends_and_early_hours_do_not_run_or_notify(self):
        # Includes login/wake catch-up: weekend evenings must not execute.
        for day, hour, expected in ((13, 20, True), (14, 20, False), (15, 23, False), (16, 19, False), (16, 20, True)):
            now = m.dt.datetime(2026, 11, day, hour, tzinfo=m.TZ)
            with self.subTest(now=now), tempfile.TemporaryDirectory() as d:
                root = Path(d); task = root/'.codex/tasks/organize'; task.mkdir(parents=True)
                with patch.object(m,'ROOT',root), patch.object(m,'TASK',task), \
                     patch.object(m.dt,'datetime') as clock, \
                     patch.object(m,'plan_files',return_value={'moves':[],'skipped':[]}) as plan, \
                     patch.object(m,'notify') as send, patch('sys.argv',['run.py','--scheduled']):
                    clock.now.return_value = now
                    m.main()
                    self.assertEqual(plan.call_count, int(expected))
                    self.assertEqual(send.call_count, int(expected))

    def test_move_and_notification(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            task = root / '.codex/tasks/organize'
            task.mkdir(parents=True)
            source = root/'download.csv'
            source.write_text('业务,数据\n乘风,1\n')
            os.utime(source, (time.time()-3600, time.time()-3600))
            before = m.fingerprint(source)['sha256']
            plan = {'moves':[{'source':'download.csv', 'destination':'乘风/笔记.csv', 'reason':'内容包含乘风业务数据'}], 'skipped':[]}
            with patch.object(m, 'ROOT', root), patch.object(m, 'TASK', task), patch.object(m, 'plan_files', return_value=plan), patch.object(m, 'notify') as send, patch('sys.argv',['run.py']):
                m.main()
                self.assertFalse(source.exists())
                self.assertEqual(m.fingerprint(root/'乘风/笔记.csv')['sha256'], before)
                send.assert_called_once()
                self.assertTrue((task/'state.json').exists())

    def test_reject_unsafe_targets(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve()
            (root/'业务').mkdir()
            (root/'业务/已有.csv').write_text('已有')
            (root/'跳转').symlink_to(Path(d).parent, target_is_directory=True)
            for target in ['../越界.csv','SOP/文件.csv','.codex/文件.csv','业务/已有.csv','跳转/文件.csv','业务/文件.exe']:
                with self.subTest(target=target), patch.object(m,'ROOT',root):
                    with self.assertRaises(ValueError):
                        m.validate({'moves':[{'source':'a.csv','destination':target,'reason':'x'}], 'skipped':[]}, {'a.csv':{}})
            self.assertEqual((root/'业务/已有.csv').read_text(),'已有')

    def test_incomplete_plan_rejected(self):
        with self.assertRaises(ValueError):
            m.validate({'moves':[], 'skipped':[]}, {'a.csv':{}})

    def test_move_script_fix_path_and_deduplicate(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve(); run = root/'.log'; run.mkdir()
            (root/'业务').mkdir()
            (root/'input.csv').write_text('id,value\n1,2\n')
            (root/'业务/已有.csv').write_text('same')
            (root/'copy.csv').write_text('same')
            (root/'script.py').write_text("from pathlib import Path\nINPUT = Path('input.csv')\n")
            candidates = {n:m.fingerprint(root/n) for n in ['input.csv','copy.csv','script.py']}
            plan = {'moves':[
                {'source':'input.csv','destination':'业务/原始.csv','reason':'原始数据'},
                {'source':'script.py','destination':'业务/处理.py','reason':'同业务脚本'}],
                'duplicates':[{'source':'copy.csv','keep':'业务/已有.csv','reason':'SHA一致'}],
                'edits':[{'path':'script.py','sha256':candidates['script.py']['sha256'],'reason':'跟随数据',
                    'replacements':[{'old':"Path('input.csv')",'new':"Path(__file__).parent / '原始.csv'",'count':1}]}],
                'checks':['业务/原始.csv','业务/处理.py','业务/已有.csv'],'skipped':[]}
            report = {'moved':[],'skipped':[]}
            with patch.object(m,'ROOT',root):
                m.apply_plan(plan,candidates,run,report)
            self.assertTrue(all(not (root/n).exists() for n in candidates))
            self.assertEqual((root/'业务/已有.csv').read_text(),'same')
            self.assertEqual(m.fingerprint(root/'业务/原始.csv'),candidates['input.csv'])
            self.assertIn("Path(__file__).parent / '原始.csv'",(root/'业务/处理.py').read_text())
            self.assertNotIn('pending_batch',report)
            self.assertEqual(len(report['deduplicated']),1)
            self.assertEqual((run/'backups/0').read_text(),"from pathlib import Path\nINPUT = Path('input.csv')\n")

    def test_similar_is_not_duplicate_and_can_archive_as_variant(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve(); (root/'业务').mkdir()
            (root/'new.csv').write_text('id,score\n1,5\n2,4\n')
            (root/'业务/原始.csv').write_text('id,score\n1,5\n2,3\n')
            candidates={'new.csv':m.fingerprint(root/'new.csv')}
            with patch.object(m,'ROOT',root):
                with self.assertRaises(ValueError):
                    m.validate({'moves':[],'duplicates':[{'source':'new.csv','keep':'业务/原始.csv'}],'skipped':[]},candidates)
                m.validate({'moves':[{'source':'new.csv','destination':'业务/修订.csv'}],'skipped':[]},candidates)

    def test_stale_or_invalid_edits_and_missing_dependencies_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve(); (root/'业务').mkdir()
            script=root/'业务/a.py'; script.write_text("INPUT = 'a.csv'\n")
            edit={'path':'业务/a.py','sha256':m.fingerprint(script)['sha256'],'replacements':[{'old':'a.csv','new':'b.csv','count':1}]}
            with patch.object(m,'ROOT',root):
                for bad in [dict(edit,sha256='0'*64), dict(edit,replacements=[{'old':'missing','new':'x','count':1}]),dict(edit,replacements=[{'old':"'a.csv'",'new':'(', 'count':1}])]:
                    with self.subTest(edit=bad), self.assertRaises((ValueError,SyntaxError)):
                        m.validate({'moves':[],'skipped':[],'edits':[bad]}, {})
                with self.assertRaises(ValueError):
                    m.validate({'moves':[],'skipped':[],'checks':['业务/missing.csv']},{})

    def test_changed_source_never_deleted(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve(); run=root/'.log'; run.mkdir(); (root/'a.csv').write_text('before')
            candidates={'a.csv':m.fingerprint(root/'a.csv')}
            (root/'a.csv').write_text('after')
            with patch.object(m,'ROOT',root), self.assertRaises(RuntimeError):
                m.apply_plan({'moves':[{'source':'a.csv','destination':'业务/a.csv'}],'skipped':[]},candidates,run,{'moved':[]})
            self.assertEqual((root/'a.csv').read_text(),'after')
            self.assertFalse((root/'业务/a.csv').exists())

    def test_notice_lists_all_skipped_reasons(self):
        report={'moved':[],'skipped':[{'source':f'{i}.xlsx','reason':f'原因{i}'} for i in range(6)]}
        message=m.notification(report,m.dt.datetime.now(m.TZ))
        for item in report['skipped']:
            self.assertIn(item['source']+'：'+item['reason'],message)

    def test_interrupted_batch_retains_sources_and_blocks_next_run(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve(); task=root/'.codex/tasks/organize'; run=task/'logs/old'; run.mkdir(parents=True)
            (root/'a.csv').write_text('data'); (root/'script.py').write_text("INPUT = 'a.csv'\n")
            candidates={n:m.fingerprint(root/n) for n in ['a.csv','script.py']}
            plan={'moves':[{'source':'a.csv','destination':'业务/a.csv'}],
                  'skipped':[{'source':'script.py','reason':'原地修正引用'}],
                  'edits':[{'path':'script.py','sha256':candidates['script.py']['sha256'],'reason':'路径',
                            'replacements':[{'old':'a.csv','new':'业务/a.csv','count':1}]}]}
            report={'moved':[],'skipped':[]}
            with patch.object(m,'ROOT',root), patch.object(m,'TASK',task):
                with patch.object(m.shutil,'copymode',side_effect=OSError('模拟编辑中断')), self.assertRaises(OSError):
                    m.apply_plan(plan,candidates,run,report)
                self.assertTrue((root/'a.csv').exists())
                self.assertEqual((root/'script.py').read_text(),"INPUT = 'a.csv'\n")
                self.assertIn('pending_batch',json.loads((run/'report.json').read_text()))
                with patch.object(m,'plan_files') as planner, patch.object(m,'notify') as send, patch('sys.argv',['run.py','--dry-run']), self.assertRaises(RuntimeError):
                    m.main()
                planner.assert_not_called(); send.assert_not_called()

    def test_dry_run_does_not_edit_delete_or_notify(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve(); task=root/'.codex/tasks/organize'; task.mkdir(parents=True); (root/'业务').mkdir()
            for path in [root/'copy.csv',root/'业务/keep.csv']:
                path.write_text('data'); os.utime(path,(time.time()-3600,)*2)
            plan={'moves':[],'skipped':[],'duplicates':[{'source':'copy.csv','keep':'业务/keep.csv','reason':'一致'}]}
            with patch.object(m,'ROOT',root), patch.object(m,'TASK',task), patch.object(m,'plan_files',return_value=plan), patch.object(m,'notify') as send, patch('sys.argv',['run.py','--dry-run']):
                m.main()
            self.assertTrue((root/'copy.csv').exists()); send.assert_not_called()

class ReadOnlyDependencyTests(unittest.TestCase):
    TOOL = '.agents/skills/eval-data-funnel/scripts/analyze.py'

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.tool = self.root / self.TOOL
        self.tool.parent.mkdir(parents=True)
        # Existence checks must neither execute nor import the dependency.
        self.tool.write_text('raise RuntimeError("must never execute")\n')
        root_patch = patch.object(m, 'ROOT', self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        for name in ('notify', 'plan_files'):
            guard = patch.object(m, name, side_effect=AssertionError('unexpected ' + name))
            guard.start()
            self.addCleanup(guard.stop)

    def plan(self, checks):
        return {'moves': [], 'skipped': [], 'checks': checks}

    def test_exact_tool_dependency_preserves_all_checks(self):
        business = self.root / '业务/input.csv'
        business.parent.mkdir()
        business.write_text('data')
        checks = ['业务/input.csv', self.TOOL]
        plan = self.plan(checks.copy())
        m.validate(plan, {})
        self.assertEqual(plan['checks'], checks)
        self.assertEqual(m.safe_dependency(self.TOOL), self.tool)

    def test_readonly_tool_checked_after_fixture_move_edit_and_dedup(self):
        (self.root / '业务').mkdir()
        (self.root / 'a.csv').write_text('data')
        (self.root / 'copy.csv').write_text('same')
        (self.root / '业务/keep.csv').write_text('same')
        (self.root / 'script.py').write_text("INPUT = 'a.csv'\n")
        candidates = {n: m.fingerprint(self.root / n) for n in ('a.csv', 'copy.csv', 'script.py')}
        checks = ['业务/a.csv', '业务/script.py', '业务/keep.csv', self.TOOL]
        plan = {'moves': [{'source': n, 'destination': '业务/' + n} for n in ('a.csv', 'script.py')],
                'duplicates': [{'source': 'copy.csv', 'keep': '业务/keep.csv'}], 'skipped': [],
                'edits': [{'path': 'script.py', 'sha256': candidates['script.py']['sha256'],
                           'reason': 'fixture path', 'replacements': [
                               {'old': 'a.csv', 'new': '业务/a.csv', 'count': 1}]}], 'checks': checks.copy()}
        run = self.root / '.log'; run.mkdir()
        report = {'moved': [], 'skipped': []}
        tool_before = m.fingerprint(self.tool)
        with patch.object(m, 'safe_dependency', wraps=m.safe_dependency) as dependency:
            m.apply_plan(plan, candidates, run, report)
        self.assertEqual(dependency.call_count, len(checks) * 2)
        self.assertEqual(plan['checks'], checks)
        self.assertEqual(m.fingerprint(self.tool), tool_before)
        self.assertTrue(all(not (self.root / n).exists() for n in candidates))
        self.assertEqual((self.root / '业务/script.py').read_text(), "INPUT = '业务/a.csv'\n")
        self.assertNotIn('pending_batch', report)
        self.assertEqual((run / 'backups/0').read_text(), "INPUT = 'a.csv'\n")

    def test_business_future_removed_and_missing_dependencies(self):
        (self.root / 'a.csv').write_text('data')
        candidates = {'a.csv': m.fingerprint(self.root / 'a.csv')}
        plan = {'moves': [{'source': 'a.csv', 'destination': '业务/a.csv'}], 'skipped': [],
                'checks': ['业务/a.csv', self.TOOL]}
        m.validate(plan, candidates)
        for invalid in ('a.csv', '业务/missing.csv'):
            with self.subTest(path=invalid), self.assertRaisesRegex(ValueError, '整理后依赖不存在'):
                m.validate(dict(plan, checks=[invalid, self.TOOL]), candidates)
        (self.root / '业务').mkdir()
        (self.root / '业务/keep.csv').write_text('data')
        duplicate = {'moves': [], 'skipped': [], 'duplicates': [
            {'source': 'a.csv', 'keep': '业务/keep.csv'}], 'checks': ['a.csv', self.TOOL]}
        with self.assertRaisesRegex(ValueError, '整理后依赖不存在'):
            m.validate(duplicate, candidates)

    def test_nonallowlisted_hidden_and_unsafe_reads_rejected(self):
        for relative in ('.agents/skills/eval-data-funnel/scripts/other.py',
                         '.agents/skills/other/scripts/analyze.py', '.env',
                         '.codex/gravekeeper/state.json', '通知配置/webhooks.json',
                         '业务/.secret', '.agents/skills/eval-data-funnel/scripts/analyze.py.bak'):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('inert fixture')
            with self.subTest(path=relative), self.assertRaises(ValueError):
                m.validate(self.plan([relative]), {})
        for relative in (str(self.tool), '../outside.py', '业务/../input.csv',
                         './' + self.TOOL, self.TOOL + '/../analyze.py'):
            with self.subTest(path=relative), self.assertRaises(ValueError):
                m.validate(self.plan([relative]), {})

    def test_allowlisted_dependency_must_exist_as_regular_file(self):
        self.tool.unlink()
        with self.assertRaises(ValueError):
            m.validate(self.plan([self.TOOL]), {})
        self.tool.mkdir()
        with self.assertRaises(ValueError):
            m.validate(self.plan([self.TOOL]), {})
        self.tool.rmdir()
        os.mkfifo(self.tool)
        with self.assertRaises(ValueError):
            m.validate(self.plan([self.TOOL]), {})

    def test_file_and_each_parent_symlink_rejected(self):
        original = self.tool.read_bytes()
        self.tool.unlink()
        target = self.root / 'regular.py'; target.write_bytes(original)
        self.tool.symlink_to(target)
        with self.assertRaises(ValueError):
            m.validate(self.plan([self.TOOL]), {})
        self.tool.unlink(); self.tool.write_bytes(original)
        # Even links resolving inside ROOT must be rejected at every depth.
        for index, parent in enumerate(self.tool.parents):
            if parent == self.root:
                break
            relocated = self.root / ('relocated' + str(index))
            parent.rename(relocated)
            try:
                parent.symlink_to(relocated, target_is_directory=True)
                with self.subTest(parent=parent), self.assertRaises(ValueError):
                    m.validate(self.plan([self.TOOL]), {})
            finally:
                parent.unlink(); relocated.rename(parent)
        (self.root / 'alias.py').symlink_to(target)
        with self.assertRaises(ValueError):
            m.validate(self.plan(['alias.py']), {})
        outside = self.root.parent / 'nonexistent-outside.py'
        self.tool.unlink(); self.tool.symlink_to(outside)
        with self.assertRaises(ValueError):
            m.validate(self.plan([self.TOOL]), {})

    def test_whitelisted_path_never_grants_write_or_delete_access(self):
        original = self.tool.read_bytes()
        (self.root / 'source.py').write_bytes(original)
        (self.root / '业务').mkdir()
        (self.root / '业务/keep.py').write_bytes(original)
        candidates = {'source.py': m.fingerprint(self.root / 'source.py')}
        with self.assertRaises(ValueError):
            m.safe_file(self.TOOL)
        with self.assertRaises(ValueError):
            m.validate({'moves': [{'source': 'source.py', 'destination': self.TOOL}], 'skipped': []}, candidates)
        with self.assertRaises(ValueError):
            m.validate(dict(self.plan([]), edits=[{'path': self.TOOL}]), {})
        with self.assertRaises(ValueError):
            m.validate({'moves': [], 'skipped': [], 'duplicates': [
                {'source': 'source.py', 'keep': self.TOOL}]}, candidates)
        run = self.root / '.log'; run.mkdir()
        hidden_candidates = {self.TOOL: m.fingerprint(self.tool)}
        for plan in (
                {'moves': [{'source': self.TOOL, 'destination': '业务/moved.py'}], 'skipped': []},
                {'moves': [], 'skipped': [], 'duplicates': [{'source': self.TOOL, 'keep': '业务/keep.py'}]}):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                m.apply_plan(plan, hidden_candidates, run, {'moved': []})
        self.assertEqual(self.tool.read_bytes(), original)
        self.assertFalse((run / 'report.json').exists())
        self.assertFalse((self.root / '业务/moved.py').exists())
        # Reject even a currently nonexistent allowlisted write target.
        self.tool.unlink()
        with self.assertRaises(ValueError):
            m.validate({'moves': [{'source': 'source.py', 'destination': self.TOOL}], 'skipped': []}, candidates)

    def test_lost_dependency_after_staging_preserves_recovery_boundary(self):
        (self.root / 'a.csv').write_text('data')
        candidates = {'a.csv': m.fingerprint(self.root / 'a.csv')}
        plan = {'moves': [{'source': 'a.csv', 'destination': '业务/a.csv'}], 'skipped': [],
                'checks': ['业务/a.csv', self.TOOL]}
        run = self.root / '.log'; run.mkdir()
        link = os.link
        def remove_dependency_after_link(source, dest):
            link(source, dest)
            self.tool.unlink()
        report = {'moved': []}
        with patch.object(m.os, 'link', side_effect=remove_dependency_after_link), self.assertRaises(ValueError):
            m.apply_plan(plan, candidates, run, report)
        self.assertTrue((self.root / 'a.csv').is_file())
        self.assertIn('pending_batch', json.loads((run / 'report.json').read_text()))
        self.assertEqual(report['moved'], [])


if __name__ == '__main__':
    unittest.main()
