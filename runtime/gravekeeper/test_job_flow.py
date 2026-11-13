import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from notebook import Notebook
from test_notebook import event
import dispatch
import guardian_sessions as g
import job_flow as flow

class JobOwnershipTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.n=Notebook(temp.name);self.cfg={'enabled':True,'space':'space'}
        (self.n.base/'config.json').write_text(json.dumps(self.cfg))
        e=self.n.submit(event());self.n.assess(e['id'],{'decision':'accepted','note':'fixture'})
        self.topic=self.n.group({'signature':'fixture','title':'fixture','cause':'fixture','records':[e['id']]})['topic']
        self.n.triage(self.topic,{'priority':'P1','decision':'investigate','note':'fixture','next_step':'fix'})
        self.cycle=g.new_cycle(self.n,self.cfg)
        self.cycle.update(status='running',terminal='guard',session_id='01800000-0000-7000-8000-000000000001')
        g.save(self.n,self.cycle)
        env=patch.dict(os.environ,{'HC_TERMINAL_ID':'guard','CODEX_THREAD_ID':self.cycle['session_id']});env.start();self.addCleanup(env.stop)
        with patch.object(dispatch,'launch',return_value=('dev',True)):
            self.job=dispatch.develop(self.n,self.topic,{'problem':'fixture','scope':'fixture','acceptance':'fixture'})['id']
        self.work=self.n.base/'work'/self.job
        self.code=self.work/'code.py';self.code.write_text('value=1\n')
        self.delivery=self.work/'交付.md';self.delivery.write_text('候选代码，未测试')
        self.dev_session='01800000-0000-7000-8000-000000000002'
        with patch.object(flow,'require_developer',return_value=self.dev_session):dispatch.job_start(self.n,self.job,1)
        self.state={'state':'live','cwd':str(self.n.root),'labels':{'herdr.space':'space'},'status':{'state':'idle'},'foreground':{'argv':['codex',self.cycle['id']]}}
        self.plan={'plan':'实际执行断言','acceptance':'value=1','artifacts':[str(self.code.relative_to(self.n.root))]}
        self.spec={'action':'code','summary':'修复已测试验收','validation':'守墓人实际执行断言','artifacts':[str(self.delivery.relative_to(self.n.root))]}

    def ready(self,round_number=1):
        with patch.object(flow,'require_developer',return_value=self.dev_session):dispatch.job_ready(self.n,self.job,str(self.delivery),round_number)

    def complete_test(self,result='passed',round_number=1):
        self.ready(round_number);r=flow.start_test(self.n,self.job,round_number,self.plan)
        report=Path(r['report_directory'])/'测试报告.md';report.write_text('命令、退出码及实际结果：'+result)
        flow.test_ready(self.n,self.job,round_number,result,str(report));return report

    def test_finish_rejects_every_unaccepted_state(self):
        for status in ('launching','starting','running','ready','testing','tested','rework','unknown'):
            with self.n.db() as db:db.execute('UPDATE jobs SET status=?',(status,))
            with self.assertRaisesRegex(ValueError,'尚未验收'):self.n.finish(self.cycle['run_id'],'premature')
            with self.n.db() as db:self.assertEqual(db.execute('SELECT status FROM runs').fetchone()[0],'running')

    def test_idle_waiting_developer_never_archives_or_wakes(self):
        with patch.object(g,'owned_state',return_value=self.state),patch.object(g,'archive_cycle') as archive,patch.object(g,'call') as call:
            self.assertIn('waiting_for_jobs',g.advance(self.n,self.cycle));archive.assert_not_called();call.assert_not_called()

    def test_archive_rechecks_jobs_even_if_finish_was_incorrect(self):
        with self.n.db() as db:db.execute("UPDATE runs SET status='complete'")
        self.cycle['status']='archiving'
        with patch.object(g,'command') as command:
            self.assertIn('waiting_for_jobs',g.archive_cycle(self.n,self.cycle));command.assert_not_called()

    def test_ready_wakes_exact_same_guardian_once_and_ack_is_idempotent(self):
        self.ready()
        with patch.object(g,'owned_state',return_value=self.state),patch.object(g,'call',return_value={'started':True}) as call:
            first=g.advance(self.n,self.cycle);self.assertEqual(first['woken'],'guard');self.assertEqual(call.call_args.args[0][2],'guard')
            self.assertIn('waiting_for_acceptance',g.advance(self.n,g.active(self.n)));self.assertEqual(call.call_count,1)
        self.assertTrue(g.continue_cycle(self.n,self.cycle['id'],first['delivery'])['accepted'])
        self.assertFalse(g.continue_cycle(self.n,self.cycle['id'],first['delivery'])['accepted'])
        self.assertEqual(g.active(self.n)['session_id'],self.cycle['session_id'])

    def test_lost_delivery_retries_same_token_without_new_session(self):
        self.ready()
        with patch.object(g,'owned_state',return_value=self.state),patch.object(g,'call',side_effect=RuntimeError('lost')):
            with self.assertRaises(RuntimeError):g.advance(self.n,self.cycle)
        cycle=g.active(self.n);token=cycle['wake']['id'];cycle['wake']['sent_at']-=301;g.save(self.n,cycle)
        with patch.object(g,'owned_state',return_value=self.state),patch.object(g,'call',return_value={}):self.assertEqual(g.advance(self.n,cycle)['delivery'],token)

    def test_only_owner_can_test_reopen_resolve_or_finish(self):
        self.ready()
        with patch.dict(os.environ,{'HC_TERMINAL_ID':'dev','CODEX_THREAD_ID':self.dev_session}):
            for action in (lambda:flow.start_test(self.n,self.job,1,self.plan),lambda:dispatch.job_reopen(self.n,self.job,'self approve',1),lambda:self.n.resolve(self.topic,self.spec)):
                with self.assertRaises(ValueError):action()
        with self.assertRaisesRegex(ValueError,'测试未通过'):self.n.resolve(self.topic,self.spec)

    def test_passed_test_still_requires_guardian_acceptance_before_finish(self):
        self.complete_test()
        with self.assertRaises(ValueError):self.n.finish(self.cycle['run_id'],'not accepted')
        self.n.resolve(self.topic,self.spec);self.n.finish(self.cycle['run_id'],'tests and acceptance complete')
        with self.n.db() as db:self.assertFalse(self.n.open_jobs(db))

    def test_failed_test_rework_keeps_owner_and_requires_fresh_round(self):
        self.complete_test('failed')
        with self.assertRaises(ValueError):self.n.resolve(self.topic,self.spec)
        changed=dispatch.job_reopen(self.n,self.job,'assertion failed',1);self.assertEqual(changed['round'],2)
        with self.n.db() as db:self.assertEqual(db.execute('SELECT owner_run FROM jobs').fetchone()[0],self.cycle['run_id'])
        with self.assertRaises(ValueError):self.n.finish(self.cycle['run_id'],'waiting rework')
        with patch.object(flow,'require_developer',return_value=self.dev_session):dispatch.job_start(self.n,self.job,2)
        self.complete_test('passed',2);self.n.resolve(self.topic,self.spec);self.n.finish(self.cycle['run_id'],'done')

    def test_changed_code_or_report_cannot_reuse_passed_test(self):
        report=self.complete_test();original=self.code.read_text();self.code.write_text('value=2\n')
        with self.assertRaisesRegex(ValueError,'发生变化'):self.n.resolve(self.topic,self.spec)
        self.code.write_text(original);report.write_text('rewritten')
        with self.assertRaisesRegex(ValueError,'发生变化'):self.n.resolve(self.topic,self.spec)

    def test_dead_guardian_with_open_job_retains_session(self):
        with patch.object(g,'owned_state',return_value=None),patch.object(g,'archive_cycle') as archive:
            self.assertIn('needs_attention',g.advance(self.n,self.cycle));archive.assert_not_called()

    def test_busy_guardian_is_not_notified_or_replaced(self):
        self.ready();self.state['status']['state']='working'
        with patch.object(g,'owned_state',return_value=self.state),patch.object(g,'call') as call:
            self.assertIn('busy_or_blocked',g.advance(self.n,self.cycle));call.assert_not_called()

    def test_developer_registration_binds_terminal_session_and_high(self):
        info={'cwd':str(self.n.root),'archived':0,'reasoning_effort':'high'}
        with patch.dict(os.environ,{'HC_TERMINAL_ID':'dev','CODEX_THREAD_ID':self.dev_session}),patch.object(g,'session_info',return_value=info):
            self.assertEqual(flow.require_developer(self.n,'dev',self.dev_session),self.dev_session)
            with self.assertRaises(ValueError):flow.require_developer(self.n,'another',self.dev_session)
            with self.assertRaises(ValueError):flow.require_developer(self.n,'dev',self.cycle['session_id'])
        with patch.dict(os.environ,{'HC_TERMINAL_ID':'dev','CODEX_THREAD_ID':self.dev_session}),patch.object(g,'session_info',return_value=info|{'reasoning_effort':'medium'}):
            with self.assertRaises(ValueError):flow.require_developer(self.n,'dev')

    def test_one_accepted_job_does_not_close_other_unaccepted_jobs(self):
        self.complete_test();self.n.resolve(self.topic,self.spec)
        with self.n.db() as db:
            db.execute("INSERT INTO jobs(id,topic,status,owner_run) VALUES ('D-other','T-other','running',?)",(self.cycle['run_id'],))
        with self.assertRaises(ValueError):self.n.finish(self.cycle['run_id'],'another task is still running')

    def test_completion_must_follow_final_finish_not_earlier_waiting_reply(self):
        self.complete_test();self.n.resolve(self.topic,self.spec);self.n.finish(self.cycle['run_id'],'done')
        with self.n.db() as db:finished=db.execute('SELECT finished FROM runs').fetchone()[0]
        with patch.object(g,'owned_state',return_value=self.state),patch.object(g,'session_info',return_value={'cwd':str(self.n.root)}),patch.object(g,'transcript_complete',return_value=False) as complete:
            self.assertIn('waiting_for_turn_end',g.advance(self.n,self.cycle));self.assertEqual(complete.call_args.args[1],finished)

if __name__=='__main__':unittest.main()
