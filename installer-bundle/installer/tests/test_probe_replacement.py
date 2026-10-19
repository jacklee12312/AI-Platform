"""Replace real fixture files, including rollback and interrupted replacement recovery."""
import json
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import atomic_json, sha256
from probe import Probe
from probe_replace import Replacement


class ProbeReplacementTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        root=Path(self.temporary.name)
        self.owner=SimpleNamespace(state={},update=mock.Mock(),event=mock.Mock())
        self.flow=SimpleNamespace(owner=self.owner,directory=root/'private/workflow',
            settings={'probe':{'mode':'auto','reuse_existing':True}},
            host_packages=mock.Mock(),asset=mock.Mock(),
            run=mock.Mock(side_effect=lambda args,**kw:SimpleNamespace(stdout='enabled' if args[1]=='is-enabled' else 'active',returncode=0)))
        self.probe=Probe(self.flow,root/'config',root/'bin/data-probe',root/'units',root/'lib')
        self.probe.binary.parent.mkdir();self.probe.binary.write_bytes(b'old executable')
        self.probe.config_root.mkdir();self.probe.unit_root.mkdir()
        atomic_json(self.probe.config,{'local_server':{'port':9090},'log':{'dir':'/var/log/data-probe'},
            'register':{'project':'original-project','bindActivateToken':'fixture-only-secret'},
            'gateway':{'enabled':False,'url':'http://original-target'}})
        self.registration=self.probe.config_root/'registration.yaml'
        self.registration.write_bytes(b'original-device-registration')
        (self.probe.config_root/'certs').mkdir();(self.probe.config_root/'certs/ca.pem').write_bytes(b'original CA')
        (self.probe.unit_root/'data-probe.service').write_bytes(b'old service')
        dropin=self.probe.unit_root/'data-probe.service.d';dropin.mkdir();(dropin/'old.conf').write_bytes(b'old override')
        self.original={p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}
        self.source=root/'archive/data-probe-offline'
        for name,data in [('build/data-probe',b'pinned executable'),('deploy/data-probe.service',b'new service'),
                          ('deploy/data-probe-rollback.service',b'new rollback unit'),('deploy/data-probe-rollback.sh',b'new rollback script')]:
            p=self.source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        archive=root/'probe.tar.gz'
        with tarfile.open(archive,'w:gz') as tar:tar.add(self.source,arcname='data-probe-offline')
        self.flow.asset.return_value=archive
        self.flow.lock={'probe':{'binary_sha256':sha256(self.source/'build/data-probe'),'archive':{}},'probe_packages':['fixture-dependency']}
        self.root=root
        self.target={'source':self.source,'binary_sha256':sha256(self.source/'build/data-probe'),'version':'1.1.2-current'}
        patcher=mock.patch('probe.fetch_probe',return_value=self.target)
        self.fetch=patcher.start();self.addCleanup(patcher.stop)

    def assert_restored(self):
        for relative,data in self.original.items():self.assertEqual((self.root/relative).read_bytes(),data,relative)
        self.assertFalse((self.probe.unit_root/'data-probe-rollback.service').exists())
        self.assertFalse((self.probe.binary.parent/'data-probe-rollback.sh').exists())
        self.assertEqual(self.owner.state['private']['probe_replacement']['status'],'rolled_back')
        self.assertIn(mock.call(['systemctl','start','data-probe']),self.flow.run.call_args_list)

    def test_different_binary_replaced_and_all_registration_configuration_bytes_preserved(self):
        with mock.patch.object(self.probe,'activate') as activate:self.probe.install()
        activate.assert_called_once()
        self.assertEqual(self.probe.binary.read_bytes(),b'pinned executable')
        self.assertEqual((self.probe.unit_root/'data-probe.service').read_bytes(),b'new service')
        for relative,data in self.original.items():
            if relative.parts[0]=='config':self.assertEqual((self.root/relative).read_bytes(),data)
        self.assertFalse((self.probe.unit_root/'data-probe.service.d/old.conf').exists())
        self.assertTrue(self.owner.state['probe_config_preserved']);self.assertTrue(self.owner.state['probe_replaced'])
        self.assertEqual(self.owner.state['private']['probe_replacement']['status'],'committed')
        backup=Path(self.owner.state['private']['probe_replacement']['backup'])
        self.assertEqual(backup.stat().st_mode&0o777,0o700)
        self.assertNotIn('fixture-only-secret',json.dumps(self.owner.state))
        with mock.patch.object(self.probe,'binding',side_effect=AssertionError('configuration must remain unchanged')):
            self.probe.enable_collectors()

    def test_failed_activation_restores_program_service_and_registration(self):
        def fail():
            self.registration.write_bytes(b'failed-new-registration')
            self.probe.config.write_bytes(b'failed-config')
            raise RuntimeError('MQTT unavailable')
        with mock.patch.object(self.probe,'activate',side_effect=fail),self.assertRaisesRegex(RuntimeError,'MQTT unavailable'):
            self.probe.install()
        self.assert_restored();self.assertNotIn('probe_config_preserved',self.owner.state)

    def test_failed_publish_restores_original_files(self):
        from common import copy_atomic
        def publish(source,target,*args,**kwargs):
            if target.name=='data-probe.service':raise OSError('fixture disk error')
            return copy_atomic(source,target,*args,**kwargs)
        with mock.patch('probe_replace.copy_atomic',side_effect=publish),self.assertRaisesRegex(OSError,'fixture disk error'):
            self.probe.install()
        self.assert_restored()

    def test_interrupted_replacement_restored_before_retry_classification(self):
        manager=Replacement(self.probe);manager.prepare();manager.deploy(self.source)
        self.assertFalse(self.probe.skip_existing())
        Replacement.recover(self.probe);self.assert_restored()
        self.assertFalse(self.probe.skip_existing())

    def test_corrupt_backup_rejected_before_touching_live_program(self):
        manager=Replacement(self.probe);manager.prepare();manager.deploy(self.source)
        backup=Path(manager.record['backup']);(backup/'0').write_bytes(b'corrupt')
        self.flow.run.reset_mock()
        with self.assertRaisesRegex(RuntimeError,'备份文件校验失败'):manager.restore()
        self.assertEqual(self.probe.binary.read_bytes(),b'pinned executable');self.flow.run.assert_not_called()

    def test_wrong_archive_binary_rejected_before_stopping_existing_probe(self):
        self.target['binary_sha256']='0'*64
        with self.assertRaisesRegex(RuntimeError,'包内二进制摘要不符'):self.probe.install()
        self.flow.run.assert_not_called();self.assertEqual(self.probe.binary.read_bytes(),b'old executable')

    def test_missing_registration_or_active_ota_never_replaces_old_executable(self):
        for condition in ('empty-registration','ota'):
            with self.subTest(condition=condition):
                self.registration.write_bytes(b'' if condition=='empty-registration' else b'original-device-registration')
                if condition=='ota':(self.probe.config_root/'.upgrading').touch()
                with self.assertRaisesRegex(RuntimeError,'注册缓存|自升级'):self.probe.install()
                self.assertEqual(self.probe.binary.read_bytes(),b'old executable')
                self.assertFalse(any(c.args[0][:2]==['systemctl','stop'] for c in self.flow.run.call_args_list))


if __name__=='__main__':unittest.main(verbosity=2)
