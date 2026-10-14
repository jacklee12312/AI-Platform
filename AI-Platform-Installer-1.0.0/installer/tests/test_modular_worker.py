"""Integration: actual acquisition, verification, unpack and payload; simulated system actions."""
import hashlib,json,os,sys,tarfile,tempfile,unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import artifacts,common,engine
BUNDLE=Path(__file__).resolve().parents[2]

class ModularWorkerTests(unittest.TestCase):
    def test_compose_plan_uses_detected_hardware_variant(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);payload=root/'resources/payload/home/cix/houmo-docker-deployment'
            variant=root/'resources/variants/single/houmo-docker-deployment'
            payload.mkdir(parents=True);variant.mkdir(parents=True)
            (payload/'compose.yaml').write_text('dual',encoding='utf-8')
            (variant/'compose.yaml').write_text('single',encoding='utf-8')
            for count,expected in [(1,variant/'compose.yaml'),(2,payload/'compose.yaml')]:
                obj=object.__new__(engine.Installer)
                obj.resources=root/'resources';obj.root=root/'state';obj.home=root/'home'
                obj.catalog={'schema':1};obj.device_count=count;obj.plan=[]
                obj.make_plan()
                match=next(source for source,target,_ in obj.plan if target==obj.home/'houmo-docker-deployment/compose.yaml')
                self.assertEqual(match,expected)

    def fixture(self,stack):
        root=Path(stack.enter_context(tempfile.TemporaryDirectory()));bundle=root/'installer';src=root/'repository/files';home=root/'home';home.mkdir();state=root/'state';state.mkdir()
        manifest=common.read_json(BUNDLE/'catalog/platform-manifest.json');manifest['installed_payload_bytes']=1024;manifest['all_model_file_bytes']=len(b'model-fixture-content')+len(b'unselected-content');manifest['docker_disk_budget_bytes']=0
        common.atomic_json(bundle/'catalog/platform-manifest.json',manifest)
        common.atomic_json(bundle/'catalog/release-catalog.json',{'fixture':True})
        def put(rel,data):
            p=src/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        put('manifests/manifest.json',json.dumps(manifest).encode())
        put('scripts/offline_install.py',b'def install_docker(): pass\ndef images(manifest): pass\ndef setup_units(): pass\ndef compose(*args): pass\n')
        put('scripts/verify_runtime.py',b'# real inference is replaced by a fixture in this test\n')
        for n in ['gateway-admin','ai-model-gateway','ballot-reader']:
            put('payload/home/cix/'+n+'/fixture.txt',b'fixture')
        put('payload/home/cix/houmo-docker-deployment/compose.yaml',(BUNDLE/'installer/tests/fixtures/compose.yaml').read_bytes())
        put('variants/single/houmo-docker-deployment/compose.yaml',(BUNDLE/'installer/tests/fixtures/compose.yaml').read_bytes())
        put('payload/home/cix/cix_telecom_projects/fixture-model.bin',b'model-fixture-content')
        put('payload/home/cix/cix_telecom_projects/unselected-model.bin',b'unselected-content')
        (src/'runtime').mkdir()
        for n in ['qwen36-vllm-runtime.tar.gz','cosyvoice-runtime.tar.gz','host-libs.tar.gz','ui-static.tar.gz','qwen38-hliellama-2.2.0.tar.gz']:
            with tarfile.open(src/'runtime'/n,'w:gz'):pass
        files=[p for p in src.rglob('*') if p.is_file()]
        put('manifests/SHA256SUMS',''.join(common.sha256(p)+'  '+p.relative_to(src).as_posix()+'\n' for p in files).encode())
        rows=[]
        for p in src.rglob('*'):
            if not p.is_file():continue
            rel=p.relative_to(src).as_posix();model='copaw-flash-9b' if rel.endswith('fixture-model.bin') else 'spark-x1-4b' if rel.endswith('unselected-model.bin') else None
            rows.append({'path':rel,'size':p.stat().st_size,'sha256':common.sha256(p),'category':'models' if model else 'platform','models':[model] if model else []})
        catalog={'resources':rows,'models':['copaw-flash-9b','spark-x1-4b'],'components':[]}
        common.atomic_json(bundle/'release.json',{'release_id':'fixture-modular','installer_version':'0.2.3','resources_checksums_sha256':common.sha256(src/'manifests/SHA256SUMS')})
        config={'schema':1,'mode':'local-only','local_roots':[str(src.parent)],'base_urls':{},'file_urls':{},'allow_http':False}
        common.atomic_json(state/'private/request.json',{'admin_password':'fixture-password-123','sources':config,'selected_models':['copaw-flash-9b'],'device_count':2})
        stack.enter_context(mock.patch('artifacts.load_catalog',return_value=catalog))
        stack.enter_context(mock.patch('artifacts.CACHE',root/'cache'))
        stack.enter_context(mock.patch('engine.quick_checks',return_value=[]))
        stack.enter_context(mock.patch('engine.detect_device_count',return_value=2))
        stack.enter_context(mock.patch('engine.subprocess.check_output',return_value='install ok installed'))
        stack.enter_context(mock.patch('engine.subprocess.run',return_value=mock.Mock(returncode=1,stdout='')))
        stack.enter_context(mock.patch('engine.shutil.which',return_value=None))
        stack.enter_context(mock.patch('engine.shutil.disk_usage',return_value=mock.Mock(free=1024**4)))
        stack.enter_context(mock.patch('engine.pwd.getpwnam',return_value=mock.Mock(pw_uid=os.getuid(),pw_gid=os.getgid())))
        stack.enter_context(mock.patch('engine.os.chown'))
        stack.enter_context(mock.patch.object(engine.Installer,'guard_units'))
        return bundle,state,home,manifest
    def simulate_commands(self,obj,manifest):
        commands=[]
        def command(*args,**kwargs):
            commands.append(tuple(str(x) for x in args))
            if '--selected-json' in args:
                names={'selected-containers-and-autostart','selected-gateway-registry','accelerator-mode:copaw','restore-mode:copaw'}
                common.atomic_json(obj.home/'houmo-docker-deployment/state/offline-acceptance.json',{'run_id':obj.state['acceptance_run_id'],'passed':True,'scope':'selected-models','selected_models':['copaw-flash-9b'],'device_count':2,'registry_models':manifest['models'],'checks':[{'name':n,'passed':True} for n in names]})
        obj.command=command;return commands
    def test_actual_acquire_reaches_normal_worker_installation(self):
        with ExitStack() as stack:
            b,s,h,m=self.fixture(stack);o=engine.Installer(b,s,h);o.state['initial_guard_passed']=True;commands=self.simulate_commands(o,m);o.run()
            self.assertEqual(o.state['status'],'installed');self.assertEqual(o.state['completed'],[n for n,_ in common.STEPS])
            self.assertEqual((h/'cix_telecom_projects/fixture-model.bin').read_bytes(),b'model-fixture-content')
            self.assertFalse((h/'cix_telecom_projects/unselected-model.bin').exists())
            self.assertNotIn('admin_password',common.read_json(s/'private/request.json'))
            self.assertEqual((h/'gateway-admin/.env').stat().st_mode & 0o777,0o600)
            self.assertIn(('/usr/local/bin/modelctl','boot'),commands)
            self.assertTrue(any('--selected-json' in command for command in commands))
    def test_missing_source_prevents_payload_and_system_stages(self):
        with ExitStack() as stack:
            b,s,h,m=self.fixture(stack)
            settings=common.read_json(s/'private/request.json');settings['sources']['local_roots']=[];common.atomic_json(s/'private/request.json',settings)
            o=engine.Installer(b,s,h);commands=self.simulate_commands(o,m)
            with self.assertRaisesRegex(RuntimeError,'资源'):o.run()
            self.assertEqual(o.state['current'],'acquire');self.assertEqual(o.state['status'],'failed')
            self.assertEqual(list(h.iterdir()),[]);self.assertFalse(commands);self.assertFalse(o.state['completed'])

if __name__=='__main__':unittest.main(verbosity=2)
