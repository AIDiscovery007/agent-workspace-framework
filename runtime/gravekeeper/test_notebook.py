from datetime import datetime, timezone
import concurrent.futures
import json
import fcntl
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from notebook import Notebook, validate
import dispatch
import job_flow

def event(key='test:task1:parse'):
    return dict(incident_key=key,title='测试夹具：输入解析失败',agent='test',session='fixture',business='验收夹具',
                occurred_at=datetime.now(timezone.utc).isoformat(),trigger='输入空字符串',expected='返回空列表',actual='抛出异常',
                scope=dict(domain='governance',component='.codex/gravekeeper/fixture.py',causal_evidence='隔离夹具中共享解析器对空输入抛出异常',reusable_reason='所有调用该解析器的任务均受影响'),
                evidence=['隔离测试夹具，仅测试事件，非真实业务次数'],attempts='未重试',conclusion='待核实',impact='测试输入失败')

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.n=Notebook(self.tmp.name)
        guard=patch.object(Notebook,'require_guardian',return_value={'run_id':'fixture-run','session_id':'guardian-fixture'})
        guard.start();self.addCleanup(guard.stop)
        worker=patch.object(job_flow,'require_developer',return_value='developer-fixture')
        worker.start();self.addCleanup(worker.stop)

    def accepted(self, data):
        r=self.n.submit(data)
        self.n.assess(r['id'],dict(decision='accepted',note='隔离测试已核实共享解析器因果证据'))
        return r

    def grouped(self,count=3,triaged=True):
        ids=[self.accepted(event('test:task'+str(i)+':parse'))['id'] for i in range(count)]
        spec=dict(signature='fixture/empty/parse',title='空输入解析',cause='三个独立测试输入触发同一分支',records=ids)
        t=self.n.group(spec)
        if triaged:self.n.triage(t['topic'],dict(priority='P1',decision='investigate',note='隔离夹具关键流程失败',next_step='复现定位并回归'))
        return self.n.group(spec),ids,spec

    def resolved(self):
        t,ids,s=self.grouped();(self.n.root/'fix.md').write_text('真实测试产物')
        self.n.resolve(t['topic'],dict(action='sop',summary='步骤已修正',validation='隔离夹具已验证',artifacts=['fix.md']))
        return t,ids,s

    def test_required_and_secret_validation(self):
        for change in ({'evidence':[]},{'occurred_at':'2026-11-10T10:00:00'},{'trigger':''},{'evidence':['https://example.test/?token=abcdefghijk123']}):
            with self.assertRaises(ValueError):validate(event()|change)

    def test_duplicate_and_concurrent(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            result=list(pool.map(lambda _:self.n.submit(event()),range(12)))
        self.assertEqual(len({r['id'] for r in result}),1)
        self.assertEqual(sum(not r['duplicate'] for r in result),1)

    def test_retry_amend_not_new_occurrence(self):
        ident=self.accepted(event())['id'];self.n.amend(ident,'重试两次仍失败')
        t=self.n.group(dict(signature='one',title='一次事件',cause='重试属于同一次',records=[ident,ident,ident]))
        self.assertEqual(t['count'],1);self.assertFalse(t['eligible'])

    def test_priority_and_double_group(self):
        t,ids,s=self.grouped(1,triaged=False);self.assertFalse(t['eligible'])
        self.n.triage(t['topic'],dict(priority='P1',decision='investigate',note='关键流程失败',next_step='定位'))
        ident=self.accepted(event('test:third:parse'))['id']
        t=self.n.group(s|{'records':[ident]});self.assertTrue(t['eligible'])
        with self.assertRaises(ValueError):self.n.group(s|{'signature':'other'})

    def test_resolve_requires_triage_and_artifact(self):
        t,_,_=self.grouped(1,triaged=False);(self.n.root/'fix').write_text('ok')
        spec=dict(action='code',summary='完成',validation='通过',artifacts=['fix'])
        with self.assertRaises(ValueError):self.n.resolve(t['topic'],spec)
        with self.assertRaises(ValueError):self.n.resolve(t['topic'],spec|{'artifacts':['missing']})

    def test_run_snapshot_does_not_drop_new_events(self):
        self.n.submit(event());run=self.n.begin();self.assertEqual(self.n.begin()['id'],run['id'])
        self.n.submit(event('new'));self.n.finish(run['id'],'完成快照')
        p=self.n.pending();self.assertGreater(int(p['revision']),int(p['reviewed']))

    def test_archive_keeps_open_and_recent_topics(self):
        t,ids,s=self.grouped();self.assertFalse(self.n.archive(time.time()+90*86400)['archived'])
        (self.n.root/'fix').write_text('ok')
        self.n.resolve(t['topic'],dict(action='code',summary='修复',validation='测试',artifacts=['fix']))
        self.assertFalse(self.n.archive(time.time()+29*86400)['archived'])

    def test_archive_roundtrip_and_tombstone(self):
        t,ids,s=self.resolved();a=self.n.archive(time.time()+31*86400)
        self.assertEqual(a['archived'],[t['topic']])
        with self.n.db() as c:
            row=c.execute('SELECT * FROM incidents WHERE id=?',(ids[0],)).fetchone()
            self.assertEqual(row['payload'],'{}');self.assertEqual(self.n.payload(row)['title'],event()['title'])
        self.assertIn('已归档',(self.n.book/'记录'/f'{ids[0]}.md').read_text())
        self.assertFalse(self.n.archive(time.time()+32*86400)['archived'])

    def test_archive_checksum_detects_tampering(self):
        t,ids,s=self.resolved();self.n.archive(time.time()+31*86400)
        with self.n.db() as c:
            row=c.execute('SELECT * FROM incidents WHERE id=?',(ids[0],)).fetchone()
            (self.n.root/row['archive']).write_bytes(b'invalid')
            with self.assertRaises(Exception):self.n.payload(row)

    def test_recurrence_reopens_topic(self):
        t,ids,s=self.resolved();self.n.archive(time.time()+31*86400)
        ident=self.accepted(event('recurrence'))['id'];self.n.group(s|{'records':[ident]})
        with self.n.db() as c:
            row=c.execute('SELECT * FROM topics WHERE id=?',(t['topic'],)).fetchone()
            self.assertEqual(row['status'],'open');self.assertIsNone(row['closed'])
        self.assertEqual(self.n.group(s|{'records':[ident]})['count'],4)

    def test_amend_archived_record_roundtrip(self):
        t,ids,s=self.resolved();self.n.archive(time.time()+31*86400);self.n.amend(ids[0],'更正证据')
        with self.n.db() as c:
            row=c.execute('SELECT * FROM incidents WHERE id=?',(ids[0],)).fetchone()
            self.assertIsNone(row['archive']);self.assertIn('更正证据',self.n.payload(row)['attempts'])

    def test_no_eligible_no_developer(self):
        t,_,_=self.grouped(5,triaged=False)
        with patch.object(dispatch,'launch') as launch:
            with self.assertRaises(ValueError):dispatch.develop(self.n,t['topic'],dict(problem='问题',scope='文件',acceptance='测试'))
            launch.assert_not_called()

    def test_developer_dedup_and_ready_wake(self):
        t,_,_=self.grouped(1)
        spec=dict(problem='问题',scope='隔离夹具',acceptance='测试')
        with patch.object(dispatch,'launch',return_value=('t-fixture',True)) as launch:
            a=dispatch.develop(self.n,t['topic'],spec);b=dispatch.develop(self.n,t['topic'],spec)
            self.assertEqual(a['id'],b['id']);launch.assert_called_once()
        path=self.n.base/'work'/a['id']/'交付.md';path.write_text('测试交付')
        dispatch.job_start(self.n,a['id'],1)
        result=dispatch.job_ready(self.n,a['id'],str(path),1);self.assertEqual(result['status'],'ready')
        self.assertTrue(dispatch.job_ready(self.n,a['id'],str(path),1)['duplicate'])

    def test_unknown_time_is_explicit_not_fabricated(self):
        with self.assertRaises(ValueError):validate(event()|{'occurred_at':'unknown'})
        self.assertEqual(validate(event()|{'occurred_at':'unknown','time_note':'历史 session 无精确时间'})['time_note'],'历史 session 无精确时间')

    def test_amend_regroup_clears_unread_without_increasing_count(self):
        t,ids,s=self.grouped();self.n.amend(ids[0],'补充证据');self.n.group(s)
        self.assertEqual(len(self.n.pending()['records']),0)
        self.assertEqual(self.n.group(s)['count'],3)

    def test_archived_group_replay_is_noop_but_changed_cause_reopens(self):
        t,ids,s=self.resolved();self.n.archive(time.time()+31*86400);self.n.group(s)
        with self.n.db() as c:
            self.assertTrue(c.execute('SELECT archive FROM incidents WHERE id=?',(ids[0],)).fetchone()[0])
        self.n.group(s|{'cause':'新增根因证据'})
        with self.n.db() as c:
            row=c.execute('SELECT * FROM topics WHERE id=?',(t['topic'],)).fetchone()
            self.assertEqual(row['status'],'open');self.assertEqual(row['cause'],'新增根因证据')

    def test_job_ack_and_rework_generation(self):
        t,_,_=self.grouped()
        with patch.object(dispatch,'launch',return_value=('test-terminal',True)):
            d=dispatch.develop(self.n,t['topic'],dict(problem='a',scope='b',acceptance='c'))
        with self.n.db() as c:self.assertEqual(c.execute('SELECT status FROM jobs').fetchone()[0],'starting')
        self.assertTrue(dispatch.job_start(self.n,d['id'],1)['accepted'])
        self.assertFalse(dispatch.job_start(self.n,d['id'],1)['accepted'])
        report=self.n.base/'work'/d['id']/'交付.md';report.write_text('第一轮')
        dispatch.job_ready(self.n,d['id'],str(report),1)
        r=dispatch.job_reopen(self.n,d['id'],'返工',1);self.assertEqual(r['round'],2)
        dispatch.job_start(self.n,d['id'],2);report.write_text('第二轮')
        dispatch.job_ready(self.n,d['id'],str(report),2)
        r=dispatch.job_reopen(self.n,d['id'],'重放上一轮',1);self.assertEqual(r['status'],'ready')
        with self.assertRaises(ValueError):dispatch.job_ready(self.n,d['id'],str(report),1)

    def tick_setup(self):
        (self.n.base/'config.json').write_text(json.dumps({'enabled':True,'space':'s1'}))
        return {'state':'live','cwd':str(self.n.root),'labels':{'herdr.space':'s1'},'status':{'app':'codex','state':'idle'}}

    def test_pending_tail_starts_fresh_cycle_despite_equal_revision(self):
        self.tick_setup();self.n.submit(event());r=self.n.begin();self.n.finish(r['id'],'本轮未读完')
        with patch.object(dispatch,'launch',return_value=('fresh-t',True)) as launch:
            result=dispatch.tick(self.n)
        self.assertEqual(result['launched'],'fresh-t');launch.assert_called_once()
        self.assertNotEqual(dispatch.guardian_sessions.active(self.n)['run_id'],r['id'])

    def test_uncertain_creation_keeps_cycle_and_never_sends_again(self):
        self.tick_setup();self.n.submit(event())
        with patch.object(dispatch,'launch',side_effect=RuntimeError('reply lost')):
            with self.assertRaises(RuntimeError):dispatch.tick(self.n)
        cycle=dispatch.guardian_sessions.active(self.n)
        with patch.object(dispatch,'launch',return_value=('existing-owned-t',False)) as launch:
            result=dispatch.tick(self.n)
        self.assertEqual(result['cycle'],cycle['id'])
        self.assertFalse(result['created'])
        self.assertEqual(launch.call_args.args[1],cycle['name'])

    def test_legacy_terminal_wrong_project_never_receives_message(self):
        self.tick_setup();self.n.begin()
        with self.n.db() as c:
            c.execute("INSERT INTO meta VALUES ('guardian_terminal','test-t')")
            c.execute("INSERT INTO meta VALUES ('guardian_session','01800000-0000-7000-8000-000000000001')")
        state={'state':'live','cwd':'/another/project','labels':{'herdr.space':'s1'}}
        with patch.object(dispatch.guardian_sessions,'call',return_value=state) as call:
            with self.assertRaisesRegex(RuntimeError,'归属不匹配'):dispatch.tick(self.n)
            self.assertEqual(call.call_count,1)

    def test_one_shot_delivery_waits_for_lock_and_is_persisted(self):
        t,_,_=self.grouped()
        with patch.object(dispatch,'launch',return_value=('test-terminal',True)):
            d=dispatch.develop(self.n,t['topic'],dict(problem='a',scope='b',acceptance='c'))
        dispatch.job_start(self.n,d['id'],1)
        report=self.n.base/'work'/d['id']/'交付.md';report.write_text('交付')
        child_script=("import sys; sys.path.insert(0,"+repr(str(Path(dispatch.__file__).resolve().parent))+"); from unittest.mock import patch; import dispatch,job_flow; patch.object(job_flow,'require_developer',return_value='developer-fixture').start(); dispatch.main()")
        with open(self.n.base/'dispatch.lock','w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            p=subprocess.Popen([sys.executable,'-c',child_script,'--root',str(self.n.root),'job-ready',d['id'],'--round','1','--report',str(report)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                time.sleep(.2)
                self.assertIsNone(p.poll(),'不应把锁竞争当成功并丢弃请求')
                fcntl.flock(lock,fcntl.LOCK_UN)
                stdout,stderr=p.communicate(timeout=5)
                self.assertEqual(p.returncode,0,stderr)
                self.assertEqual(json.loads(stdout)['status'],'ready')
            finally:
                if p.poll() is None:p.kill()
                p.communicate()

    def test_start_delivery_recovery_requires_ack(self):
        t,_,_=self.grouped()
        with patch.object(dispatch,'launch',return_value=('test-terminal',True)):
            d=dispatch.develop(self.n,t['topic'],dict(problem='a',scope='b',acceptance='c'))
        with self.n.db() as c:c.execute('UPDATE jobs SET created=?',(time.time()-120,))
        state={'state':'live','cwd':str(self.n.root),'status':{'app':'codex','state':'idle'}}
        with patch.object(dispatch,'call',side_effect=[state,{'started':True}]) as call:
            dispatch.recover_jobs(self.n);self.assertEqual(call.call_count,2)
        with self.n.db() as c:self.assertEqual(c.execute('SELECT status FROM jobs').fetchone()[0],'starting')
        dispatch.job_start(self.n,d['id'],1)
        with patch.object(dispatch,'call') as call:dispatch.recover_jobs(self.n);call.assert_not_called()

    def test_executor_effort_and_permissions_are_explicit(self):
        (self.n.base/'config.json').write_text('{"model":"test-model"}')
        for role,effort in [('developer','high'),('execution','medium')]:
            argv=dispatch.launch_argv(self.n,role=role)
            self.assertIn(f'model_reasoning_effort="{effort}"',argv)
            self.assertEqual(argv[argv.index('-s')+1],'danger-full-access')
            self.assertEqual(argv[argv.index('-a')+1],'never')
            self.assertIn('--no-daemon',argv)
        self.assertIn('model_reasoning_effort="xhigh"',dispatch.launch_argv(self.n,effort='low'))
        guardian=dispatch.launch_argv(self.n)
        self.assertIn('--dangerously-bypass-approvals-and-sandbox',guardian)
        self.assertIn('--no-daemon',guardian)

    def test_search_includes_reviewed_and_cold_archive(self):
        t,ids,s=self.resolved();self.n.archive(time.time()+31*86400)
        found=self.n.search('空输入')
        self.assertEqual(found['record_matches'],3)
        self.assertEqual(found['topics'][0]['id'],t['topic'])
        self.assertTrue(all(r['archive'] for r in found['records']))


    def test_scope_required_and_business_scope_rejected_without_growth(self):
        for scope in (None, {}, dict(domain='business'), dict(domain='bridge',component='x')):
            with self.assertRaises(ValueError):self.n.submit(event()|{'scope':scope})
        with self.n.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM incidents').fetchone()[0],0)
            self.assertEqual(self.n.meta(c,'revision'),'0')

    def test_candidates_cannot_group_even_with_three_occurrences(self):
        ids=[self.n.submit(event('candidate:'+str(i)))['id'] for i in range(3)]
        with self.assertRaises(ValueError):self.n.group(dict(signature='blocked',title='阻断',cause='证据',records=ids))
        self.assertEqual(len(self.n.pending()['records']),3)
        with self.n.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM topics').fetchone()[0],0)

    def test_repeated_scope_review_does_not_wake_or_reopen(self):
        ident=self.n.submit(event())['id'];spec=dict(decision='excluded',note='业务个案')
        self.n.assess(ident,spec)
        before=self.n.pending()['revision']
        self.assertTrue(self.n.assess(ident,spec)['unchanged'])
        self.assertEqual(self.n.pending()['revision'],before)

    def test_exclusion_removes_topic_and_threshold_but_preserves_history(self):
        t,ids,s=self.grouped()
        for ident in ids:self.n.assess(ident,dict(decision='excluded',note='复核为单次业务脚本问题，无共享机制因果证据'))
        self.assertFalse(self.n.pending()['records']);self.assertFalse(self.n.pending()['topics'])
        self.assertNotIn('空输入解析',(self.n.book/'索引.md').read_text())
        self.assertEqual(self.n.search('空输入')['record_matches'],3)
        self.n.amend(ids[0],'补充业务处置，不改变范围判断')
        self.assertFalse(self.n.pending()['records'])
        with patch.object(dispatch,'launch') as launch:
            with self.assertRaises(ValueError):dispatch.develop(self.n,t['topic'],dict(problem='a',scope='b',acceptance='c'))
            launch.assert_not_called()
        (self.n.root/'fix').write_text('x')
        with self.assertRaises(ValueError):self.n.resolve(t['topic'],dict(action='sop',summary='a',validation='b',artifacts=['fix']))
        # New causal evidence may restore the original record, never duplicate it.
        self.n.assess(ids[0],dict(decision='accepted',note='新增证据已证实共享组件缺陷'))
        self.assertEqual(self.n.group(s|{'records':[ids[0]]})['count'],1)
        self.assertTrue(self.n.submit(event('test:task0:parse'))['duplicate'])

    def test_mixed_topic_counts_only_accepted(self):
        t,ids,s=self.grouped()
        self.n.assess(ids[0],dict(decision='excluded',note='此事件实际不同源且属业务个案'))
        r=self.n.group(s|{'records':ids[1:]})
        self.assertEqual(r['count'],2);self.assertTrue(r['eligible'])
        self.n.triage(t['topic'],dict(priority='P3',decision='defer',note='轻微有绕行',next_step='复查',revisit_at='2099-01-01T00:00:00+08:00'))
        with patch.object(dispatch,'launch') as launch:
            with self.assertRaises(ValueError):dispatch.develop(self.n,t['topic'],dict(problem='a',scope='b',acceptance='c'))
            launch.assert_not_called()

    def test_exclusion_does_not_silently_cancel_running_job(self):
        t,ids,s=self.grouped()
        with patch.object(dispatch,'launch',return_value=('t-fixture',True)):
            dispatch.develop(self.n,t['topic'],dict(problem='a',scope='b',acceptance='c'))
        with self.assertRaises(ValueError):self.n.assess(ids[0],dict(decision='excluded',note='需先处理现有开发'))

    def test_excluded_records_do_not_wake_when_revision_reviewed(self):
        self.tick_setup();ident=self.n.submit(event())['id']
        self.n.assess(ident,dict(decision='excluded',note='非体系问题'))
        r=self.n.begin();self.n.finish(r['id'],'已检查范围')
        with patch.object(dispatch,'call') as call:
            self.assertTrue(dispatch.tick(self.n)['idle']);call.assert_not_called()

    def test_legacy_records_need_review_and_remain_readable(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as root:
            state=Path(root)/'错题本/.state';state.mkdir(parents=True)
            c=sqlite3.connect(state/'state.sqlite3')
            c.execute('CREATE TABLE incidents(id TEXT PRIMARY KEY, incident_key TEXT UNIQUE NOT NULL, title TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,topic TEXT,review_note TEXT,archive TEXT,digest TEXT)')
            old=event();del old['scope']
            c.execute('INSERT INTO incidents VALUES (?,?,?,?,?,?,NULL,?,NULL,NULL)',('E-old','old','旧问题',json.dumps(old),time.time(),time.time(),'旧版已审核'))
            c.commit();c.close()
            n=Notebook(root);self.assertEqual(n.pending()['records'][0]['admission'],'candidate')
            n.amend('E-old','保留旧格式可补充')
            with self.assertRaises(ValueError):n.assess('E-old',dict(decision='accepted',note='没有补齐因果依据'))
            n.assess('E-old',dict(decision='excluded',note='旧记录无体系证据'))
            self.assertFalse(n.pending()['records'])

    def test_deferred_due_wakes_and_high_priority_cannot_defer(self):
        state=self.tick_setup();t,_,_=self.grouped(1)
        spec=dict(priority='P1',decision='defer',note='等待',next_step='复查',revisit_at='2099-01-01T00:00:00+08:00')
        with self.assertRaises(ValueError):self.n.triage(t['topic'],spec)
        self.n.triage(t['topic'],spec|{'priority':'P2'})
        r=self.n.begin();self.n.finish(r['id'],'暂缓')
        with patch.object(dispatch,'call') as call:self.assertTrue(dispatch.tick(self.n)['idle']);call.assert_not_called()
        with self.n.db() as c:c.execute('UPDATE topics SET revisit_at=?',(time.time()-1,))
        with patch.object(dispatch,'launch',return_value=('fresh-t',True)):self.assertEqual(dispatch.tick(self.n)['launched'],'fresh-t')

    def test_observed_login_failure_does_not_require_root_cause(self):
        data=event();data['scope'].pop('causal_evidence');data['scope']['observed_evidence']='已授权任务无法识别成员登录信息，根因待定位'
        ident=self.n.submit(data)['id']
        self.assertEqual(self.n.pending()['records'][0]['admission'],'candidate')
        self.n.assess(ident,dict(decision='accepted',note='已核实身份识别环节失败，调查根因'))
        t=self.n.group(dict(signature='identity',title='识别失败',cause='根因待定位',records=[ident]))
        self.n.triage(t['topic'],dict(priority='P1',decision='investigate',note='核心任务失败无绕行',next_step='调查识别链路'))
        (self.n.root/'fix').write_text('实际修复及回归证据')
        self.assertEqual(self.n.resolve(t['topic'],dict(action='code',summary='已修复',validation='回归通过',artifacts=['fix']))['status'],'verified')

if __name__=='__main__':unittest.main()
