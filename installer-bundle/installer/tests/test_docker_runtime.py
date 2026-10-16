"""Exact Docker replacement, dependency refusal and restart after a partial uninstall."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from docker_runtime import DockerRuntime,PACKAGES,DOCKER


class DockerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.paths=[root/(name+'.deb') for name in PACKAGES]
        self.versions={name:'1.0-fixture' for name in PACKAGES}
        self.installed=dict(self.versions,**{'containerd.io':'2.0-fixture'})
        self.daemon=True;self.fail_install=False;self.fail_dry_run=False
        self.containers={'a'*64:{'Running':True,'Paused':False},'b'*64:{'Running':False,'Paused':False}}
        self.original=copy.deepcopy(self.containers);self.calls=[]
        self.config=root/'etc/docker';self.config.mkdir(parents=True)
        (self.config/'daemon.json').write_text('{"fixture-secret":"private-value"}')
        self.owner=SimpleNamespace(state={},update=mock.Mock(),event=mock.Mock())
        self.flow=SimpleNamespace(owner=self.owner,directory=root/'private/workflow',run=self.run_command)
        self.runtime=DockerRuntime(self.flow,(self.config,root/'etc/containerd'))
        patch=mock.patch('docker_runtime.shutil.which',return_value='/usr/bin/docker');patch.start();self.addCleanup(patch.stop)

    def run_command(self,args,**kw):
        args=list(map(str,args));self.calls.append(args);text='';code=0
        if args[0]=='dpkg-deb':
            name=Path(args[2]).name[:-4]
            text={'Package':name,'Version':self.versions.get(name,''),'Architecture':'arm64'}[args[3]]
        elif args[0]=='dpkg-query':
            text='install ok installed|'+self.installed[args[-1]] if args[-1] in self.installed else ''
        elif args[0]=='dpkg':
            if '--no-act' in args:
                if self.fail_dry_run:raise RuntimeError('fixture dependent package')
            elif '--remove' in args:
                for name in args[2:]:self.installed.pop(name,None)
                self.daemon=False
            elif '--install' in args:
                if self.fail_install:raise RuntimeError('fixture interrupted install')
                self.installed=dict(self.versions);self.daemon=True
                # Docker may restart an initially stopped container with an always policy.
                for state in self.containers.values():state['Running']=True;state['Paused']=False
        elif args[0]=='systemctl':
            if args[1]=='show':text='loaded'
            elif args[1]=='stop':self.daemon=False
            elif args[1]=='enable':self.daemon=True
        elif args[:3]==DOCKER:
            command=args[3:]
            if not self.daemon:
                code=1
            elif command[0]=='info':text=json.dumps({'OSType':'linux','Architecture':'aarch64','DockerRootDir':'/fixture/docker'})
            elif command[0]=='ps':text='\n'.join(self.containers)
            elif command[0]=='inspect':text=json.dumps(self.containers[command[-1]])
            elif command[0]=='image':text='sha256:'+'c'*64
            elif command[0]=='volume':text='original-volume'
            elif command[0] in ('stop','start','pause','unpause'):
                for identifier in command[1:]:
                    if identifier not in self.containers:continue
                    key='Running' if command[0] in ('stop','start') else 'Paused'
                    self.containers[identifier][key]=command[0] in ('start','pause')
        if code and kw.get('check',True):raise RuntimeError('fixture daemon unavailable')
        return SimpleNamespace(stdout=text,stderr='',returncode=code)

    def test_newer_packages_are_removed_before_exact_install_without_erasing_data(self):
        self.runtime.install(self.paths)
        remove=next(i for i,c in enumerate(self.calls) if c[:2]==['dpkg','--remove'])
        install=next(i for i,c in enumerate(self.calls) if c[:3]==['dpkg','--force-confold','--install'])
        self.assertLess(remove,install);self.assertEqual(self.installed,self.versions)
        self.assertEqual(self.containers,self.original)
        self.assertEqual((self.config/'daemon.json').read_text(),'{"fixture-secret":"private-value"}')
        record=self.owner.state['private']['docker_replacement']
        self.assertEqual(record['status'],'committed');self.assertEqual(record['running'],['a'*64])
        self.assertNotIn('private-value',json.dumps(self.owner.state))
        self.assertFalse(any(value in {'--purge','prune','rm','rmi','down'} for c in self.calls for value in c))

    def test_matching_set_reused_without_removing_packages_or_stopping_containers(self):
        self.installed=dict(self.versions);self.runtime.install(self.paths)
        self.assertNotIn('private',self.owner.state)
        self.assertFalse(any(c[0]=='dpkg' for c in self.calls))
        self.assertFalse(any(c[:4]==DOCKER+['stop'] for c in self.calls))
        self.assertEqual(self.containers,self.original)

    def test_install_failure_retains_inventory_and_retry_works_with_daemon_down(self):
        self.fail_install=True
        with self.assertRaisesRegex(RuntimeError,'interrupted install'):self.runtime.install(self.paths)
        self.assertEqual(self.owner.state['private']['docker_replacement']['status'],'installing')
        self.assertFalse(self.daemon)
        self.fail_install=False;DockerRuntime(self.flow,self.runtime.config_roots).install(self.paths)
        self.assertEqual(self.containers,self.original)
        self.assertEqual(self.owner.state['private']['docker_replacement']['status'],'committed')

    def test_dependency_check_failure_occurs_before_stopping_any_service(self):
        self.fail_dry_run=True
        with self.assertRaisesRegex(RuntimeError,'dependent package'):self.runtime.install(self.paths)
        self.assertEqual(self.containers,self.original);self.assertTrue(self.daemon)
        self.assertFalse(any(c[:2]==['systemctl','stop'] for c in self.calls))

    def test_preserves_original_paused_and_stopped_states(self):
        self.containers['a'*64]['Paused']=True;self.original=copy.deepcopy(self.containers)
        self.runtime.install(self.paths);self.assertEqual(self.containers,self.original)

    def test_missing_docker_is_installed_with_empty_container_inventory(self):
        self.installed={};self.daemon=False;self.containers={}
        def executable(name):return '/usr/bin/docker' if self.daemon else None
        with mock.patch('docker_runtime.shutil.which',side_effect=executable):self.runtime.install(self.paths)
        self.assertEqual(self.installed,self.versions)
        self.assertFalse(any(c[:2]==['dpkg','--remove'] for c in self.calls))

    def test_unavailable_existing_daemon_blocks_before_uninstall_without_a_prior_record(self):
        self.daemon=False
        with self.assertRaisesRegex(RuntimeError,'无法记录'):self.runtime.install(self.paths)
        self.assertFalse(any(c[:2]==['dpkg','--remove'] for c in self.calls))

    def test_retry_of_completed_install_restores_containers_without_reinstalling(self):
        self.runtime.install(self.paths)
        self.owner.state['private']['docker_replacement']['status']='restoring'
        self.containers['a'*64]['Running']=False;self.calls=[]
        self.runtime.install(self.paths)
        self.assertEqual(self.containers,self.original)
        self.assertFalse(any(c[0]=='dpkg' for c in self.calls))

    def test_invalid_package_set_is_refused_before_any_mutation(self):
        with self.assertRaisesRegex(RuntimeError,'名称或架构'):self.runtime.install(self.paths+[self.paths[0]])
        self.assertFalse(any(c[0] in ('dpkg','systemctl') for c in self.calls))


if __name__=='__main__':unittest.main(verbosity=2)
