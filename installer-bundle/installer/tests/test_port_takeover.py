"""Real backup/recovery and listener termination; service/Docker calls are isolated."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import port_takeover
import components
import common
import test_workflow


class TakeoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.calls=[];self.docker=[]
        self.owner=SimpleNamespace(root=self.root/'state',storage=SimpleNamespace(data_root=self.root/'bulk'),
          state={},update=mock.Mock(),event=mock.Mock())
        self.flow=SimpleNamespace(owner=self.owner,run=self.command)
        self.take=port_takeover.Takeover(self.flow,'test');self.take.begin()

    def command(self,args,**kwargs):
        args=list(map(str,args));self.calls.append(args)
        stdout='';code=0
        if args[:3]==['docker','ps','-aq']:stdout='\n'.join(c['Id'] for c in self.docker)
        elif args[:2]==['docker','inspect']:stdout=json.dumps([c for c in self.docker if c['Id'] in args[2:]])
        elif args[:2]==['docker','stop']:
            next(c for c in self.docker if c['Id']==args[-1])['State']['Running']=False
        elif args[:2]==['docker','start']:
            next(c for c in self.docker if c['Id']==args[-1])['State']['Running']=True
        elif args[:2]==['docker','rename']:
            next(c for c in self.docker if c['Id']==args[-2])['Name']='/'+args[-1]
        elif args[0]=='docker':pass
        elif args[0]=='systemctl':
            if args[1]=='show':stdout='LoadState=not-found\nActiveState=inactive\nUnitFileState=\nFragmentPath=\nTriggeredBy='
        elif args[0]=='findmnt':stdout='/'
        else:return subprocess.run(args,capture_output=True,text=True,check=False,timeout=30)
        return SimpleNamespace(returncode=code,stdout=stdout,stderr='')

    def test_backup_is_verified_and_survives_new_data_and_repeated_archive(self):
        data=self.root/'mysql';data.mkdir();(data/'db').write_bytes(b'old-database')
        self.take.archive(data)
        row=self.take.record['paths'][str(data)]
        self.assertEqual((Path(row['backup'])/'db').read_bytes(),b'old-database')
        data.mkdir();(data/'db').write_bytes(b'new-database')
        self.take.archive(data)
        self.assertEqual((data/'db').read_bytes(),b'new-database')
        self.take.rollback()
        self.assertEqual((data/'db').read_bytes(),b'old-database')
        self.take.rollback()
        self.assertEqual((data/'db').read_bytes(),b'old-database')

    def test_corrupt_backup_cannot_replace_or_delete_current_data(self):
        data=self.root/'database';data.mkdir();(data/'db').write_bytes(b'old')
        self.take.archive(data)
        row=self.take.record['paths'][str(data)]
        (Path(row['backup'])/'db').write_bytes(b'corrupt')
        data.mkdir();(data/'db').write_bytes(b'new')
        with self.assertRaisesRegex(RuntimeError,'备份'):self.take.rollback()
        self.assertEqual((data/'db').read_bytes(),b'new')

    def test_top_level_symlink_and_nested_mount_are_refused(self):
        outside=self.root/'outside';outside.mkdir();(outside/'keep').write_text('preserve')
        link=self.root/'link';link.symlink_to(outside)
        with self.assertRaises(RuntimeError):self.take.archive(link)
        original=self.flow.run
        self.flow.run=lambda args,**kw:SimpleNamespace(stdout=str(outside/'mount')) if args[0]=='findmnt' else original(args,**kw)
        with self.assertRaisesRegex(RuntimeError,'挂载'):self.take.archive(outside)
        self.assertEqual((outside/'keep').read_text(),'preserve')

    def container(self,name,port):
        return {'Id':('a' if name=='old-db' else 'b')*64,'Name':'/'+name,'State':{'Running':True},
          'HostConfig':{'PortBindings':{'3306/tcp':[{'HostPort':str(port)}]},'RestartPolicy':{'Name':'always','MaximumRetryCount':0}},
          'Config':{'Env':['PASSWORD=private-fixture-secret']}}

    def test_foreign_named_container_is_stopped_archived_and_can_be_restored(self):
        self.docker=[self.container('old-db',3306),self.container('unselected',6379)]
        with mock.patch('port_takeover.free_ports',return_value=True),mock.patch('port_takeover.listeners',return_value=[]):self.take.claim([3306])
        self.assertFalse(self.docker[0]['State']['Running'])
        self.assertTrue(self.docker[0]['Name'].startswith('/ai-platform-old-'))
        self.assertTrue(self.docker[1]['State']['Running'])
        self.assertIn(['docker','update','--restart=no','a'*64],self.calls)
        self.assertFalse(any('-v' in c or 'prune' in c for c in self.calls))
        self.assertNotIn('private-fixture-secret',str(self.owner.event.call_args_list))
        self.take.rollback()
        self.assertEqual(self.docker[0]['Name'],'/old-db')
        self.assertTrue(self.docker[0]['State']['Running'])

    def test_socket_owner_parser_covers_ipv4_ipv6_and_multiple_pids(self):
        text='LISTEN 0 128 0.0.0.0:3306 0.0.0.0:* users:(("x",pid=12,fd=1))\n'
        text+='LISTEN 0 128 [::]:3306 [::]:* users:(("x",pid=13,fd=1),("x",pid=14,fd=2))'
        self.assertEqual(port_takeover.listeners(text,{3306}),[(3306,{12}),(3306,{13,14})])
        self.assertEqual(port_takeover.listeners(text,{6379}),[])

    def test_missing_unit_is_masked_to_prevent_package_auto_start(self):
        self.take.stop_unit('ai-platform-test-missing.service')
        self.assertIn(['systemctl','mask','ai-platform-test-missing.service'],self.calls)
        self.take.release_unit('ai-platform-test-missing.service')
        self.assertTrue(self.take.record['units']['ai-platform-test-missing.service']['released'])

    def test_control_ports_and_system_services_are_not_process_killed(self):
        for port in [22,9090,0,65536]:
            with self.assertRaises(RuntimeError):self.take.claim([port])
        with self.assertRaises(RuntimeError):self.take.stop_unit('docker.service')
        with self.assertRaises(RuntimeError):self.take.stop_pid(1)

    def test_real_ipv4_and_ipv6_listener_is_terminated_before_port_claim(self):
        for family in [socket.AF_INET,socket.AF_INET6]:
            with self.subTest(family=family):
                script='import socket,time; s=socket.socket('+str(int(family))+',socket.SOCK_STREAM); '
                script+='s.bind(('+repr('127.0.0.1' if family==socket.AF_INET else '::1')+',0)); s.listen(); print(s.getsockname()[1],flush=True); time.sleep(60)'
                child=subprocess.Popen([sys.executable,'-u','-c',script],stdout=subprocess.PIPE,text=True)
                try:
                    port=int(child.stdout.readline())
                    self.assertFalse(port_takeover.free_ports([port]))
                    self.take.claim([port])
                    child.wait(timeout=5)
                    self.assertTrue(port_takeover.free_ports([port]))
                finally:
                    if child.poll() is None:child.kill();child.wait()
                    child.stdout.close()


class OrderingTests(unittest.TestCase):
    def test_containerd_manifest_identity_and_classic_config_identity_are_both_verified(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter=test_workflow.ComponentSelectionExecutionTests().adapter(Path(folder),['mysql'])
            row=adapter.images['mysql']
            for identifier in [row['config_id'],row['manifest_digest']]:
                info={'Id':identifier,'Architecture':'arm64','Os':'linux','RootFS':{'Layers':row['rootfs_diff_ids']}}
                adapter.flow.run=mock.Mock(return_value=SimpleNamespace(returncode=0,stdout=json.dumps([info])))
                adapter.image('mysql')
                self.assertEqual(adapter.spec['services']['mysql']['image'],identifier)
            info['RootFS']['Layers']=['sha256:'+'f'*64]
            adapter.flow.run.return_value.stdout=json.dumps([info])
            with self.assertRaisesRegex(RuntimeError,'校验失败'):adapter.image('mysql')

    def test_resource_failure_prevents_any_takeover(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter=test_workflow.ComponentSelectionExecutionTests().adapter(Path(folder),['mysql'])
            with (mock.patch.object(adapter,'image',side_effect=RuntimeError('offline image missing')),
                  mock.patch('port_takeover.Takeover',side_effect=AssertionError('must not stop old services'))):
                with self.assertRaisesRegex(RuntimeError,'offline image missing'):adapter.install()

    def test_resume_reuses_committed_task_container_without_archiving_data(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter=test_workflow.ComponentSelectionExecutionTests().adapter(Path(folder),['mysql'])
            txn=mock.Mock(record={'status':'committed','created':['current-id']})
            info={'Id':'current-id','State':{'Running':True}}
            with mock.patch.object(adapter,'image'),mock.patch.object(adapter,'inspect',return_value=info),mock.patch.object(adapter,'compatible'),mock.patch.object(adapter,'healthy',return_value=True),mock.patch.object(adapter,'export_connections'),mock.patch('port_takeover.Takeover',return_value=txn):
                adapter.install()
            txn.archive.assert_not_called();txn.claim.assert_not_called();txn.rollback.assert_not_called()
