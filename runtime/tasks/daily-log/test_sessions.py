import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import session_runner as s


class SessionTests(unittest.TestCase):
    def test_startup_idle_waits_for_first_working_event(self):
        process = Mock()
        process.stdout.fileno.return_value = 1
        selector = Mock()
        selector.select.return_value = [(1, 1)]
        states = [
            {'state': 'live', 'turn': 0, 'status': {'state': 'idle'}},
            {'state': 'live', 'turn': 1, 'status': {'state': 'working'}},
        ]
        with patch.object(s.subprocess, 'Popen', return_value=process), \
             patch.object(s.selectors, 'DefaultSelector', return_value=selector), \
             patch.object(s.os, 'read', return_value=b'{"event":"status"}\n'), \
             patch.object(s, 'call', side_effect=states) as call:
            s.wait_started('owned')
        self.assertEqual(call.call_count, 2)
        selector.select.assert_called_once()
        process.terminate.assert_called_once()

    def test_blocked_start_is_not_accepted(self):
        with patch.object(s.subprocess, 'Popen'), patch.object(s.selectors, 'DefaultSelector'), \
             patch.object(s, 'call', return_value={'state': 'live', 'status': {'state': 'blocked'}}):
            with self.assertRaisesRegex(RuntimeError, '启动失败'):
                s.wait_started('owned')

    def test_only_unique_run_directory_identifies_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); run = base / 'run'; run.mkdir()
            sessions = base / 'sessions'; sessions.mkdir()
            wanted = '01800000-0000-7000-8000-000000000001'
            for name, cwd, ident in [('other', '/other', '01800000-0000-7000-8000-000000000002'), ('owned', str(run), wanted)]:
                (sessions / (name + '.jsonl')).write_text(json.dumps({'payload': {'id': ident, 'cwd': cwd}}) + '\n')
            (sessions / 'partial.jsonl').write_text('{')
            record = {'created_at': 0}
            with patch.object(s, 'SESSIONS', sessions):
                self.assertEqual(s.owned_rollout(run, record), sessions / 'owned.jsonl')
            self.assertEqual(record['session_id'], wanted)

    def test_wrong_terminal_directory_refuses_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp).resolve()
            s.save(run / 'agent.json', {'cwd': str(run), 'name': 'daily', 'space': 's', 'terminal': 't'})
            with patch.object(s, 'call', side_effect=[{'spaces': []}, {'state': 'live', 'cwd': '/other'}]) as call:
                with self.assertRaisesRegex(RuntimeError, '归属已变化'):
                    s.cleanup(run)
                self.assertFalse(any('kill' in x.args[0] or 'rm' in x.args[0] for x in call.call_args_list))

    def test_cleanup_failure_is_retried_without_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp); run = logs / 'one'; run.mkdir()
            s.save(run / 'agent.json', {'cleaned': False})
            with patch.object(s, 'cleanup', side_effect=RuntimeError('temporary cleanup failure')):
                with self.assertRaises(RuntimeError):
                    s.cleanup_pending(logs)
            self.assertTrue((run / 'cleanup-error.json').exists())
            with patch.object(s, 'cleanup') as cleanup:
                s.cleanup_pending(logs)
                cleanup.assert_called_once_with(run)

    def test_completed_cleanup_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp); s.save(run / 'agent.json', {'cleaned': True})
            with patch.object(s, 'call') as call:
                s.cleanup(run)
                call.assert_not_called()

    def test_final_completion_and_actual_high_are_separate_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'session.jsonl'
            events = [
                {'type': 'turn_context', 'payload': {'model': 'gpt-6-astra', 'effort': 'high'}},
                {'type': 'event_msg', 'payload': {'type': 'task_complete', 'last_agent_message': '{"summary":"ok","items":[]}'}}
            ]
            path.write_text('\n'.join(json.dumps(e) for e in events) + '\n{')
            self.assertEqual(json.loads(s.final_result(path))['items'], [])
            self.assertEqual(s.execution_settings(path)['effort'], 'high')


if __name__ == '__main__':
    unittest.main()
