"""Containerd-aware capacity checks and preservation across storage migration failures."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import container_storage as cs

G = 1024**3


class ContainerStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name);self.system = self.root/'system';self.bulk = self.root/'bulk'
        self.sources = [self.system/'docker', self.system/'containerd']
        for p in self.sources:
            p.mkdir(parents=True);(p/'data').write_bytes(b'original persistent data')
        self.bulk.mkdir();self.calls = [];self.system_free = 10*G;self.bulk_free = 200*G
        self.owner = SimpleNamespace(storage=SimpleNamespace(data_root=self.bulk/'installer'), state={},
                                     update=mock.Mock(), event=mock.Mock())
        self.owner.workflow = SimpleNamespace(owner=self.owner, run=self.command)
        patch=mock.patch.object(cs,'trusted_directory');patch.start();self.addCleanup(patch.stop)
        self.manager = cs.ContainerStorage(self.owner, self.root/'record/migration.json', self.root/'units')
        self.manager.runtime.inventory = mock.Mock(return_value={'data_root':str(self.sources[0]),
            'containers':['a'*64], 'running':['a'*64], 'paused':[], 'images':['original-image'], 'volumes':['original-volume']})
        self.manager.runtime.restore_containers = mock.Mock()
        self.manager.mounted = mock.Mock(return_value=True)
        self.detected = {'stores':dict.fromkeys(self.sources),'docker':self.sources[0],'images':self.sources[1]}
        self.entries = [(self.sources[0],2*G,'Docker metadata'),(self.sources[1],30*G,'images'),
                        (self.bulk,60*G,'models, cache and temporary work')]
        for name, value in [('budgets',self.budgets),('existing',self.existing)]:
            patch=mock.patch.object(cs,name,value);patch.start();self.addCleanup(patch.stop)

    def existing(self,p):
        p=Path(p);device=1 if p.is_relative_to(self.system) else 2
        return SimpleNamespace(stat=lambda:SimpleNamespace(st_dev=device),__str__=lambda:str(p))

    def budgets(self,entries):
        rows={}
        for p,n,purpose in entries:
            device=1 if Path(p).is_relative_to(self.system) else 2
            row=rows.setdefault(device,{'path':str(p),'need_bytes':0,
                'free_bytes':self.system_free if device==1 else self.bulk_free,'purposes':[]})
            row['need_bytes']+=n;row['purposes'].append(purpose)
        return list(rows.values())

    def command(self,args,**kw):
        args=list(map(str,args));self.calls.append(args);out='';code=0
        if args[0]=='findmnt':
            if '-J' in args:out=json.dumps({'filesystems':[{'target':'/','fstype':'ext4'}]})
        elif args[0]=='systemd-escape':out=args[-1].strip('/').replace('/','-')+'.mount'
        elif args[:2]==['systemctl','show']:out='loaded'
        elif args[0]=='cp':return subprocess.run(args,capture_output=True,text=True,check=True)
        elif args[0]=='ctr':out='moby'
        return SimpleNamespace(stdout=out,stderr='',returncode=code)

    def test_enough_space_does_not_stop_or_reconfigure_existing_services(self):
        self.system_free=100*G
        self.manager.prepare(self.entries,self.detected)
        self.assertEqual(self.calls,[])
        self.assertFalse(self.manager.record_path.exists())

    def test_insufficient_target_is_rejected_before_stopping_original_containers(self):
        self.bulk_free=70*G
        with self.assertRaisesRegex(RuntimeError,'空间不足'):self.manager.prepare(self.entries,self.detected)
        self.assertEqual(self.calls,[])
        self.assertFalse(self.manager.record_path.exists())

    def test_separate_containerd_partition_is_moved_even_when_docker_root_has_space(self):
        docker=self.bulk/'docker';docker.mkdir()
        detected={'stores':{docker:None,self.sources[1]:None},'docker':docker,'images':self.sources[1]}
        entries=[(docker,2*G,'Docker metadata'),(self.sources[1],30*G,'images'),(self.bulk,60*G,'models')]
        self.manager.prepare(entries,detected)
        self.assertEqual([r['source'] for r in self.manager.record['moves']],[str(self.sources[1])])

    def test_fresh_box_creates_empty_stores_before_runtime_packages_are_installed(self):
        for p in self.sources:__import__('shutil').rmtree(p)
        self.manager.runtime.inventory.return_value={'data_root':None,'containers':[],
            'running':[],'paused':[],'images':[],'volumes':[]}
        self.manager.prepare(self.entries,self.detected)
        self.assertEqual(self.manager.record['phase'],'committed')
        self.manager.runtime.restore_containers.assert_not_called()
        for src,dst,_ in self.manager.paths():self.assertTrue(src.is_dir() and dst.is_dir())

    def test_both_stores_migrate_verified_with_original_paths_and_data_retained(self):
        self.manager.prepare(self.entries,self.detected)
        self.assertEqual(self.manager.record['phase'],'committed')
        self.assertEqual(len(self.manager.record['moves']),2)
        for src,dst,unit in self.manager.paths():
            self.assertEqual(cs.tree_digest(src),cs.tree_digest(dst))
            self.assertEqual((src/'data').read_bytes(),b'original persistent data')
            content=(self.manager.units_dir/unit).read_text()
            self.assertIn('Where='+str(src),content)
            self.assertIn('RequiresMountsFor=',content)
        self.manager.runtime.restore_containers.assert_called_once()
        self.assertLess(next(i for i,c in enumerate(self.calls) if c[0]=='cp'),
                        next(i for i,c in enumerate(self.calls) if c[:2]==['systemctl','enable']))

    def test_generated_mount_units_parse_without_ignored_scalar_paths(self):
        self.manager.prepare(self.entries,self.detected)
        result=subprocess.run(['systemd-analyze','verify',*[str(self.manager.units_dir/u)
            for _,_,u in self.manager.paths()]],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('ignoring:',result.stderr.lower())

    def test_copy_corruption_restores_original_services_without_activating_mounts(self):
        with mock.patch.object(cs,'tree_digest',side_effect=['original','corrupt']):
            with self.assertRaisesRegex(RuntimeError,'副本校验失败'):self.manager.prepare(self.entries,self.detected)
        self.assertEqual(self.manager.record['phase'],'copying')
        self.manager.runtime.restore_containers.assert_called_once()
        self.assertFalse(any(c[:2]==['systemctl','enable'] for c in self.calls))
        self.assertEqual((self.sources[0]/'data').read_bytes(),b'original persistent data')

    def test_activation_recovery_never_recopies_old_data_over_new_writes(self):
        self.manager.prepare(self.entries,self.detected)
        for _,dst,_ in self.manager.paths():(dst/'data').write_bytes(b'new writes after activation')
        self.manager.save('activating');self.calls.clear()
        self.manager.recover()
        self.assertFalse(any(c[0]=='cp' for c in self.calls))
        for _,dst,_ in self.manager.paths():self.assertEqual((dst/'data').read_bytes(),b'new writes after activation')

    def test_partial_copy_recovery_replaces_only_our_unactivated_copy(self):
        with mock.patch.object(cs,'tree_digest',side_effect=['original','corrupt']):
            with self.assertRaises(RuntimeError):self.manager.prepare(self.entries,self.detected)
        dst=Path(self.manager.record['moves'][0]['target']);(dst/'unverified-extra').write_text('partial')
        self.manager.recover()
        self.assertEqual(self.manager.record['phase'],'committed')
        self.assertFalse((dst/'unverified-extra').exists())

    def test_digest_detects_content_mode_symlink_and_hardlink_changes(self):
        src=self.sources[0];(src/'hard').hardlink_to(src/'data');(src/'link').symlink_to('data')
        dst=self.bulk/'copy';subprocess.run(['cp','-a',str(src),str(dst)],check=True)
        pin=cs.tree_digest(src);self.assertEqual(pin,cs.tree_digest(dst))
        (dst/'hard').unlink();(dst/'hard').write_bytes((dst/'data').read_bytes())
        self.assertNotEqual(pin,cs.tree_digest(dst))
        (dst/'data').chmod(0o600);self.assertNotEqual(pin,cs.tree_digest(dst))

    def test_containerd_root_is_detected_separately_from_docker_root(self):
        def run(args,**kw):
            if args[0]=='docker':out=json.dumps({'DockerRootDir':str(self.sources[0]),'DriverStatus':[['driver-type','io.containerd.snapshotter.v1']]})
            elif args[0]=='systemctl':out='0'
            else:out='root = '+json.dumps(str(self.sources[1]))+'\n'
            return SimpleNamespace(returncode=0,stdout=out)
        with mock.patch.object(cs.shutil,'which',return_value='/usr/bin/tool'):
            detected=cs.layout(run)
        self.assertEqual(detected['images'],self.sources[1]);self.assertEqual(len(detected['stores']),2)

    def test_fresh_box_without_docker_still_plans_containerd_store(self):
        with mock.patch.object(cs.shutil,'which',return_value=None):detected=cs.layout(mock.Mock())
        self.assertEqual(detected['docker'],Path('/var/lib/docker'))
        self.assertEqual(detected['images'],Path('/var/lib/containerd'))


if __name__=='__main__':unittest.main(verbosity=2)
