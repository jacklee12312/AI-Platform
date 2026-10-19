"""Privileged bridge tests with process execution replaced; no real services changed."""
import hashlib,io,json,os,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import common,service_entry
BUNDLE=Path(__file__).resolve().parents[2]

class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'state';self.previous=os.umask(0o022)
        self.sources=common.read_json(BUNDLE/'sources.json');self.release=common.read_json(BUNDLE/'release.json');self.calls=[]
    def tearDown(self):os.umask(self.previous);self.tmp.cleanup()
    def dispatch(self,action,data,old_active=False,workflow_enabled=False,yiqiban_enabled=False,preflight_only=False):
        def run(args,**kwargs):
            self.calls.append(args)
            active=old_active and args==['systemctl','is-active','ai-platform-offline-install.service']
            return SimpleNamespace(stdout='active' if active else 'inactive',returncode=0)
        stdin=SimpleNamespace(buffer=io.BytesIO(json.dumps(data).encode()))
        catalog={'models':['copaw-flash-9b']}
        if workflow_enabled:catalog['workflow_sha256']='fixture'
        original_read=service_entry.read_json
        def read_metadata(path,*args):
            if Path(path)==BUNDLE/'installer-manifest.json':
                files=[*sorted((BUNDLE/'installer').glob('*.py')),BUNDLE/'release.json']
                return {'format':1,'files':[{'path':p.relative_to(BUNDLE).as_posix(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]}
            value=original_read(path,*args)
            if Path(path)==BUNDLE/'release.json' and not yiqiban_enabled:value=dict(value);value.pop('yiqiban_resource_id',None)
            return value
        argv=['service_entry','--bundle',str(BUNDLE),action]+(['--preflight-only'] if preflight_only else [])
        with mock.patch.object(service_entry,'read_json',side_effect=read_metadata),mock.patch.object(service_entry,'STATE_ROOT',self.root),mock.patch('service_entry.os.geteuid',return_value=0),mock.patch.object(service_entry,'detect_device_count',return_value=2),mock.patch.object(service_entry,'verify_app'),mock.patch.object(service_entry,'load_catalog',return_value=catalog),mock.patch('service_entry.subprocess.run',side_effect=run),mock.patch.object(sys,'argv',argv),mock.patch.object(sys,'stdin',stdin):service_entry.main()

    def test_yiqiban_cannot_be_started_without_its_shared_components(self):
        value={'admin_password':'fixture-password-123','sources':self.sources,'selected_models':['copaw-flash-9b'],'device_count':2,
               'workflow':{'probe':{},'components':{'selected':[]}}}
        with mock.patch('probe.detect_probe',return_value=True),self.assertRaisesRegex(ValueError,'MySQL'):
            self.dispatch('install',value,workflow_enabled=True,yiqiban_enabled=True)
        self.assertFalse((self.root/'private/request.json').exists())

    def test_yiqiban_root_bridge_preserves_public_host_and_copies_acceptance_worker(self):
        value={'admin_password':'fixture-password-123','sources':self.sources,'selected_models':['copaw-flash-9b'],'device_count':2,
               'yiqiban':{'public_host':'192.168.1.47'},
               'workflow':{'probe':{},'components':{'selected':['mysql','redis','paradedb'],
                 'mysql_password':'fixture-password-123','redis_password':'fixture-password-123','paradedb_password':'fixture-password-123'}}}
        with mock.patch('probe.detect_probe',return_value=True):self.dispatch('install',value,workflow_enabled=True,yiqiban_enabled=True)
        saved=common.read_json(self.root/'private/request.json')
        self.assertEqual(saved['yiqiban']['public_host'],'192.168.1.47')
        self.assertTrue((self.root/'private/code/yiqiban_verify.py').is_file())
    def test_root_independently_detects_existing_probe_without_copying_client_registration(self):
        value={'admin_password':'fixture-password-123','sources':self.sources,'selected_models':['copaw-flash-9b'],'device_count':2,
               'workflow':{'probe':{'bindActivateToken':'unused-client-secret'},'components':{'selected':[]}}}
        with mock.patch('probe.detect_probe',return_value=True):self.dispatch('install',value,workflow_enabled=True)
        saved=common.read_json(self.root/'private/request.json')
        self.assertEqual(saved['workflow']['probe'],{'mode':'auto','reuse_existing':True})
        self.assertNotIn('unused-client-secret',json.dumps(saved))
    def test_client_reuse_flag_cannot_skip_a_missing_probe(self):
        value={'admin_password':'fixture-password-123','sources':self.sources,'selected_models':['copaw-flash-9b'],'device_count':2,
               'workflow':{'probe':{'mode':'auto','reuse_existing':True},'components':{'selected':[]}}}
        with mock.patch('probe.detect_probe',return_value=False),self.assertRaisesRegex(ValueError,'必须'):
            self.dispatch('install',value,workflow_enabled=True)
        self.assertFalse((self.root/'private/request.json').exists());self.assertFalse(any(c[0]=='systemd-run' for c in self.calls))
    def test_install_saves_sources_and_keeps_secret_out_of_command_line(self):
        self.dispatch('install',{'admin_password':'fixture-password-123','sources':self.sources,'selected_models':['copaw-flash-9b'],'device_count':2})
        request=common.read_json(self.root/'private/request.json');self.assertEqual(request['sources'],self.sources)
        self.assertEqual((self.root/'private/request.json').stat().st_mode & 0o777,0o600)
        launch=next(c for c in self.calls if c[0]=='systemd-run')
        self.assertNotIn('fixture-password-123',' '.join(launch));self.assertIn('--unit=ai-platform-install.service',launch)
        self.assertTrue((self.root/'private/code/artifacts.py').is_file())
        self.assertTrue((self.root/'private/code/version.py').is_file())
        self.assertTrue((self.root/'private/code/container_storage.py').is_file())
        staged=common.read_json(self.root/'private/code/worker-manifest.json')
        self.assertIn('container_storage',staged['checked_imports'])
        self.assertEqual(common.read_json(self.root/'private/code/release.json')['installer_version'],self.release['installer_version'])
    def test_preflight_dispatch_uses_the_same_private_worker_and_explicit_safe_stop(self):
        self.dispatch('install',{'admin_password':'fixture-password-123','sources':self.sources,'selected_models':['copaw-flash-9b'],'device_count':2},preflight_only=True)
        launch=next(c for c in self.calls if c[0]=='systemd-run')
        self.assertIn(str(self.root/'private/code/engine.py'),launch)
        self.assertIn('--preflight-only',launch)
    def test_resume_changes_sources_but_preserves_keys_and_password_hash(self):
        common.atomic_json(self.root/'private/journal.json',{'release_id':self.release['release_id'],'installer_version':self.release['installer_version'],'status':'failed'})
        common.atomic_json(self.root/'private/request.json',{'admin_hash':'fixture-hash','api_key':'fixture-api-key','sources':self.sources,'selected_models':['copaw-flash-9b'],'device_count':2})
        changed=dict(self.sources,mode='local-only')
        self.dispatch('resume',{'sources':changed,'device_count':2})
        request=common.read_json(self.root/'private/request.json')
        self.assertEqual(request['admin_hash'],'fixture-hash');self.assertEqual(request['api_key'],'fixture-api-key');self.assertEqual(request['sources']['mode'],'local-only')
    def test_old_running_installer_blocks_second_worker(self):
        with self.assertRaisesRegex(RuntimeError,'旧版安装'):self.dispatch('install',{'admin_password':'fixture-password-123','sources':self.sources},True)
        self.assertFalse(any(c[0]=='systemd-run' for c in self.calls))
    def test_invalid_sources_do_not_start_worker(self):
        with self.assertRaises(RuntimeError):self.dispatch('install',{'admin_password':'fixture-password-123','sources':dict(self.sources,mode='invalid')})
        self.assertFalse(any(c[0]=='systemd-run' for c in self.calls))

if __name__=='__main__':unittest.main(verbosity=2)
