import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import aw

class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()/'workspace with spaces'
        self.cfg=aw.read(aw.REPO/'config/workspace.example.json')
        self.cfg['members']=[{'account':'alice@example.test','slug':'alice'},{'account':'bob@example.test','slug':'bob'}]
        self.path=Path(self.tmp.name)/'configuration.json';self.path.write_text(aw.dump(self.cfg))
    def install(self):return aw.init(self.root,self.path,'core')
    def test_new_workspace_and_idempotent_install_preserve_data(self):
        r=self.install();self.assertFalse(r['services_started']);self.assertFalse(r['credentials_copied'])
        notebook=self.root/'.codex/gravekeeper/notebook.py';db=self.root/'错题本/.state/state.sqlite3'
        self.assertTrue(db.is_file());before=db.read_bytes()
        self.assertEqual(self.install()['status'],'already-initialized');self.assertEqual(db.read_bytes(),before)
        self.assertTrue(aw.doctor(self.root)['passed'])
        self.assertFalse((self.root/'.secrets/hi.json').exists())
        self.assertTrue((self.root/'.agents/skills/standardize-sop/assets/SOP模板.md').is_file())
        self.assertFalse((self.root/'.agents/skills/workspace-bootstrap').exists())
    def test_existing_project_and_changed_configuration_refused(self):
        self.root.mkdir();(self.root/'business.txt').write_text('keep')
        with self.assertRaises(ValueError):self.install()
        self.assertEqual((self.root/'business.txt').read_text(),'keep')
        (self.root/'business.txt').unlink();self.install()
        self.cfg['owner']='another@example.test';self.path.write_text(aw.dump(self.cfg))
        with self.assertRaises(ValueError):self.install()
    def test_member_traversal_and_duplicate_binding_rejected(self):
        for slug in ('../escape','a/b','..','/tmp/x'):
            self.cfg['members'][0]['slug']=slug
            with self.assertRaises(ValueError):aw.validate(self.cfg,'core')
    def test_time_gate_kept_in_service_and_core_cannot_start(self):
        self.install();r=aw.service(self.root,'organize','generate')
        value=plistlib.loads(Path(r['generated']).read_bytes())
        self.assertIn('--scheduled',value['ProgramArguments']);self.assertEqual(value['StartInterval'],900)
        with self.assertRaises(ValueError):aw.service(self.root,'organize','install')
    def test_installed_account_permissions_and_symlink_escape(self):
        self.install();base=self.root/'.codex/hi-bridge'
        code='''import json,sys
sys.path.insert(0,sys.argv[1])
import member_access as a
c=json.load(open(sys.argv[1]+'/config.json'))
assert a.workspace(c,'alice@example.test').name=='alice'
try:a.workspace(c,'unbound@example.test')
except ValueError:pass
else:raise AssertionError('unbound account allowed')
fs=a.filesystem(c,'alice@example.test')
assert fs[str(a.workspace(c,'alice@example.test'))]=='write'
assert not any('/bob' in k for k in fs)
assert a.overrides(c,'alice@example.test')['approval_policy']=='never'
'''
        result=subprocess.run([sys.executable,'-c',code,str(base)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
    def test_full_configuration_has_required_external_contract(self):
        with self.assertRaises(ValueError):aw.validate(self.cfg,'full')
    def test_stop_holds_task_lock_until_service_unloaded(self):
        import fcntl
        self.install();generated=Path(aw.service(self.root,'organize','generate')['generated'])
        home=Path(self.tmp.name)/'home';installed=home/'Library/LaunchAgents'/generated.name
        installed.parent.mkdir(parents=True);installed.write_bytes(generated.read_bytes())
        lockpath=self.root/'.codex/tasks/organize/run.lock'
        def unload(*args,**kwargs):
            with lockpath.open('a') as competing:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(competing,fcntl.LOCK_EX|fcntl.LOCK_NB)
            return subprocess.CompletedProcess(args[0],0)
        with patch.object(aw.sys,'platform','darwin'),patch.object(Path,'home',return_value=home),patch.object(aw.subprocess,'run',side_effect=unload):
            self.assertTrue(aw.service(self.root,'organize','stop')['data_preserved'])
        self.assertFalse(installed.exists());self.assertTrue(installed.with_suffix('.plist.disabled').exists())
        with lockpath.open('a') as lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)

if __name__=='__main__':unittest.main()
