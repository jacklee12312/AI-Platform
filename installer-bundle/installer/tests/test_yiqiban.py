import json
import io
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import tarfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from yiqiban import YiQiBan
from yiqiban_config import (GATEWAY_URL, bind_application_environment, connection_environment, model_files,
                            model_plan, resumable_schema, shared_compose, sql_statements, validate_host)
from yiqiban_verify import response_valid, verify
from yiqiban_payload import load, saved_image_info


class ConfigurationTests(unittest.TestCase):
    def test_database_containers_and_volumes_are_removed_without_touching_business_volumes(self):
        source = Path(__file__).resolve().parents[2] / 'yiqiban/files/compose.yaml'
        original = json.loads(source.read_text('utf-8'))
        composed = shared_compose(original, 'ai-platform-components_default', Path('/bulk/yiqiban/volumes'))
        self.assertEqual(len(original['services']), 12)
        self.assertEqual(len(composed['services']), 9)
        self.assertNotIn('weknora-mysql', composed['services'])
        self.assertNotIn('mysql-data', composed['volumes'])
        self.assertIn('data-files', composed['volumes'])
        self.assertEqual(composed['networks']['components'], {'external': True, 'name': 'ai-platform-components_default'})
        for name in ('weknora-backend', 'agent-go', 'agent-runner'):
            self.assertIn('components', composed['services'][name]['networks'])
            self.assertFalse({'weknora-mysql', 'weknora-redis', 'weknora-postgres'} & set(composed['services'][name]['depends_on']))
        for name in ('weknora-backend', 'agent-go', 'agent-runner', 'qwenpaw'):
            self.assertIn('host.docker.internal:host-gateway', composed['services'][name]['extra_hosts'])
        for volume in composed['volumes'].values():
            self.assertTrue(volume['driver_opts']['device'].startswith('/bulk/yiqiban/volumes/'))

    def test_partial_model_install_does_not_register_missing_embedding_or_rerank(self):
        plan = model_plan(['spark-x1-4b'])
        self.assertEqual(plan['default_chat'], 'spark-x1-4b')
        self.assertEqual(plan['embedding'], [])
        files = model_files(plan)
        self.assertEqual([r['name'] for r in files['pro/builtin_models.yaml']['builtin_models']], ['spark-x1-4b'])
        self.assertEqual(files['paw/builtin_models.yaml']['default_active']['model'], 'spark-x1-4b')
        self.assertNotIn('mimo', json.dumps(files).lower())

    def test_chatless_install_is_rejected_before_application_deployment(self):
        with self.assertRaisesRegex(ValueError, '对话模型'):model_plan(['bge-m3', 'bge-reranker-v2-m3'])

    def test_credentials_are_env_references_in_all_three_model_registries(self):
        files = model_files(model_plan(['copaw-flash-9b', 'bge-m3', 'bge-reranker-v2-m3']))
        serialized = json.dumps(files)
        self.assertIn('${PLATFORM_MODEL_API_KEY}', serialized)
        self.assertNotIn('MIMO_API_KEY', serialized)
        embedding = next(r for r in files['pro/builtin_models.yaml']['builtin_models'] if r['type'] == 'Embedding')
        self.assertEqual(embedding['parameters']['embedding_parameters']['dimension'], 1024)

    def test_paw_overlay_targets_a_vendor_builtin_instead_of_an_ignored_custom_id(self):
        paw=model_files(model_plan(['copaw-flash-9b']))['paw/builtin_models.yaml']
        self.assertEqual(paw['default_active']['provider_id'],'opencode')
        self.assertEqual(paw['providers'][0]['id'],'opencode')

    def test_shared_credentials_do_not_overwrite_private_database_names_and_whitelist(self):
        credentials = dict(postgres_user='yiqiban_pro_example', postgres_db='yiqiban_pro_example', postgres_password='hex-secret', redis_pro_db=15)
        env = connection_environment({'SSRF_WHITELIST_EXTRA': 'internal.example'}, credentials, 'gateway-key', 'redis-secret', '172.17.0.1')
        self.assertEqual(env['DB_USER'], 'yiqiban_pro_example')
        self.assertEqual(env['DB_HOST'], 'paradedb')
        self.assertEqual(env['REDIS_DB'], '15')
        self.assertEqual(env['PLATFORM_MODEL_BASE_URL'], GATEWAY_URL)
        self.assertIn('internal.example', env['SSRF_WHITELIST_EXTRA'])
        self.assertNotIn('0.0.0.0/0', env['SSRF_WHITELIST_EXTRA'])

    def test_dollar_credentials_are_literal_compose_environment_values(self):
        env = {k: 'value' for k in ['WEB_PORT', 'WORKFLOW_PORT', 'PAW_PORT', 'XINGCHEN_WEB_URL', 'QWENPAW_CONSOLE_URL', 'AGENT_SANDBOX_API_KEY']}
        env['REDIS_PASSWORD'] = "a$WORD'\"\\end"
        result, interpolation = bind_application_environment({'services': {'pro': {'env_file': ['.env'], 'environment': {'PORT': '${WEB_PORT}'}}}}, env)
        self.assertEqual(result['services']['pro']['environment']['REDIS_PASSWORD'], "a$$WORD'\"\\end")
        self.assertEqual(result['services']['pro']['environment']['PORT'], '${WEB_PORT}')
        self.assertNotIn('REDIS_PASSWORD', interpolation)
        self.assertNotIn('env_file', result['services']['pro'])

    def test_host_cannot_inject_shell_or_url_syntax(self):
        for host in ['http://box', 'box:8081', '../box', 'box;id', 'box\nother', '-box', 'a..b']:
            with self.subTest(host=host), self.assertRaises(ValueError):validate_host(host)
        self.assertEqual(validate_host('192.168.1.47'), '192.168.1.47')

    def test_original_schema_becomes_resume_safe_without_deleting_existing_data(self):
        source = Path(__file__).resolve().parents[2] / 'yiqiban/files/sql/01-agent-schema.sql'
        sql, tables = resumable_schema(source.read_text('utf-8-sig'))
        self.assertEqual(len(tables), 162)
        self.assertEqual(sql.count('CREATE TABLE IF NOT EXISTS'), 162)
        self.assertNotIn('DROP TABLE', sql)
        self.assertNotIn('DELETE FROM', sql)

    def test_sql_cannot_select_a_different_database_or_delete_existing_tables(self):
        for sql in ['USE other; CREATE TABLE `own` (id INT);', 'DROP TABLE `other`; CREATE TABLE `own` (id INT);',
                    'CREATE DATABASE other;', '/*! DROP DATABASE other */ CREATE TABLE `own` (id INT);']:
            with self.subTest(sql=sql), self.assertRaises(ValueError):resumable_schema(sql, expected=1)
        self.assertEqual(len(sql_statements("CREATE TABLE `a` (t TEXT COMMENT 'a;b'); SET FOREIGN_KEY_CHECKS=1;")), 2)


class DatabaseCommandTests(unittest.TestCase):
    def test_first_install_creates_private_state_and_resume_preserves_database_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'private').mkdir()
            (root/'private/request.json').write_text(json.dumps({'yiqiban':{'public_host':'192.168.133.47'}}))
            app=YiQiBan.__new__(YiQiBan);app.base=root/'yiqiban';app.state_path=app.base/'private.json'
            app.owner=SimpleNamespace(root=root);app.manifest={'release_id':'fixture'}
            app.flow=SimpleNamespace(settings={'components':{'selected':['mysql','redis','paradedb'],
                'mysql_password':'fixture-mysql-secret','redis_password':'fixture-redis-secret','paradedb_password':'fixture-postgres-secret'}})
            def redis(*args):
                if args[:1]==('INFO',):return '# Keyspace'
                if args[:2]==('CONFIG','GET'):return 'databases\n16'
                return '1'
            app.redis=redis
            with patch('yiqiban.Components') as components:
                app.initialize();original=dict(app.state);app.initialize()
                self.assertEqual(components.return_value.acceptance.call_count,2)
            self.assertEqual(app.state,original)
            self.assertNotEqual(app.state['redis_pro_db'],app.state['redis_workflow_db'])
            self.assertEqual(app.state_path.stat().st_mode & 0o777,0o600)
            self.assertTrue(app.state['mysql_db'].startswith('yiqiban_agent_'))

    def test_app_data_roots_keep_the_expected_service_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            app=YiQiBan.__new__(YiQiBan);app.base=Path(temp)
            path=app.base/'runner-storage'
            app.data_directory(path,(os.geteuid(),os.getegid()))
            self.assertEqual(path.stat().st_mode & 0o777,0o700)
            with self.assertRaisesRegex(RuntimeError,'归属'):app.data_directory(path,(os.geteuid()+10000,os.getegid()))

    def test_mysql_admin_password_is_not_in_argv_or_sql(self):
        app = YiQiBan.__new__(YiQiBan)
        app.flow = SimpleNamespace(settings={'components': {'mysql_password': 'secret-with-$-quotes'}})
        app.state = {};calls = []
        app.run = lambda args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(stdout='1')
        self.assertEqual(app.mysql('SELECT 1;'), '1')
        args, options = calls[0]
        self.assertNotIn('secret-with-$-quotes', ' '.join(args) + options['input'])
        self.assertEqual(options['env']['MYSQL_PWD'], 'secret-with-$-quotes')

    def test_redis_reservation_never_flushes_a_used_database(self):
        app = YiQiBan.__new__(YiQiBan);app.state = {'mysql_db': 'yiqiban_test', 'redis_pro_db': 15, 'redis_workflow_db': 14}
        calls = [];app.redis = lambda *args: calls.append(args) or '0'
        with self.assertRaisesRegex(RuntimeError, '占用'):app.reserve_redis()
        self.assertNotIn('FLUSH', str(calls))

    def test_interrupted_deploy_recovers_only_its_new_app_containers(self):
        app=YiQiBan.__new__(YiQiBan);app.app=Path('/bulk/yiqiban/app')
        app.docker_compose=lambda *a: SimpleNamespace(stdout='baseline ours foreign database')
        def inspect(args):
            name=args[-1]
            labels={'com.docker.compose.project.working_dir':str(app.app),'ai-platform.installer.task':'task',
                    'ai-platform.installer.attempt':'attempt' if name!='foreign' else 'other'}
            if name=='database':labels={}
            return SimpleNamespace(stdout=json.dumps([{'Id':name,'Config':{'Labels':labels}}]))
        app.run=inspect;created=[]
        transaction=SimpleNamespace(record={'attempt':'attempt','baseline_containers':['baseline']},state={'token':'task'},
                                    created=lambda info:created.append(info['Id']))
        app.recover_deployment(transaction)
        self.assertEqual(created,['ours'])


class ResourceTests(unittest.TestCase):
    def test_signed_payload_has_ten_arm64_application_images_and_no_databases(self):
        # Full source file hashes belong to release.py check; avoid scanning GB
        # archives and hundreds of Windows-mounted files in every Linux unit run.
        bundle=Path(__file__).resolve().parents[2]
        subprocess.run(['openssl','pkeyutl','-verify','-pubin','-inkey',str(bundle/'catalog/release-public.pem'),
                        '-rawin','-in',str(bundle/'yiqiban/manifest.json'),'-sigfile',str(bundle/'yiqiban/manifest.sig')],
                       capture_output=True,check=True)
        manifest=json.loads((bundle/'yiqiban/manifest.json').read_text('utf-8'))
        self.assertEqual(len(manifest['images']),10)
        self.assertFalse({'mysql','redis','paradedb'} & {r['service'] for r in manifest['images']})

    def test_saved_image_configuration_requires_arm64_and_derives_rootfs(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'image.tar'
            for architecture in ['arm64','amd64']:
                with tarfile.open(path,'w') as tar:
                    for name,value in [('manifest.json',[{'RepoTags':['app:v1'],'Config':'config.json'}]),
                                       ('config.json',{'architecture':architecture,'os':'linux','rootfs':{'diff_ids':['sha256:test']}})]:
                        body=json.dumps(value).encode();row=tarfile.TarInfo(name);row.size=len(body);tar.addfile(row,io.BytesIO(body))
                if architecture=='amd64':
                    with self.assertRaisesRegex(RuntimeError,'架构'):saved_image_info(path,'app:v1')
                else:
                    info=saved_image_info(path,'app:v1')
                    self.assertTrue(info['config_id'].startswith('sha256:'));self.assertEqual(info['rootfs_diff_ids'],['sha256:test'])

    def test_mutated_application_manifest_is_rejected_before_file_access(self):
        bundle=Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp);(target/'yiqiban').mkdir();(target/'catalog').mkdir()
            for name in ['release.json','yiqiban/manifest.json','yiqiban/manifest.sig','catalog/release-public.pem']:
                shutil.copyfile(bundle/name,target/name)
            with (target/'yiqiban/manifest.json').open('ab') as f:f.write(b' ')
            with self.assertRaisesRegex(RuntimeError,'签名无效'):load(target)


class InferenceAcceptanceTests(unittest.TestCase):
    def fixture(self, root):
        home = root / 'home';(home / 'houmo-docker-deployment/state').mkdir(parents=True)
        (home / 'houmo-docker-deployment/.env').write_text('HOUMO_API_KEY=not-a-real-key\n')
        mode = home / 'houmo-docker-deployment/state/active-mode';mode.write_text('copaw')
        events = [];calls = []
        owner = SimpleNamespace(home=home, pause_point=lambda: None, update=lambda *a, **k: None, event=events.append)
        app = SimpleNamespace(owner=owner, plan=model_plan(['copaw-flash-9b']), state={}, save=lambda: None, base=root)
        app.docker_compose=lambda *args:SimpleNamespace(stdout='fixture-paw')
        def command(args, **kwargs):
            calls.append([str(x) for x in args])
            if str(args[0]).endswith('modelctl'):mode.write_text(args[-1])
            return SimpleNamespace(stdout='configured\n' if args[0]=='docker' else '', returncode=0)
        app.run = command
        return app, calls

    def test_network_failure_restores_original_mode_and_records_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            app, calls = self.fixture(Path(temp))
            with patch('yiqiban_verify.container_request', side_effect=RuntimeError('network unavailable')):
                with self.assertRaisesRegex(RuntimeError, 'network unavailable'):verify(app)
            report = json.loads((Path(temp) / 'acceptance.json').read_text())
            self.assertFalse(report['passed']);self.assertEqual(report['restored_mode'], 'copaw')
            self.assertEqual(calls[-2][-1], 'copaw')

    def test_success_tests_each_container_and_wrong_key_but_does_not_claim_business_activation(self):
        with tempfile.TemporaryDirectory() as temp:
            app, calls = self.fixture(Path(temp));requests = []
            def request(_, service, path, key, body=None):
                requests.append((service, path))
                if key.startswith('invalid-'):return 401, {'error': 'unauthorized'}
                if path == '/models':return 200, {'data': [{'id': 'copaw-flash-9b'}]}
                return 200, {'choices': [{'message': {'content': 'OK'}}]}
            with patch('yiqiban_verify.container_request', side_effect=request):report = verify(app)
            self.assertTrue(report['passed']);self.assertEqual(len(report['checks']), 8)
            self.assertEqual(report['not_installed'], ['embedding', 'rerank'])
            self.assertEqual(report['application_business_api_acceptance'], 'not_run')
            self.assertIn('valid-license', report['business_activation'])
            self.assertEqual(sum(path == '/chat/completions' for _, path in requests), 3)

    def test_empty_chat_and_nonfinite_vectors_do_not_pass_as_connected(self):
        self.assertFalse(response_valid('chat', {'choices': [{'message': {'content': ''}}]}))
        self.assertFalse(response_valid('chat', {'choices': [{'message': None}]}))
        self.assertFalse(response_valid('embedding', {'data': [{'embedding': [float('nan')] * 1024}]}))
        self.assertFalse(response_valid('rerank', {'results': [{'index': 0, 'relevance_score': 'bad'}]}))
        self.assertTrue(response_valid('embedding', {'data': [{'embedding': [0.1] * 1024}]}))

    def test_pause_after_switching_model_restores_the_original_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            app,calls=self.fixture(Path(temp));app.plan=model_plan(['copaw-flash-9b','bge-m3'])
            def pause():
                if (app.owner.home/'houmo-docker-deployment/state/active-mode').read_text()=='bge-m3':
                    raise RuntimeError('fixture paused')
            app.owner.pause_point=pause
            def request(_,service,path,key,body=None):
                if key.startswith('invalid-'):return 401,{}
                if path=='/models':return 200,{'data':[{'id':'copaw-flash-9b'},{'id':'bge-m3'}]}
                return 200,{'choices':[{'message':{'content':'OK'}}]}
            with patch('yiqiban_verify.container_request',side_effect=request),self.assertRaisesRegex(RuntimeError,'fixture paused'):
                verify(app)
            report=json.loads((Path(temp)/'acceptance.json').read_text())
            self.assertFalse(report['passed']);self.assertEqual(report['restored_mode'],'copaw')
            self.assertTrue(any(c[-1]=='bge-m3' for c in calls));self.assertNotIn('verification_original_mode',app.state)

    def test_restore_failure_cannot_report_success(self):
        with tempfile.TemporaryDirectory() as temp:
            app,_=self.fixture(Path(temp))
            (app.owner.home/'houmo-docker-deployment/state/active-mode').write_text('qwen38')
            command=app.run
            def fail_restore(args,**kwargs):
                if args[-1]=='qwen38':raise RuntimeError('fixture restore failed')
                return command(args,**kwargs)
            app.run=fail_restore
            def request(_,service,path,key,body=None):
                if key.startswith('invalid-'):return 401,{}
                if path=='/models':return 200,{'data':[{'id':'copaw-flash-9b'}]}
                return 200,{'choices':[{'message':{'content':'OK'}}]}
            with patch('yiqiban_verify.container_request',side_effect=request),self.assertRaisesRegex(RuntimeError,'fixture restore failed'):
                verify(app)
            report=json.loads((Path(temp)/'acceptance.json').read_text())
            self.assertFalse(report['passed']);self.assertTrue(report['mode_restore_failed'])
            self.assertEqual(app.state['verification_original_mode'],'qwen38')

    def test_unloaded_paw_model_configuration_cannot_pass_via_gateway_curl(self):
        with tempfile.TemporaryDirectory() as temp:
            app,_=self.fixture(Path(temp))
            with patch('yiqiban_verify.verify_paw_configuration',side_effect=RuntimeError('overlay ignored')),patch('yiqiban_verify.container_request') as request:
                with self.assertRaisesRegex(RuntimeError,'overlay ignored'):verify(app)
                request.assert_not_called()
            report=json.loads((Path(temp)/'acceptance.json').read_text())
            self.assertFalse(report['passed']);self.assertEqual(report['restored_mode'],'copaw')


if __name__ == '__main__':unittest.main()
