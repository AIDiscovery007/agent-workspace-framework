import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from notebook import Notebook
import dispatch
import guardian_sessions as g


class GuardianSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.n = Notebook(self.tmp.name)
        self.cfg = {'enabled': True, 'space': 'space'}
        (self.n.base / 'config.json').write_text(json.dumps(self.cfg))
        self.cycle = g.new_cycle(self.n, self.cfg)
        self.cycle.update(terminal='owned', session_id='01800000-0000-7000-8000-000000000001', status='running')
        g.save(self.n, self.cycle)
        env=patch.dict(os.environ,{'HC_TERMINAL_ID':'owned','CODEX_THREAD_ID':self.cycle['session_id']});env.start();self.addCleanup(env.stop)
        self.state = {'state': 'live', 'cwd': str(self.n.root), 'labels': {'herdr.space': 'space'},
                      'foreground': {'argv': ['codex', '--no-daemon', self.cycle['id']]},
                      'status': {'app': 'codex', 'state': 'idle'}}
        self.transcript = self.n.root / 'rollout.jsonl'; self.transcript.write_text('{}\n')
        self.info = {'id': self.cycle['session_id'], 'cwd': str(self.n.root), 'model': 'gpt-6-astra',
                     'reasoning_effort': 'xhigh', 'approval_mode': 'never', 'sandbox_policy': '{"type":"disabled"}', 'archived': 0, 'rollout_path': str(self.transcript)}

    def test_startup_idle_without_registration_does_not_archive(self):
        self.cycle.pop('session_id')
        with patch.object(g, 'owned_state', return_value=self.state), patch.object(g, 'archive_cycle') as archive:
            self.assertIn('starting', g.advance(self.n, self.cycle))
            archive.assert_not_called()

    def test_finish_alone_waits_for_final_turn(self):
        self.n.finish(self.cycle['run_id'], 'done')
        with patch.object(g, 'owned_state', return_value=self.state), patch.object(g, 'session_info', return_value=self.info), \
             patch.object(g, 'transcript_complete', return_value=False), patch.object(g, 'archive_cycle') as archive:
            self.assertIn('waiting_for_turn_end', g.advance(self.n, self.cycle))
            archive.assert_not_called()

    def test_final_without_ledger_finish_retains_attention(self):
        with patch.object(g, 'owned_state', return_value=self.state), patch.object(g, 'session_info', return_value=self.info), \
             patch.object(g, 'archive_cycle') as archive:
            self.assertIn('needs_attention', g.advance(self.n, self.cycle))
            archive.assert_not_called()

    def test_busy_and_blocked_never_archive(self):
        for status in ('working', 'blocked', 'error'):
            self.state['status']['state'] = status
            with patch.object(g, 'owned_state', return_value=self.state), patch.object(g, 'command') as command:
                self.assertIn('busy_or_blocked', g.advance(self.n, self.cycle))
                self.assertIn('busy_or_blocked', g.archive_cycle(self.n, self.cycle))
                command.assert_not_called()

    def test_completed_cycle_archives_even_when_no_new_revision(self):
        self.n.finish(self.cycle['run_id'], 'done')
        with patch.object(g, 'advance', return_value={'archived': self.cycle['session_id']}) as advance:
            self.assertIn('archived', dispatch.tick(self.n))
            advance.assert_called_once()

    def test_registration_binds_own_terminal_and_actual_xhigh(self):
        self.cycle.pop('session_id'); g.save(self.n, self.cycle)
        with patch.dict(os.environ, {'HC_TERMINAL_ID': 'other', 'CODEX_THREAD_ID': self.info['id']}):
            with self.assertRaises(ValueError): g.register(self.n, self.cycle['id'])
        with patch.dict(os.environ, {'HC_TERMINAL_ID': 'owned', 'CODEX_THREAD_ID': self.info['id']}), \
             patch.object(g, 'session_info', return_value=self.info | {'reasoning_effort': 'high'}):
            with self.assertRaises(ValueError): g.register(self.n, self.cycle['id'])
        with patch.dict(os.environ, {'HC_TERMINAL_ID': 'owned', 'CODEX_THREAD_ID': self.info['id']}), \
             patch.object(g, 'session_info', return_value=self.info):
            self.assertTrue(g.register(self.n, self.cycle['id'])['accepted'])

    def test_registration_rejects_interactive_or_sandboxed_execution(self):
        for change in ({'approval_mode': 'on-request'}, {'sandbox_policy': '{"type":"workspace-write"}'}):
            with patch.dict(os.environ, {'HC_TERMINAL_ID': 'owned', 'CODEX_THREAD_ID': self.info['id']}), \
                 patch.object(g, 'session_info', return_value=self.info | change):
                with self.assertRaises(ValueError): g.register(self.n, self.cycle['id'])

    def test_wrong_foreground_or_directory_never_touched(self):
        for change in ({'cwd': '/other'}, {'foreground': {'argv': ['codex', 'unrelated']}}):
            with patch.object(g, 'call', return_value=self.state | change), patch.object(g, 'command') as command:
                with self.assertRaises(RuntimeError): g.archive_cycle(self.n, self.cycle)
                command.assert_not_called()

    def test_archive_failure_keeps_active_cycle_for_retry(self):
        self.n.finish(self.cycle['run_id'], 'done')
        with patch.object(g, 'owned_state', return_value=self.state), patch.object(g, 'session_info', return_value=self.info), \
             patch.object(g, 'transcript_complete', return_value=True), patch.object(g, 'archive_cycle', side_effect=RuntimeError('archive failed')):
            with self.assertRaises(RuntimeError): g.advance(self.n, self.cycle)
        self.assertEqual(g.active(self.n)['status'], 'archiving')
        self.assertEqual(g.new_cycle(self.n, self.cfg)['id'], self.cycle['id'])

    def test_native_archive_keeps_history_and_clears_only_own_pointer(self):
        self.n.finish(self.cycle['run_id'], 'done')
        self.cycle['status'] = 'archiving'; g.save(self.n, self.cycle)
        with self.n.db() as db: before = self.n.meta(db, 'reviewed')
        with patch.object(g, 'owned_state', return_value=self.state), \
             patch.object(g, 'session_info', side_effect=[self.info, self.info | {'archived': 1}]), \
             patch.object(g, 'command') as command, patch.object(g, 'call') as call:
            def response(argv, **kwargs):
                if 'ls' in argv: return {'spaces': []}
                if 'get' in argv: return None
                return {}
            call.side_effect = response
            result = g.archive_cycle(self.n, self.cycle)
            self.assertEqual(result['archived'], self.cycle['session_id'])
            command.assert_called_once_with([g.BIN / 'codex', 'archive', self.cycle['session_id']], timeout=90)
        self.assertTrue(self.transcript.exists())
        self.assertIsNone(g.active(self.n))
        with self.n.db() as db: self.assertEqual(self.n.meta(db, 'reviewed'), before)
        new = g.new_cycle(self.n, self.cfg)
        self.assertNotEqual(new['id'], self.cycle['id'])
        self.assertNotEqual(new['run_id'], self.cycle['run_id'])

    def test_interrupted_session_never_marks_unfinished_run_complete(self):
        with patch.object(g, 'owned_state', return_value=None), patch.object(g, 'session_info', return_value=self.info), \
             patch.object(g, 'archive_cycle', return_value={'archived': self.cycle['session_id']}):
            g.advance(self.n, self.cycle)
        self.assertEqual(self.cycle['outcome'], 'interrupted')
        with self.n.db() as db:
            self.assertEqual(db.execute('SELECT status FROM runs WHERE id=?', (self.cycle['run_id'],)).fetchone()[0], 'running')


if __name__ == '__main__': unittest.main()
