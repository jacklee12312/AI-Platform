"""Stage ordering, recovery, credential handling and existing-data boundaries."""
import copy
import hashlib
import itertools
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
            'components':{'selected':list(workflow.COMPONENTS),**{key:'fixture-$-password-123' for key in workflow.PASSWORD_FIELDS},'minio_user':'fixture-minio'}}


class WorkflowTests(unittest.TestCase):
    def test_automatic_existing_probe_requires_no_registration_fields_or_token(self):
        value={'probe':{'mode':'auto','bindActivateToken':'unused-client-secret'},'components':{'selected':[]}}
        self.assertEqual(workflow.validate_settings(value,probe_installed=True),
                         {'probe':{'mode':'auto','reuse_existing':True},'components':{'selected':[]}})
    def test_automatic_missing_probe_cannot_be_skipped_with_a_reuse_flag(self):
        value=settings();value['probe'].update(mode='auto',reuse_existing=True,bindActivateToken='')
        with self.assertRaisesRegex(ValueError,'接入令牌'):workflow.validate_settings(value,probe_installed=False)
        value['probe']['bindActivateToken']='fixture-registration-secret'
        self.assertFalse(workflow.validate_settings(value,probe_installed=False)['probe']['reuse_existing'])
    def test_every_component_subset_resolves_dependencies_and_limits_credentials(self):
        lock=common.read_json(BUNDLE/'catalog/workflow.json')
        names=list(workflow.COMPONENTS)
        for flags in itertools.product((False,True),repeat=len(names)):
            selected=[name for name,flag in zip(names,flags) if flag]
            with self.subTest(selected=selected):
                value=settings();value['components']['selected']=selected
                normalized=workflow.validate_settings(value)['components']
                expected=set(selected)|({'minio','etcd'} if 'milvus' in selected else set())
                self.assertEqual(set(workflow.resolved_components(normalized)),expected)
                self.assertEqual(normalized['selected'],selected)
                expected_fields={'selected'}|{name+'_password' for name in expected if name+'_password' in workflow.PASSWORD_FIELDS}
                if 'minio' in expected:expected_fields.add('minio_user')
                self.assertEqual(set(normalized),expected_fields)
                spec=components.compose_spec(lock['images'],normalized)['services']
                self.assertEqual(set(spec),expected&set(components.PORTS))
    def test_empty_selection_requires_no_component_secrets(self):
        value=settings();value['components']={'selected':[],'mysql_password':'ignored-short-value'}
        self.assertEqual(workflow.validate_settings(value)['components'],{'selected':[]})
    def test_unknown_duplicate_or_malformed_selection_is_rejected(self):
        for selected in ['mysql',None,['unknown'],['mysql','mysql'],[{}],[1]]:
            value=settings();value['components']['selected']=selected
            with self.subTest(selected=selected),self.assertRaises(ValueError):workflow.validate_settings(value)
    def test_legacy_configuration_keeps_its_original_all_component_scope(self):
        value=settings();value['components'].pop('selected')
        self.assertEqual(workflow.validate_settings(value)['components']['selected'],list(workflow.COMPONENTS))
    def test_milvus_requires_minio_credentials_but_no_other_database_passwords(self):
        value=settings();value['components']={'selected':['milvus'],'minio_user':'fixture-minio','minio_password':'fixture-password-123'}
        self.assertEqual(set(workflow.resolved_components(workflow.validate_settings(value)['components'])),{'etcd','minio','milvus'})
        value['components']['minio_password']='short'
        with self.assertRaises(ValueError):workflow.validate_settings(value)
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
    def test_mqtt_journal_uses_monotonic_time_for_either_display_order(self):
        entries=[{'__MONOTONIC_TIMESTAMP':'10','MESSAGE':'MQTT connect failed, rc=-1'},
                 {'__MONOTONIC_TIMESTAMP':'20','MESSAGE':'MQTT connected to broker:1883'}]
        for rows in (entries,list(reversed(entries))):
            self.assertTrue(workflow.mqtt_journal_connected('\n'.join(map(json.dumps,rows))))
        entries.append({'__MONOTONIC_TIMESTAMP':'30','MESSAGE':'MQTT connection lost'})
        self.assertFalse(workflow.mqtt_journal_connected('\n'.join(map(json.dumps,reversed(entries)))))
        self.assertFalse(workflow.mqtt_journal_connected('not journal JSON'))
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
    def test_new_probe_gate_requires_registration_and_latest_mqtt_without_http(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);common.atomic_json(root/'registration.yaml',{'device_id':'fixture'})
            def run(args,**kw):
                text='active' if args[1]=='is-active' else 'f'*32 if args[0]=='systemctl' else '\n'.join(map(json.dumps,events))
                return SimpleNamespace(returncode=0,stdout=text)
            adapter=probe.Probe(SimpleNamespace(settings=settings(),run=run),config_root=root)
            events=[{'__MONOTONIC_TIMESTAMP':'20','MESSAGE':'MQTT connected to broker:1883'},
                    {'__MONOTONIC_TIMESTAMP':'10','MESSAGE':'MQTT connect failed'}]
            with mock.patch('urllib.request.build_opener',side_effect=AssertionError('probe has no HTTP health endpoint')):
                self.assertTrue(adapter.ready())
                events.append({'__MONOTONIC_TIMESTAMP':'30','MESSAGE':'MQTT connection lost'})
                self.assertFalse(adapter.ready())
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


class ComponentSelectionExecutionTests(unittest.TestCase):
    def adapter(self,root,selected):
        value=settings();value['components']['selected']=selected
        value=workflow.validate_settings(value)
        owner=SimpleNamespace(state={},release={'installer_version':'1.1.1'},home=root/'home',
                              event=mock.Mock(),update=mock.Mock(),pause_point=mock.Mock())
        owner.home.mkdir()
        flow=SimpleNamespace(settings=value,lock=common.read_json(BUNDLE/'catalog/workflow.json'),owner=owner,
                             run=mock.Mock(return_value=SimpleNamespace(returncode=0,stdout='1',stderr='')),
                             wait=lambda check,*args,**kw: self.assertTrue(check()),host_packages=mock.Mock())
        return components.Components(flow,data_root=root/'data',config_root=root/'config')
    def test_empty_scope_skips_all_component_actions(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);adapter=self.adapter(root,[])
            with mock.patch.object(adapter,'preflight') as preflight,mock.patch.object(adapter,'host_services') as hosts,mock.patch.object(adapter,'export_connections') as export:
                adapter.install();preflight.assert_not_called();hosts.assert_not_called();export.assert_called_once()
            self.assertFalse((root/'config').exists());self.assertFalse((root/'data').exists())
            adapter.acceptance()
            self.assertEqual(adapter.flow.run.call_args_list,[mock.call(['systemctl','is-active','--quiet','docker'])])
    def test_mysql_only_ignores_foreign_redis_data_and_checks_only_mysql(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);adapter=self.adapter(root,['mysql'])
            foreign=root/'data/redis';foreign.mkdir(parents=True);(foreign/'existing.rdb').write_bytes(b'preserve')
            with mock.patch.object(adapter,'inspect',return_value=None) as inspect,mock.patch.object(adapter,'image') as image,mock.patch('components.check_ports') as ports,mock.patch.object(adapter,'healthy',return_value=True) as healthy,mock.patch.object(adapter,'export_connections'):
                adapter.install()
                self.assertEqual({c.args[0] for c in inspect.call_args_list},{'mysql'})
                ports.assert_called_once_with([3306]);image.assert_called_once_with('mysql');healthy.assert_called_once_with('mysql')
                self.assertEqual(set(common.read_json(adapter.compose)['services']),{'mysql'})
                self.assertEqual(adapter.flow.owner.state['components_passed'],['mysql'])
            self.assertEqual((foreign/'existing.rdb').read_bytes(),b'preserve')
            adapter.flow.run.reset_mock()
            with mock.patch.object(adapter,'healthy',return_value=True) as healthy,mock.patch.object(adapter,'nginx_healthy') as nginx,mock.patch.object(adapter,'etcd_healthy') as etcd:
                adapter.acceptance();healthy.assert_called_once_with('mysql');nginx.assert_not_called();etcd.assert_not_called()
            self.assertEqual(adapter.flow.run.call_args_list,[mock.call(['systemctl','is-active','--quiet','docker'])])
    def test_individual_database_health_does_not_require_other_database_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter=self.adapter(Path(folder),['mysql'])
            with mock.patch.object(adapter,'inspect',return_value={'State':{'Running':True}}),mock.patch.object(adapter,'compatible'):
                self.assertTrue(adapter.healthy('mysql'))
            self.assertEqual(adapter.flow.run.call_args.kwargs['env']['MYSQL_PWD'],'fixture-$-password-123')
    def test_host_components_are_independent_and_do_not_generate_container_compose(self):
        for name,version in [('nginx','1.22.1'),('etcd','3.4.23')]:
            with self.subTest(name=name),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);adapter=self.adapter(root,[name]);adapter.flow.run.return_value.stdout=version
                with mock.patch('components.shutil.which',return_value='/fixture/bin'),mock.patch.object(adapter,'nginx_healthy',return_value=True),mock.patch.object(adapter,'etcd_healthy',return_value=True),mock.patch.object(adapter,'export_connections'):
                    adapter.install();adapter.acceptance()
                calls=[c.args[0] for c in adapter.flow.run.call_args_list]
                other='etcd' if name=='nginx' else 'nginx'
                self.assertFalse(any(other in command for command in calls),calls)
                self.assertIn(['systemctl','enable','--now',name],calls)
                self.assertFalse((root/'config').exists());adapter.flow.host_packages.assert_not_called()
    def test_connection_export_contains_only_selected_components_and_dependencies(self):
        for selected,expected in [([],set()),(['redis'],{'redis'}),(['milvus'],{'etcd','minio','milvus'})]:
            with self.subTest(selected=selected),tempfile.TemporaryDirectory() as folder:
                adapter=self.adapter(Path(folder),selected)
                with mock.patch('components.pwd.getpwnam',return_value=SimpleNamespace(pw_uid=os.getuid(),pw_gid=os.getgid())),mock.patch('components.os.chown'):
                    adapter.export_connections()
                path=adapter.flow.owner.home/'AI-Platform-1.1-connections.json';value=common.read_json(path)
                self.assertEqual(set(value['components']),expected)
                self.assertEqual(value['requested_components'],selected)
                self.assertEqual(path.stat().st_mode&0o777,0o600)
                self.assertEqual('etcd' in value,'etcd' in expected)
                self.assertNotIn('mysql_password',json.dumps(value))
    def test_probe_dependency_preparation_does_not_fetch_unselected_host_components(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'fixture.deb';source.write_bytes(b'fixture-only')
            obj=object.__new__(workflow.Workflow);obj.directory=root;obj.settings={'components':{'selected':['redis']}}
            obj.lock=common.read_json(BUNDLE/'catalog/workflow.json')
            obj.asset=mock.Mock(return_value=source)
            obj.run=mock.Mock(return_value=SimpleNamespace(returncode=1,stdout=''))
            obj.host_packages(['libpaho-mqtt1.3'])
            fetched={call.args[0].get('package') for call in obj.asset.call_args_list}
            self.assertFalse(fetched&{'nginx','nginx-common','etcd-server','etcd-client'})
            install=obj.run.call_args.args[0]
            self.assertEqual(install[-1],'libpaho-mqtt1.3='+next(r['version'] for r in obj.lock['host_packages'] if r.get('package')=='libpaho-mqtt1.3'))


class AutomaticProbeTests(unittest.TestCase):
    def test_detection_checks_markers_without_reading_private_configuration(self):
        for marker in ['binary','config','unit']:
            with self.subTest(marker=marker),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);binary=root/'data-probe';config=root/'config';units=root/'units';units.mkdir()
                if marker=='binary':binary.write_bytes(b'fixture')
                elif marker=='config':config.mkdir(mode=0o700)
                else:(units/'data-probe.service').write_text('[Service]')
                with mock.patch('probe.Path.read_text',side_effect=AssertionError('must not read private data')),mock.patch('probe.subprocess.run') as run:
                    self.assertTrue(probe.detect_probe(binary,config,(units,)));run.assert_not_called()
    def test_inaccessible_installation_counts_as_present(self):
        with mock.patch('probe.Path.lstat',side_effect=[FileNotFoundError(),PermissionError()]),mock.patch('probe.subprocess.run') as run:
            self.assertTrue(probe.detect_probe());run.assert_not_called()
    def test_systemd_presence_and_absence_are_distinguished(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for state,expected in [('not-found',False),('loaded',True),('masked',True),('bad-setting',True)]:
                with self.subTest(state=state),mock.patch('probe.platform.system',return_value='Linux'),mock.patch('probe.shutil.which',return_value='/bin/systemctl'),mock.patch('probe.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=state)):
                    self.assertEqual(probe.detect_probe(root/'binary',root/'config',(root/'units',)),expected)
    def test_unknown_detection_state_is_not_treated_as_an_absent_probe(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with mock.patch('probe.platform.system',return_value='Linux'),mock.patch('probe.shutil.which',return_value='/bin/systemctl'),mock.patch('probe.subprocess.run',return_value=SimpleNamespace(returncode=1,stdout='')):
                with self.assertRaisesRegex(RuntimeError,'无法自动识别'):probe.detect_probe(root/'binary',root/'config',(root/'units',))
    def test_existing_probe_keeps_original_binding_without_client_registration_fields(self):
        for appeared_late in (False,True):
            with self.subTest(appeared_late=appeared_late),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);binary=root/'data-probe';binary.write_bytes(b'pinned-fixture-binary')
                config=root/'config';config.mkdir();original=probe.make_config(settings()['probe'])
                original['register']['project']='original-project'
                common.atomic_json(config/'probe.yaml',original);before=(config/'probe.yaml').read_bytes()
                client=settings()['probe'] if appeared_late else {}
                client=dict(client,mode='auto',reuse_existing=not appeared_late)
                flow=SimpleNamespace(settings={'probe':client},owner=SimpleNamespace(state={},event=mock.Mock()),
                                     lock={'probe':{'binary_sha256':common.sha256(binary)}},host_packages=mock.Mock(),asset=mock.Mock())
                adapter=probe.Probe(flow,config_root=config,binary=binary,unit_root=root/'units')
                with mock.patch.object(adapter,'activate') as activate,mock.patch('probe.read_probe_config',side_effect=AssertionError('private probe configuration must not be read')):
                    adapter.install();adapter.enable_collectors();activate.assert_not_called()
                self.assertTrue(flow.owner.state['probe_skipped']);self.assertNotIn('probe_passed',flow.owner.state)
                self.assertEqual((config/'probe.yaml').read_bytes(),before)
                self.assertEqual(adapter.binding()['register']['project'],'original-project')
                self.assertEqual(probe.Probe(flow,config_root=config,binary=binary).binding()['register']['project'],'original-project')
                flow.host_packages.assert_not_called();flow.asset.assert_not_called()
    def test_absent_probe_enters_required_installation_before_models(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);value=settings();value['probe'].update(mode='auto',reuse_existing=False)
            flow=SimpleNamespace(settings=value,owner=SimpleNamespace(state={}),lock={'probe_packages':['fixture-library']},
                                 host_packages=mock.Mock(side_effect=RuntimeError('required probe install reached')))
            adapter=probe.Probe(flow,config_root=root/'config',binary=root/'binary',unit_root=root/'units')
            with mock.patch('probe.detect_probe',return_value=False),mock.patch('probe.socket.socket'),self.assertRaisesRegex(RuntimeError,'required probe install reached'):
                adapter.install()
            flow.host_packages.assert_called_once_with(['fixture-library'])
    def test_probe_removed_after_privileged_detection_cannot_be_silently_skipped(self):
        flow=SimpleNamespace(settings={'probe':{'mode':'auto','reuse_existing':True}},owner=SimpleNamespace(state={}))
        with mock.patch('probe.detect_probe',return_value=False),self.assertRaisesRegex(RuntimeError,'已缺失'):
            probe.Probe(flow).install()
    def test_installer_owned_partial_probe_is_resumed_instead_of_skipped(self):
        flow=SimpleNamespace(settings={'probe':{'mode':'auto','reuse_existing':False}},owner=SimpleNamespace(state={'probe_owned':True}))
        with mock.patch('probe.detect_probe',return_value=True):
            self.assertFalse(probe.Probe(flow).skip_existing())
        self.assertFalse(flow.owner.state['probe_skipped'])
    def test_finish_leaves_external_probe_untouched(self):
        obj=object.__new__(workflow.Workflow);obj.owner=SimpleNamespace(state={'probe_skipped':True},event=mock.Mock())
        with mock.patch('components.Components') as component,mock.patch('probe.Probe') as adapter:
            obj.finish();component.return_value.acceptance.assert_called_once();adapter.assert_not_called()
        self.assertTrue(obj.owner.state['workflow_passed'])
    def test_finish_preserves_replaced_probe_configuration(self):
        obj=object.__new__(workflow.Workflow);obj.owner=SimpleNamespace(state={'probe_config_preserved':True},event=mock.Mock())
        with mock.patch('components.Components') as component,mock.patch('probe.Probe') as adapter:
            obj.finish();component.return_value.acceptance.assert_called_once();adapter.assert_not_called()


class StageIntegrationTests(unittest.TestCase):
    def fixture(self,stack):
        helper=worker_fixture.ModularWorkerTests()
        b,s,h,m=helper.fixture(stack);obj=engine.Installer(b,s,h);obj.state['initial_guard_passed']=True
        helper.simulate_commands(obj,m)
        return obj
    def test_failed_probe_prevents_components_and_model_resources(self):
        with ExitStack() as stack:
            obj=self.fixture(stack)
            obj.workflow=SimpleNamespace(settings=settings(),probe=mock.Mock(side_effect=RuntimeError('MQTT offline')),components=mock.Mock(),finish=mock.Mock())
            with mock.patch.object(obj,'acquire') as acquire:
                with self.assertRaisesRegex(RuntimeError,'MQTT'):obj.run()
            acquire.assert_not_called();obj.workflow.components.assert_not_called()
            self.assertEqual(obj.state['completed'],[])
    def test_external_probe_stage_and_its_installation_notice_are_skipped(self):
        with ExitStack() as stack:
            obj=self.fixture(stack);order=[]
            def skip():obj.state['probe_skipped']=True;return True
            obj.workflow=SimpleNamespace(settings=settings(),skip_existing_probe=skip,
                probe=mock.Mock(side_effect=AssertionError('external probe touched')),
                components=lambda:order.append('components'),finish=lambda:order.append('integration'))
            obj.run();obj.workflow.probe.assert_not_called()
            self.assertEqual(order,['components','integration'])
            self.assertIn('probe',obj.state['completed']);self.assertTrue(obj.state['probe_skipped'])
            self.assertFalse(any('安装探针' in item['message'] for item in obj.state['events']))
    def test_failed_component_can_resume_but_probe_is_rechecked(self):
        with ExitStack() as stack:
            obj=self.fixture(stack);order=[]
            def component():
                order.append('components')
                if order.count('components')==1:raise RuntimeError('MinIO unavailable')
            obj.workflow=SimpleNamespace(settings=settings(),probe=lambda:order.append('probe'),components=component,finish=lambda:order.append('integration'))
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
            obj.workflow=SimpleNamespace(settings=settings(),probe=lambda:None,components=lambda:None,finish=mock.Mock(side_effect=RuntimeError('MQTT lost')))
            with self.assertRaisesRegex(RuntimeError,'MQTT'):obj.run()
            self.assertEqual(obj.state['status'],'failed')
            self.assertNotIn('integration',obj.state['completed'])


if __name__=='__main__':unittest.main(verbosity=2)
