"""Release gates and normal worker orchestration. No Docker or accelerator mutations."""
import copy, json, os, subprocess, sys, tempfile, unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import common, engine

BUNDLE=Path(__file__).resolve().parents[2]

class ReleaseContractTests(unittest.TestCase):
    def setUp(self):
        self.models=json.loads((BUNDLE/'catalog/platform-manifest.json').read_text())['models']
        names={'containers-and-autostart','active-accelerator-real-inference','authenticated-gateway-registry','restore-mode:copaw','gateway-sse-and-usage'}
        names.update('accelerator-mode:'+m for m in common.ACCELERATOR_MODELS)
        names.update('inference:'+m for m in set(self.models)-set(common.ACCELERATOR_MODELS.values()))
        self.report={'run_id':'this-run','scope':'all-modes','passed':True,'registry_models':self.models,'checks':[{'name':n,'passed':True} for n in sorted(names)]}
    def test_complete_report_passes(self):
        self.assertEqual(len(self.report['checks']),30)
        common.validate_acceptance(self.report,'this-run',self.models)
    def test_incomplete_stale_false_or_duplicate_report_rejected(self):
        mutations=[lambda r:r.update(run_id='old-run'),lambda r:r.update(scope='active-mode-and-cpu'),lambda r:r.update(passed=False),lambda r:r.update(registry_models=self.models[:-1]),lambda r:r['checks'].pop(),lambda r:r['checks'].append(r['checks'][0]),lambda r:r['checks'][0].update(passed=False)]
        for change in mutations:
            r=copy.deepcopy(self.report);change(r)
            with self.subTest(report=r),self.assertRaises(RuntimeError):common.validate_acceptance(r,'this-run',self.models)
    def test_locked_backend_parameters(self):
        text=(BUNDLE/'installer/tests/fixtures/compose.yaml').read_text()
        common.validate_runtime_config(text)
        for invalid in [text.replace("    - '8'","    - '10'"),text.replace('  bge-m3:','  missing-bge:')]:
            with self.assertRaises(RuntimeError):common.validate_runtime_config(invalid)
    def test_failure_includes_fresh_report_detail(self):
        with tempfile.TemporaryDirectory() as d:
            o=object.__new__(engine.Installer);o.home=Path(d);o.resources=Path(d);o.state={};o.manifest={'models':self.models};o.selected=self.models;o.device_count=2;o.update=mock.Mock();o.event=mock.Mock()
            def fail(*args,**kwargs):
                if '--selected-json' in args:
                    common.atomic_json(o.home/'houmo-docker-deployment/state/offline-acceptance.json',{'run_id':o.state['acceptance_run_id'],'error':'accelerator-mode:bge-m3: failed'})
                    raise subprocess.CalledProcessError(1,['verify_runtime'])
            o.command=mock.Mock(side_effect=fail)
            with self.assertRaisesRegex(RuntimeError,'accelerator-mode:bge-m3'):o.acceptance()
            self.assertNotIn('acceptance',o.state)

class NormalWorkerTests(unittest.TestCase):
    """Normal Installer.run + real journal writes; host actions are injected fixtures."""
    def environment(self,stack):
        root=Path(stack.enter_context(tempfile.TemporaryDirectory()))
        home=root/'home';home.mkdir();state=root/'state';state.mkdir();bundle=root/'bundle'
        common.atomic_json(bundle/'release.json',{'release_id':'fixture'})
        common.atomic_json(bundle/'resources/manifests/manifest.json',{'docker_disk_budget_bytes':0})
        stack.enter_context(mock.patch('engine.detect_device_count',return_value=2))
        stack.enter_context(mock.patch('engine.quick_checks',return_value=[]))
        stack.enter_context(mock.patch('engine.subprocess.check_output',return_value='install ok installed'))
        stack.enter_context(mock.patch('engine.subprocess.run',return_value=mock.Mock(returncode=1,stdout='')))
        stack.enter_context(mock.patch('engine.shutil.which',return_value=None))
        stack.enter_context(mock.patch('engine.shutil.disk_usage',return_value=mock.Mock(free=100*1024**3)))
        return bundle,state,home
    def configure(self,obj,fail=None):
        calls=[];obj.base=mock.Mock()
        def action(name):
            def run():
                calls.append(name)
                if name==fail:raise RuntimeError('injected '+name)
                if name=='credentials':
                    for rel in ['gateway-admin/.env','houmo-docker-deployment/.env']:
                        p=obj.home/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('fixture-secret');p.chmod(0o600)
            return run
        for name,_ in common.STEPS:
            if name=='images':obj.base.images=mock.Mock(side_effect=lambda m:action('images')())
            else:setattr(obj,name.replace('-','_'),action(name))
        return calls
    def test_first_install_uses_all_stages_in_order(self):
        with ExitStack() as stack:
            b,s,h=self.environment(stack);o=engine.Installer(b,s,h);calls=self.configure(o);o.run()
            expected=[n for n,_ in common.STEPS]
            self.assertEqual(calls,expected);self.assertEqual(common.read_json(o.public)['status'],'installed');self.assertEqual(o.state['completed'],expected)
    def test_each_stage_failure_then_normal_resume(self):
        for stage,_ in common.STEPS:
            with self.subTest(stage=stage),ExitStack() as stack:
                b,s,h=self.environment(stack);first=engine.Installer(b,s,h);calls=self.configure(first,stage)
                with self.assertRaisesRegex(RuntimeError,'injected'):first.run()
                self.assertEqual(first.state['status'],'failed');self.assertEqual(calls[-1],stage)
                old_env=(h/'gateway-admin/.env').read_bytes() if (h/'gateway-admin/.env').exists() else None
                resumed=engine.Installer(b,s,h);calls=self.configure(resumed);resumed.run()
                self.assertEqual(resumed.state['status'],'installed');self.assertNotIn('error',resumed.state)
                if old_env is not None:
                    self.assertNotIn('credentials',calls);self.assertEqual((h/'gateway-admin/.env').read_bytes(),old_env)
    def test_preflight_failure_has_no_install_actions(self):
        with ExitStack() as stack:
            b,s,h=self.environment(stack);o=engine.Installer(b,s,h);calls=self.configure(o)
            with mock.patch('engine.quick_checks',return_value=[{'name':'fixture','ok':False,'detail':'unsupported host'}]),self.assertRaises(RuntimeError):o.run()
            self.assertEqual(calls,[]);self.assertEqual(o.state['completed'],[])

if __name__=='__main__':unittest.main(verbosity=2)
