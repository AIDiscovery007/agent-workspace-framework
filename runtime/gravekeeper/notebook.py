#!/usr/bin/env python3
"""Shared incident ledger. SQLite is authoritative; Markdown is a rebuildable view."""
import argparse
import contextlib
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
FIELDS = ('incident_key', 'title', 'agent', 'session', 'business', 'occurred_at',
          'trigger', 'expected', 'actual', 'attempts', 'conclusion', 'impact')

def dump(x):
    return json.dumps(x, ensure_ascii=False, indent=2)

def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with open(tmp, 'wb') as f:
            f.write(value if isinstance(value, bytes) else value.encode())
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

def validate_scope(scope):
    if not isinstance(scope, dict) or scope.get('domain') not in ('instructions','sop','skill','bridge','automation','governance'):
        raise ValueError('scope.domain 必须是 instructions/sop/skill/bridge/automation/governance；业务个案和外部故障不收录')
    scope = dict(scope)
    if 'observed_evidence' in scope:
        scope['causal_evidence'] = scope['observed_evidence']
    for key in ('component','causal_evidence','reusable_reason'):
        if not isinstance(scope.get(key),str) or not scope[key].strip() or len(scope[key])>2000:
            raise ValueError('必须提供 scope.'+key+'：具体共享环节、实际失败证据及影响；根因可待定位')
    return {k:scope[k].strip() for k in ('domain','component','causal_evidence','reusable_reason')}

def validate(data, require_scope=True):
    if not isinstance(data, dict):
        raise ValueError('记录必须为 JSON 对象')
    for key in FIELDS:
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError('缺少非空字段：' + key)
        if len(data[key]) > (4000 if key in ('attempts','actual','conclusion') else 1000):
            raise ValueError('字段过长：' + key)
    if len(data['title']) > 100 or len(data['incident_key']) > 240:
        raise ValueError('标题最多 100 字，事件标识最多 240 字')
    if data['occurred_at']=='unknown':
        if not isinstance(data.get('time_note'),str) or not data['time_note'].strip() or len(data['time_note'])>1000:
            raise ValueError('时间未知时必须填写 time_note，说明可确认范围或缺失原因')
    else:
        when = dt.datetime.fromisoformat(data['occurred_at'])
        if when.tzinfo is None or when.timestamp() > time.time() + 300:
            raise ValueError('occurred_at 必须带时区且不能为未来时间；无法确认可用 unknown + time_note')
    evidence = data.get('evidence')
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 12 or any(not isinstance(x,str) or not x.strip() or len(x)>2000 for x in evidence):
        raise ValueError('evidence 必须包含 1–12 条证据，不得伪造')
    value = {k:data[k].strip() for k in FIELDS}
    value['evidence'] = evidence
    if require_scope or 'scope' in data:
        value['scope'] = validate_scope(data.get('scope'))
    if data['occurred_at']=='unknown':value['time_note']=data['time_note'].strip()
    # This is a last-line heuristic, not a credential scanner guarantee.
    if re.search(r'(?i)(?:bearer\s+[\w.-]{16,}|[?&](?:key|token|secret|signature)=[^\s&]{8,}|(?:access_token|app_secret)\s*[=:]\s*[^\s]{8,})', dump(value)):
        raise ValueError('记录疑似含凭据或签名 URL；先脱敏')
    return value

class Notebook:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.base = self.root / '.codex/gravekeeper'
        self.book = self.root / '错题本'
        self.state = self.book / '.state'
        self.base.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)
        with self.db() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT OR IGNORE INTO meta VALUES ('revision','0');
            INSERT OR IGNORE INTO meta VALUES ('reviewed','0');
            CREATE TABLE IF NOT EXISTS incidents(
              id TEXT PRIMARY KEY, incident_key TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
              payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
              topic TEXT, review_note TEXT, archive TEXT, digest TEXT);
            CREATE TABLE IF NOT EXISTS topics(
              id TEXT PRIMARY KEY, signature TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
              cause TEXT NOT NULL, status TEXT NOT NULL, action TEXT, outcome TEXT,
              created REAL NOT NULL, updated REAL NOT NULL, closed REAL, archive TEXT);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,at REAL,action TEXT,detail TEXT);
            CREATE TABLE IF NOT EXISTS runs(
              id TEXT PRIMARY KEY,revision INTEGER,status TEXT,created REAL,finished REAL,note TEXT);
            CREATE TABLE IF NOT EXISTS jobs(
              id TEXT PRIMARY KEY,topic TEXT UNIQUE,status TEXT,terminal TEXT,created REAL,detail TEXT);
            ''')
            c.execute('BEGIN IMMEDIATE')
            if 'round' not in {r[1] for r in c.execute('PRAGMA table_info(jobs)')}:
                c.execute('ALTER TABLE jobs ADD COLUMN round INTEGER NOT NULL DEFAULT 1')
            job_columns={r[1] for r in c.execute('PRAGMA table_info(jobs)')}
            for name in ('owner_run','developer_session','report'):
                if name not in job_columns:c.execute(f'ALTER TABLE jobs ADD COLUMN {name} TEXT')
            c.execute('''CREATE TABLE IF NOT EXISTS job_tests(
              job TEXT,round INTEGER,status TEXT,session_id TEXT,created REAL,
              spec TEXT,artifacts TEXT,report TEXT,report_digest TEXT,
              PRIMARY KEY(job,round))''')
            # Legacy entries need a scope review; never grandfather them into governance.
            if 'admission' not in {r[1] for r in c.execute('PRAGMA table_info(incidents)')}:
                c.execute("ALTER TABLE incidents ADD COLUMN admission TEXT NOT NULL DEFAULT 'candidate'")
                c.execute('ALTER TABLE incidents ADD COLUMN scope_note TEXT')

            columns={r[1] for r in c.execute('PRAGMA table_info(topics)')}
            for name,kind in [('priority','TEXT'),('triage_decision','TEXT'),('triage_note','TEXT'),('next_step','TEXT'),('revisit_at','REAL')]:
                if name not in columns:c.execute(f'ALTER TABLE topics ADD COLUMN {name} {kind}')

    @contextlib.contextmanager
    def db(self):
        c = sqlite3.connect(self.state/'state.sqlite3', timeout=30)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def bump(self, c, action, detail):
        c.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
        c.execute('INSERT INTO audit(at,action,detail) VALUES (?,?,?)',(time.time(),action,dump(detail)))

    def submit(self, data):
        data = validate(data)
        now = time.time()
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT * FROM incidents WHERE incident_key=?',(data['incident_key'],)).fetchone()
            if old:
                return {'id':old['id'],'duplicate':True,'message':'同一事件已存在，补充请用 amend'}
            ident = 'E-' + uuid.uuid4().hex[:12]
            c.execute('INSERT INTO incidents(id,incident_key,title,payload,created,updated) VALUES (?,?,?,?,?,?)',
                      (ident,data['incident_key'],data['title'],dump(data),now,now))
            self.bump(c,'submit',{'id':ident})
            self.render(c)
        return {'id':ident,'duplicate':False}

    def payload(self, row):
        if row['archive']:
            raw = gzip.decompress((self.root/row['archive']).read_bytes())
            if hashlib.sha256(raw).hexdigest() != row['digest']:
                raise ValueError('归档校验失败：' + row['id'])
            return json.loads(raw)
        return json.loads(row['payload'])

    def amend(self, ident, note):
        if not isinstance(note,str) or not note.strip() or len(note)>4000:
            raise ValueError('补充说明应为 1–4000 字')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM incidents WHERE id=?',(ident,)).fetchone()
            if not row:
                raise ValueError('事件不存在')
            p = self.payload(row)
            p['attempts'] += '\n补充：' + note
            p = validate(p, require_scope=False)
            c.execute('UPDATE incidents SET payload=?,updated=?,archive=NULL,digest=NULL,review_note=? WHERE id=?',
                      (dump(p),time.time(),row['review_note'] if row['admission']=='excluded' else None,ident))
            if row['topic'] and row['admission']=='accepted':
                c.execute("UPDATE topics SET status='open',closed=NULL,archive=NULL,updated=? WHERE id=?",(time.time(),row['topic']))
            self.bump(c,'amend',{'id':ident})
            self.render(c)
        return {'id':ident,'amended':True}

    def assess(self, ident, spec):
        """Curator/owner scope review, with reversible exclusion and retained evidence."""
        decision=spec.get('decision');note=spec.get('note')
        if decision not in ('accepted','excluded') or not isinstance(note,str) or not note.strip() or len(note)>2000:
            raise ValueError('assess 需要 decision=accepted/excluded 和具体 note；仅治理角色维护')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM incidents WHERE id=?',(ident,)).fetchone()
            if not row:raise ValueError('事件不存在')
            if row['topic'] and c.execute("SELECT 1 FROM jobs WHERE topic=? AND status!='verified'",(row['topic'],)).fetchone():
                raise ValueError('专题有未完成开发任务；先核实并结束任务，不能静默移出治理')
            p=self.payload(row)
            if decision=='accepted':
                p['scope']=validate_scope(spec.get('scope',p.get('scope')))
                p=validate(p)
            if row['admission']==decision and row['scope_note']==note and p==self.payload(row):
                return {'id':ident,'admission':decision,'unchanged':True}
            c.execute('UPDATE incidents SET admission=?,scope_note=?,review_note=?,payload=?,archive=NULL,digest=NULL,updated=? WHERE id=?',
                      (decision,note,None if decision=='accepted' else '范围排除：'+note,dump(p),time.time(),ident))
            if row['topic']:
                if not self.accepted_count(c,row['topic']):
                    c.execute("UPDATE topics SET status='excluded',updated=? WHERE id=?",(time.time(),row['topic']))
                elif decision=='accepted':
                    c.execute("UPDATE topics SET status='open',closed=NULL,archive=NULL,updated=? WHERE id=?",(time.time(),row['topic']))
            self.bump(c,'assess',{'id':ident,'decision':decision,'note':note})
            self.render(c)
        return {'id':ident,'admission':decision}

    @staticmethod
    def accepted_count(c, tid):
        return c.execute("SELECT count(*) FROM incidents WHERE topic=? AND admission='accepted'",(tid,)).fetchone()[0]

    def eligible(self, c, tid):
        t=c.execute('SELECT * FROM topics WHERE id=?',(tid,)).fetchone()
        return bool(t and t['status']!='excluded' and self.accepted_count(c,tid)>0
                    and t['priority'] and t['triage_decision']=='investigate')

    def triage(self, tid, spec):
        priority=spec.get('priority');decision=spec.get('decision')
        if priority not in ('P0','P1','P2','P3') or decision not in ('investigate','defer'):
            raise ValueError('triage 需要 priority=P0/P1/P2/P3 和 decision=investigate/defer')
        for key in ('note','next_step'):
            if not isinstance(spec.get(key),str) or not spec[key].strip() or len(spec[key])>4000:
                raise ValueError('triage 必须提供 '+key)
        if priority in ('P0','P1') and decision!='investigate':
            raise ValueError('高优问题必须本轮启动定位，不得等待次数')
        revisit=None
        if decision=='defer':
            when=dt.datetime.fromisoformat(spec.get('revisit_at',''))
            if when.tzinfo is None or when.timestamp()<=time.time():
                raise ValueError('暂缓必须提供未来的带时区 revisit_at')
            revisit=when.timestamp()
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            t=c.execute('SELECT * FROM topics WHERE id=?',(tid,)).fetchone()
            if not t or t['status'] in ('verified','archived','excluded') or not self.accepted_count(c,tid):
                raise ValueError('只能评估有 accepted 记录的未解决专题')
            if decision=='defer' and c.execute("SELECT 1 FROM jobs WHERE topic=? AND status!='verified'",(tid,)).fetchone():
                raise ValueError('已有开发任务，不能静默暂缓')
            c.execute('UPDATE topics SET priority=?,triage_decision=?,triage_note=?,next_step=?,revisit_at=?,updated=? WHERE id=?',
                      (priority,decision,spec['note'],spec['next_step'],revisit,time.time(),tid))
            self.bump(c,'triage',{'topic':tid,**spec});self.render(c)
        return {'topic':tid,'priority':priority,'decision':decision}

    def group(self, spec):
        for key in ('signature','title','cause'):
            if not isinstance(spec.get(key),str) or not spec[key].strip() or len(spec[key])>4000:
                raise ValueError('专题缺少 '+key)
        ids = list(dict.fromkeys(spec.get('records',[])))
        if not ids:
            raise ValueError('必须列出实际记录 ID')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            rows=[]
            for ident in ids:
                row=c.execute('SELECT * FROM incidents WHERE id=?',(ident,)).fetchone()
                if not row:
                    raise ValueError('记录不存在：'+ident)
                if row['admission']!='accepted':
                    raise ValueError('记录未通过体系归属复核，不能建专题或计数：'+ident)
                rows.append(row)
            topic=c.execute('SELECT * FROM topics WHERE signature=?',(spec['signature'],)).fetchone()
            tid=topic['id'] if topic else 'T-'+uuid.uuid4().hex[:10]
            if any(r['topic'] and r['topic']!=tid for r in rows):
                raise ValueError('记录已归属其他专题；不得重复计数，请先核实分类')
            added=any(r['topic']!=tid for r in rows)
            if topic and not added and topic['cause']==spec['cause']:
                for row in rows:
                    if row['review_note'] is None:
                        c.execute('UPDATE incidents SET review_note=? WHERE id=?',('归因：'+spec['cause'],row['id']))
                count=self.accepted_count(c,tid)
                self.render(c)
                return {'topic':tid,'count':count,'eligible':self.eligible(c,tid),'unchanged':True}
            if topic:
                if added or topic['cause']!=spec['cause']:
                    c.execute("UPDATE topics SET status='open',cause=?,closed=NULL,archive=NULL,updated=? WHERE id=?",(spec['cause'],time.time(),tid))
            else:
                c.execute('INSERT INTO topics(id,signature,title,cause,status,created,updated) VALUES (?,?,?,?,?,?,?)',
                          (tid,spec['signature'],spec['title'],spec['cause'],'open',time.time(),time.time()))
            for row in rows:
                # Reopen archived records when a known issue recurs.
                c.execute('UPDATE incidents SET topic=?,review_note=?,payload=?,archive=NULL,digest=NULL WHERE id=?',
                          (tid,'归因：'+spec['cause'],dump(self.payload(row)),row['id']))
            self.bump(c,'group',{'topic':tid,'records':ids})
            self.render(c)
            count=self.accepted_count(c,tid)
            return {'topic':tid,'count':count,'eligible':self.eligible(c,tid)}

    def review(self, ident, note):
        if not note or len(note)>2000:
            raise ValueError('必须说明暂不合并或尚待核实的原因')
        with self.db() as c:
            if not c.execute('UPDATE incidents SET review_note=? WHERE id=?',(note,ident)).rowcount:
                raise ValueError('记录不存在')
            self.render(c)

    @staticmethod
    def open_jobs(c):
        return [dict(r) for r in c.execute("SELECT id,topic,status,round,owner_run FROM jobs WHERE COALESCE(status,'unknown')!='verified' ORDER BY created")]

    def require_guardian(self, c, owner_run=None):
        ident=self.meta(c,'guardian_cycle')
        if not ident:raise ValueError('需要本轮已登记守墓人身份')
        cycle=json.loads((self.base/'sessions'/ident/'cycle.json').read_text())
        if (cycle.get('status')!='running' or not cycle.get('session_id') or
            os.environ.get('CODEX_THREAD_ID')!=cycle['session_id'] or
            os.environ.get('HC_TERMINAL_ID')!=cycle.get('terminal') or
            (owner_run and owner_run!=cycle['run_id'])):
            raise ValueError('只有原轮次守墓人可调度、验收或完成巡检')
        return cycle

    def checked_artifacts(self, artifacts):
        for name,digest in artifacts.items():
            path=(self.root/name).resolve()
            if not path.is_relative_to(self.root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
                raise ValueError('测试对象在交付后发生变化：'+name)

    def resolve(self, tid, spec):
        if spec.get('action') not in ('sop','code','both','external'):
            raise ValueError('action 应为 sop/code/both/external')
        if not spec.get('summary') or not spec.get('validation') or not spec.get('artifacts'):
            raise ValueError('必须提供 summary、validation 和实际 artifacts 路径')
        for name in spec['artifacts']:
            path=(self.root/name).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                raise ValueError('产物必须为项目内真实文件：'+name)
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            t=c.execute('SELECT * FROM topics WHERE id=?',(tid,)).fetchone()
            if not t: raise ValueError('专题不存在')
            count=self.accepted_count(c,tid)
            if not self.eligible(c,tid): raise ValueError('专题须经优先级评估并启动治理后才能结案')
            job=c.execute('SELECT * FROM jobs WHERE topic=?',(tid,)).fetchone()
            if job:
                guardian=self.require_guardian(c,job['owner_run'])
                test=c.execute('SELECT * FROM job_tests WHERE job=? AND round=?',(job['id'],job['round'])).fetchone()
                if job['status']!='tested' or not test or test['status']!='passed':
                    raise ValueError('守墓人测试未通过，不能验收结案')
                if test['session_id']!=guardian['session_id'] or test['session_id']==job['developer_session']:
                    raise ValueError('开发 Agent 不能提交测试或验收')
                self.checked_artifacts(json.loads(test['artifacts']))
                self.checked_artifacts({test['report']:test['report_digest']})
            now=time.time()
            c.execute("UPDATE topics SET status='verified',action=?,outcome=?,closed=?,updated=? WHERE id=?",(spec['action'],dump(spec),now,now,tid))
            c.execute("UPDATE jobs SET status='verified' WHERE topic=?",(tid,))
            self.bump(c,'resolve',{'topic':tid,'artifacts':spec['artifacts']})
            self.render(c)
        return {'topic':tid,'status':'verified'}

    def pending(self):
        with self.db() as c:
            return {'revision':self.meta(c,'revision'), 'reviewed':self.meta(c,'reviewed'),
                    'records':[dict(r)|{'payload':self.payload(r)} for r in c.execute("SELECT * FROM incidents WHERE admission!='excluded' AND (admission='candidate' OR review_note IS NULL) ORDER BY created LIMIT 100")],
                    'topics':[dict(t)|{'count':self.accepted_count(c,t['id'])} for t in c.execute("SELECT * FROM topics WHERE status NOT IN ('verified','archived','excluded') ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 WHEN 'P3' THEN 3 ELSE -1 END, updated") if self.accepted_count(c,t['id'])][:100]}

    def search(self,query):
        terms=query.split()
        if not terms or len(query)>200:raise ValueError('输入 1–200 字的错误特征/组件/关键词，空格分隔')
        # Search compact metadata, including reviewed and archived incidents; decompress only on show.
        def match(value):return any(term.casefold() in value.casefold() for term in terms)
        with self.db() as c:
            topics=[dict(t) for t in c.execute('SELECT * FROM topics ORDER BY updated DESC') if match(t['title']+' '+t['signature']+' '+t['cause'])]
            tids={t['id'] for t in topics}
            records=[dict(r) for r in c.execute('SELECT id,incident_key,title,topic,review_note,archive,admission,scope_note FROM incidents ORDER BY created DESC') if r['topic'] in tids or match(r['title']+' '+(r['review_note'] or ''))]
        return {'records':records[:100],'topics':topics[:50],'record_matches':len(records),'topic_matches':len(topics),'hint':'结果超限时缩小关键词；show ID 回读完整证据'}

    @staticmethod
    def meta(c,key):
        row=c.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return row[0] if row else None

    def begin(self):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute("SELECT * FROM runs WHERE status='running'").fetchone()
            if old: return dict(old)
            rid='R-'+uuid.uuid4().hex[:10]
            c.execute('INSERT INTO runs VALUES (?,?,?,?,NULL,NULL)',(rid,int(self.meta(c,'revision')),'running',time.time()))
            return dict(c.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone())

    def finish(self, rid, note):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            pending=self.open_jobs(c)
            if pending:raise ValueError('存在尚未验收闭环的任务，保留同一守墓人 session：'+','.join(x['id']+':'+x['status'] for x in pending))
            if self.meta(c,'guardian_cycle'):self.require_guardian(c,rid)
            row=c.execute("SELECT * FROM runs WHERE id=? AND status='running'",(rid,)).fetchone()
            if not row:
                old=c.execute("SELECT * FROM runs WHERE id=? AND status='complete'",(rid,)).fetchone()
                if old:return {'run':rid,'reviewed_revision':old['revision'],'duplicate':True}
                raise ValueError('没有对应的进行中巡检')
            # Use the snapshot, not the current revision: submissions during review must wake again.
            c.execute("UPDATE meta SET value=? WHERE key='reviewed'",(str(row['revision']),))
            c.execute("UPDATE runs SET status='complete',finished=?,note=? WHERE id=?",(time.time(),note,rid))
        return {'run':rid,'reviewed_revision':row['revision']}

    def archive(self, now=None):
        now=time.time() if now is None else now
        archived=[]
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            for t in c.execute("SELECT * FROM topics WHERE status='verified' AND closed<=? AND updated<=?",(now-30*86400,now-30*86400)).fetchall():
                month=dt.datetime.fromtimestamp(now).strftime('%Y-%m')
                folder=self.book/'归档'/month/t['id']
                folder.mkdir(parents=True,exist_ok=True)
                for row in c.execute('SELECT * FROM incidents WHERE topic=?',(t['id'],)).fetchall():
                    raw=dump(self.payload(row)).encode()
                    path=folder/(row['id']+'.json.gz')
                    atomic(path,gzip.compress(raw,mtime=0))
                    if gzip.decompress(path.read_bytes()) != raw: raise ValueError('归档回读不一致')
                    c.execute("UPDATE incidents SET payload='{}',archive=?,digest=? WHERE id=?",(str(path.relative_to(self.root)),hashlib.sha256(raw).hexdigest(),row['id']))
                c.execute("UPDATE topics SET status='archived',archive=? WHERE id=?",(str(folder.relative_to(self.root)),t['id']))
                c.execute('INSERT INTO audit(at,action,detail) VALUES (?,?,?)',(now,'archive',t['id']))
                archived.append(t['id'])
            self.render(c)
        return {'archived':archived,'retention_days':30}

    def render(self,c=None):
        if c is None:
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                return self.render(db)
        records=c.execute('SELECT * FROM incidents ORDER BY created DESC').fetchall()
        topics=c.execute('SELECT * FROM topics ORDER BY updated DESC').fetchall()
        def title(s): return s.replace('\n',' ').replace('|','／').replace('[','（').replace(']','）')
        active=[t for t in topics if t['status'] not in ('archived','excluded') and self.accepted_count(c,t['id'])]
        pending=[r for r in records if not r['topic'] and not r['archive'] and r['admission']=='accepted']
        candidates=[r for r in records if r['admission']=='candidate']
        excluded=[r for r in records if r['admission']=='excluded']
        index=['# 错题本','', '由 notebook.py 生成；通过命令提交，不直接编辑本页。', '',
               f'活动专题 {len(active)} · 未归类记录 {len(pending)} · 待范围复核 {len(candidates)} · 已排除记录 {len(excluded)} · 已归档专题 {sum(t["status"]=="archived" for t in topics)}', '',
               '[记录规范](../SOP/错题记录与治理SOP.md) · [全部记录](目录.md) · [归档目录](归档/索引.md)', '',
               '| 专题 | 优先级 | 决策 | 次数（参考） | 状态 |','| --- | --- | --- | ---: | --- |']
        for t in active[:100]:
            count=self.accepted_count(c,t['id'])
            index.append(f"| [{title(t['title'])}](专题/{t['id']}.md) | {t['priority'] or '待评估'} | {t['triage_decision'] or '待评估'} | {count} | {t['status']} |")
        index += ['','最近未归类记录（最多 20 条）：','']
        index += [f"- [{title(r['title'])}](记录/{r['id']}.md)" for r in pending[:20]]
        index += ['','待范围复核（通过前不建专题、不计次数）：','']
        index += [f"- [{title(r['title'])}](记录/{r['id']}.md)" for r in candidates[:20]]
        atomic(self.book/'索引.md','\n'.join(index)+'\n')
        atomic(self.book/'目录.md','# 全部记录\n\n'+'\n'.join(f"- [{title(r['title'])}](记录/{r['id']}.md) · {r['id']} · {r['admission']}" for r in records)+'\n')
        archive_index=['# 归档索引','']
        for t in topics:
            text=f"# {t['title']}\n\n编号：{t['id']}\n\n状态：{t['status']}\n\n归因：{t['cause']}\n\n"
            text+=f"优先级：{t['priority'] or '待评估'} · 决策：{t['triage_decision'] or '待评估'}\n\n依据：{t['triage_note'] or '待评估'}\n\n下一步：{t['next_step'] or '待评估'}\n\n复查时间（Unix）：{t['revisit_at']}\n\n"
            text+='关联事件：\n\n'+'\n'.join(f"- [{title(r['title'])}](../记录/{r['id']}.md)" for r in records if r['topic']==t['id'])+'\n'
            if t['outcome']: text+='\n治理与验收：\n\n```json\n'+t['outcome']+'\n```\n'
            atomic(self.book/'专题'/(t['id']+'.md'),text)
            if t['archive']: archive_index.append(f"- [{title(t['title'])}](../专题/{t['id']}.md) · {t['archive']}")
        atomic(self.book/'归档/索引.md','\n'.join(archive_index)+'\n')
        for r in records:
            if r['archive']:
                text=f"# {r['title']}\n\n已归档。编号：{r['id']}\n\n使用 `python3 .codex/gravekeeper/notebook.py show {r['id']}` 回读并校验压缩原文。\n\n归档：`{r['archive']}`\n"
            else:
                p=self.payload(r)
                text=f"# {p['title']}\n\n编号：{r['id']}\n\n范围：{r['admission']} · {r['scope_note'] or '待复核'}\n\n```json\n{dump(p)}\n```\n\n审核：{r['review_note'] or '待分析'}\n"
            atomic(self.book/'记录'/(r['id']+'.md'),text)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=ROOT,help='仅测试时用独立目录；生产保持默认')
    sub=p.add_subparsers(dest='cmd',required=True)
    for name in ('submit','group','pending','begin','archive','render','status'): sub.add_parser(name)
    for name in ('show','resolve','assess','triage'):
        sub.add_parser(name).add_argument('id')
    for name in ('amend','review','finish'):
        q=sub.add_parser(name);q.add_argument('id');q.add_argument('--note',required=True)
    sub.add_parser('search').add_argument('query')
    a=p.parse_args();n=Notebook(a.root)
    if a.cmd in ('submit','group'): result=getattr(n,a.cmd)(json.load(sys.stdin))
    elif a.cmd in ('resolve','assess','triage'): result=getattr(n,a.cmd)(a.id,json.load(sys.stdin))
    elif a.cmd in ('amend','review','finish'): result=getattr(n,a.cmd)(a.id,a.note)
    elif a.cmd=='search':result=n.search(a.query)
    elif a.cmd=='show':
        with n.db() as c:
            row=c.execute('SELECT * FROM incidents WHERE id=?',(a.id,)).fetchone()
            if not row: raise ValueError('事件不存在')
            result=dict(row)|{'payload':n.payload(row)}
    elif a.cmd=='status':
        with n.db() as c:
            result={k:c.execute('SELECT count(*) FROM '+k).fetchone()[0] for k in ('incidents','topics','jobs')}
            result['admission_counts']={r[0]:r[1] for r in c.execute('SELECT admission,count(*) FROM incidents GROUP BY admission')}
            result['active_topics']=c.execute("SELECT count(*) FROM topics t WHERE status NOT IN ('archived','excluded') AND EXISTS (SELECT 1 FROM incidents i WHERE i.topic=t.id AND i.admission='accepted')").fetchone()[0]
            result['excluded_topics']=c.execute("SELECT count(*) FROM topics WHERE status='excluded'").fetchone()[0]
            result['meta']={r[0]:r[1] for r in c.execute('SELECT * FROM meta')}
            result['runs']=[dict(r) for r in c.execute('SELECT * FROM runs ORDER BY created DESC LIMIT 5')]
    else: result=getattr(n,a.cmd)()
    print(dump(result))

if __name__=='__main__':
    try: main()
    except (ValueError,KeyError) as exc:
        print(dump({'error':str(exc)}),file=sys.stderr);sys.exit(2)
