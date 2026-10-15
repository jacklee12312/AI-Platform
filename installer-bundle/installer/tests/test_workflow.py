"""Stage ordering, recovery, credential handling and existing-data boundaries."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import common
import components
import engine
import probe
import probe_runner
import workflow
import test_modular_worker as worker_fixture

BUNDLE=Path(__file__).resolve().parents[2]


def settings():
    return {'probe':{'platform_url':'http://platform.example:15433','reuse_existing':False,
                     **{key:'fixture-'+key for key in workflow.REGISTER_FIELDS},'bindActivateToken':'fixture-registration-secret'},
            'components':{**{key:'fixture-$-password-123' for key in workflow.PASSWORD_FIELDS},'minio_user':'fixture-minio'}}


class WorkflowTests(unittest.TestCase):
    def test_settings_validate_without_echoing_secrets(self):
        self.assertEqual(workflow.validate_settings(settings()),settings())
        for mutate in [lambda x:x['probe'].update(bindActivateToken=''),
                       lambda x:x['probe'].update(platform_url='http://user:password@platform.example'),
                       lambda x:x['components'].update(mysql_password='short')]:
            value=settings();mutate(value)
            with self.assertRaises(ValueError) as error:workflow.validate_settings(value)
            self.assertNotIn('fixture-registration-secret',str(error.exception))
    def test_reuse_requires_explicit_binding_but_not_new_activation_token(self):
        value=settings();value['probe'].update(reuse_existing=True,bindActivateToken='')
        self.assertEqual(workflow.validate_settings(value)['probe']['bindActivateToken'],'')
        value['probe']['project']=''
        with self.assertRaises(ValueError):workflow.validate_settings(value)
    def test_mqtt_loss_invalidates_earlier_connection(self):
        self.assertFalse(workflow.mqtt_connected('Registration success: fixture\nHTTP 200'))
        self.assertTrue(workflow.mqtt_connected('MQTT connected to broker:1883'))
        self.assertFalse(workflow.mqtt_connected('MQTT connected to broker:1883\nMQTT connection lost, cause=timeout'))
        self.assertTrue(workflow.mqtt_connected('MQTT connected to broker:1883\nMQTT disconnected\nMQTT connected to broker:1883'))
    def test_probe_console_redacts_registration_and_broker_credentials(self):
        text='Registration payload: {"bindActivateToken":"fixture-secret-123","mqttPassword":"broker-secret","owner":"fixture"}'
        cleaned=probe_runner.redact_line(text,('fixture-secret-123',))
        self.assertNotIn('fixture-secret-123',cleaned);self.assertNotIn('broker-secret',cleaned)
        self.assertIn('"owner":"fixture"',cleaned)
        connected='MQTT connected to broker:1883'
        self.assertEqual(probe_runner.redact_line(connected),connected)
        self.assertNotIn('bearer-value',probe_runner.redact_line('Authorization: Bearer bearer-value'))
    def test_minio_credentials_are_shared_with_milvus(self):
        lock=common.read_json(BUNDLE/'catalog/workflow.json')
        spec=components.compose_spec(lock['images'],settings()['components'])['services']
        self.assertEqual(spec['minio']['environment']['MINIO_ROOT_PASSWORD'],spec['milvus']['environment']['MINIO_SECRET_ACCESS_KEY'])
        self.assertEqual(spec['minio']['environment']['MINIO_ROOT_USER'],spec['milvus']['environment']['MINIO_ACCESS_KEY_ID'])
        self.assertEqual(spec['paradedb']['volumes'],['/data/paradedb:/var/lib/postgresql'])
        self.assertIn('--requirepass',spec['redis']['command'])
        self.assertTrue(all(value['image'].startswith('sha256:') for value in spec.values()))
    def test_compose_escapes_password_interpolation(self):
        data={'password':'p$VAR $$literal # mixed 中文','command':['a$b']}
        escaped=components.escape_compose(data)
        self.assertEqual(escaped['password'].replace('$$','$'),data['password'])
        self.assertEqual(escaped['command'],['a$$b'])
        self.assertNotEqual(escaped,data)
    def test_probe_collectors_are_disabled_until_models_are_ready(self):
        value=probe.make_config(settings()['probe'])
        self.assertFalse(value['gateway']['enabled']);self.assertFalse(value['container_log']['enabled'])
        self.assertEqual(value['gateway']['token_env_file'],'/home/cix/houmo-docker-deployment/.env')
        self.assertTrue(probe.make_config(settings()['probe'],collectors=True)['gateway']['enabled'])
    def test_probe_template_parser_reads_upstream_supported_scalar_format(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'probe.yaml'
            path.write_text('platform:\n  url: "http://platform.example:15433" # comment\nregister:\n  project: "fixture-project"\n  owner: \'O\'\'Brien\'\nlocal_server:\n  port: 9090\ngateway:\n  enabled: false\n')
            result=probe.read_probe_config(path)
            self.assertEqual(result['register']['owner'],"O'Brien")
            self.assertEqual(result['local_server']['port'],9090)
            self.assertIs(result['gateway']['enabled'],False)
    def test_probe_binding_mismatch_is_rejected_without_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);value=probe.make_config(settings()['probe']);value['register']['project']='other-project'
            common.atomic_json(root/'probe.yaml',value)
            adapter=probe.Probe(SimpleNamespace(settings=settings()),config_root=root)
            before=(root/'probe.yaml').read_bytes()
            with self.assertRaisesRegex(RuntimeError,'project'):adapter.binding()
            self.assertEqual(before,(root/'probe.yaml').read_bytes())
    def test_probe_gate_needs_live_mqtt_even_when_cache_and_http_are_healthy(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);common.atomic_json(root/'registration.yaml',{'device_id':'fixture'})
            def run(args,**kw):
                text='active' if args[1]=='is-active' else 'f'*32 if args[0]=='systemctl' else 'MQTT connected to broker:1883\nMQTT connection lost, cause=timeout'
                return SimpleNamespace(returncode=0,stdout=text)
            adapter=probe.Probe(SimpleNamespace(settings=settings(),run=run),config_root=root)
            with mock.patch('probe.urllib.request.build_opener') as opener:
                self.assertFalse(adapter.ready());opener.assert_not_called()
    def test_workflow_digest_and_minio_pin_are_enforced(self):
        lock=common.read_json(BUNDLE/'catalog/workflow.json')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);common.atomic_json(root/'catalog/workflow.json',lock)
            catalog={'workflow_sha256':common.sha256(root/'catalog/workflow.json')}
            self.assertEqual(workflow.load_workflow(root,catalog)['order'],['probe','components','models'])
            lock['images'][0]['config_id']='sha256:'+'0'*64
            common.atomic_json(root/'catalog/workflow.json',lock)
            with self.assertRaisesRegex(RuntimeError,'发行签名'):workflow.load_workflow(root,catalog)
            row=next(x for x in lock['images'] if x['id']=='minio');row['manifest_digest']='sha256:'+'0'*64
            row['pinned']=row['image'].rsplit(':',1)[0]+'@'+row['manifest_digest']
            common.atomic_json(root/'catalog/workflow.json',lock);catalog['workflow_sha256']=common.sha256(root/'catalog/workflow.json')
            with self.assertRaisesRegex(RuntimeError,'MinIO'):workflow.load_workflow(root,catalog)
    def test_existing_database_without_container_is_not_initialized(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);data=root/'data/mysql';data.mkdir(parents=True);(data/'ibdata1').write_bytes(b'preserve')
            owner=SimpleNamespace(state={})
            lock=common.read_json(BUNDLE/'catalog/workflow.json')
            adapter=components.Components(SimpleNamespace(settings=settings(),lock=lock,owner=owner),data_root=root/'data')
            with mock.patch.object(adapter,'inspect',return_value=None),mock.patch('components.check_ports'):
                with self.assertRaisesRegex(RuntimeError,'已有 mysql 数据'):adapter.preflight()
            self.assertEqual((data/'ibdata1').read_bytes(),b'preserve')
    def test_inactive_nginx_cannot_pass_when_other_services_are_active(self):
        def run(args,**kwargs):
            if args[-1]=='nginx':raise RuntimeError('nginx inactive')
            return SimpleNamespace(returncode=0,stdout='active')
        lock=common.read_json(BUNDLE/'catalog/workflow.json')
        adapter=components.Components(SimpleNamespace(settings=settings(),lock=lock,run=run))
        with mock.patch.object(adapter,'etcd_healthy',return_value=True),mock.patch.object(adapter,'healthy',return_value=True):
            with self.assertRaisesRegex(RuntimeError,'nginx inactive'):adapter.acceptance()
    def test_subprocess_failure_does_not_publish_secret_stderr(self):
        obj=object.__new__(workflow.Workflow)
        obj.settings=settings()
        result=SimpleNamespace(returncode=1,stdout='',stderr='fixture-registration-secret')
        with mock.patch('workflow.subprocess.run',return_value=result),self.assertRaises(RuntimeError) as error:
            obj.run(['docker','inspect','fixture'])
        self.assertNotIn('fixture-registration-secret',str(error.exception))
    def test_sanitized_probe_contains_no_upstream_registration_values(self):
        import tarfile
        lock=common.read_json(BUNDLE/'catalog/workflow.json')
        archive=BUNDLE.parent/'support-files/files'/lock['probe']['archive']['path']
        with tarfile.open(archive) as tar,tempfile.TemporaryDirectory() as folder:
            text=tar.extractfile('data-probe-offline/config/probe.yaml').read()
            path=Path(folder)/'probe.yaml';path.write_bytes(text);value=probe.read_probe_config(path)
        self.assertEqual(value['register']['bindActivateToken'],'')
        self.assertTrue(all(value['register'][key]=='' for key in workflow.REGISTER_FIELDS))
        self.assertFalse(value['gateway']['enabled'])


class StageIntegrationTests(unittest.TestCase):
    def fixture(self,stack):
        helper=worker_fixture.ModularWorkerTests()
        b,s,h,m=helper.fixture(stack);obj=engine.Installer(b,s,h);obj.state['initial_guard_passed']=True
        helper.simulate_commands(obj,m)
        return obj
    def test_failed_probe_prevents_components_and_model_resources(self):
        with ExitStack() as stack:
            obj=self.fixture(stack)
            obj.workflow=SimpleNamespace(probe=mock.Mock(side_effect=RuntimeError('MQTT offline')),components=mock.Mock(),finish=mock.Mock())
            with mock.patch.object(obj,'acquire') as acquire:
                with self.assertRaisesRegex(RuntimeError,'MQTT'):obj.run()
            acquire.assert_not_called();obj.workflow.components.assert_not_called()
            self.assertEqual(obj.state['completed'],[])
    def test_failed_component_can_resume_but_probe_is_rechecked(self):
        with ExitStack() as stack:
            obj=self.fixture(stack);order=[]
            def component():
                order.append('components')
                if order.count('components')==1:raise RuntimeError('MinIO unavailable')
            obj.workflow=SimpleNamespace(probe=lambda:order.append('probe'),components=component,finish=lambda:order.append('integration'))
            acquire=obj.acquire
            def wrapped_acquire():order.append('models');acquire()
            obj.acquire=wrapped_acquire
            with self.assertRaisesRegex(RuntimeError,'MinIO'):obj.run()
            self.assertEqual(order,['probe','components'])
            obj.run()
            self.assertEqual(order,['probe','components','probe','components','models','integration'])
            self.assertEqual(obj.state['status'],'installed')
            public=common.read_json(obj.public)
            self.assertNotIn('fixture-password-123',json.dumps(public))
    def test_integration_failure_cannot_mark_installed(self):
        with ExitStack() as stack:
            obj=self.fixture(stack)
            obj.workflow=SimpleNamespace(probe=lambda:None,components=lambda:None,finish=mock.Mock(side_effect=RuntimeError('MQTT lost')))
            with self.assertRaisesRegex(RuntimeError,'MQTT'):obj.run()
            self.assertEqual(obj.state['status'],'failed')
            self.assertNotIn('integration',obj.state['completed'])


if __name__=='__main__':unittest.main(verbosity=2)
