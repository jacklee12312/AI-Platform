"""No hardware or Internet required: real files, archives, API and transactions."""
import copy
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.model_store import Catalog, ModelStore, atomic_json, file_lock, under
from agent.model_install import Installer, InstallFailure
from agent.model_runtime import ModelRuntime
from agent.model_api import Uploads, Upload, saved_job


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body); self.status = status; self.headers = headers or {}


class Runtime:
    def __init__(self):
        self.calls = []; self.failed = False; self.running = False; self.errors = []
    def inspect(self, model):
        return {'State': {'Running': True}} if self.running else None
    def preflight(self, models, activate):
        return (self.errors if activate else []), []
    def image_ready(self, model):
        return True
    def stop(self, model):
        self.calls.append(('stop', model['id'])); self.running = False
    def start(self, model, progress):
        self.calls.append(('start', model['id'])); self.running = True
    def probe(self, model):
        self.calls.append(('probe', model['id']))
        if self.failed:
            raise RuntimeError('simulated inference failure')
        return {'passed': True}


class ModelInstallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.home = self.root / 'home'; self.home.mkdir()
        self.body = b'verified model weights'
        self.sha = hashlib.sha256(self.body).hexdigest()
        source = Catalog()
        doc = {'schema_version': 1, 'models': [copy.deepcopy(source.models['mobilenet_v2'])]}
        row = doc['models'][0]
        row['files'] = [{'url': 'https://modelscope.cn/fixed', 'target': 'cv_deploy/models/mobilenet_v2/model.cix', 'size': len(self.body), 'sha256': self.sha}]
        path = self.root / 'catalog.json'; atomic_json(path, doc)
        self.catalog = Catalog(path)
        self.item = self.catalog.models['mobilenet_v2']['files'][0]
        self.store = ModelStore(self.root / 'store', self.catalog, opener=lambda *a, **k: Response(self.body))
        self.runtime = Runtime(); self.inst = Installer(self.store, self.home, self.runtime)
        self.inst.save({'schema_version': 1, 'revision': 0, 'models': {}})

    def ready(self):
        self.store.fetch(['mobilenet_v2'])

    def test_catalog_covers_exactly_enabled_baseline(self):
        baseline = json.loads((Path(__file__).resolve().parents[1] / 'agent/runtime_baseline.json').read_text())
        actual = Catalog()
        self.assertEqual(set(actual.models), {k for k, v in baseline['models'].items() if v['gateway_enabled']})
        self.assertEqual(len(actual.models), 23)
        self.assertEqual(len(actual.files), 62)
        for model in actual.models.values():
            spec = baseline['models'][model['id']]
            if 'compose_service' in spec:
                self.assertEqual(model['service'], spec['compose_service'])
            self.assertTrue((Path(__file__).resolve().parents[2] / model['directory'] / model['compose']).is_file())

    def test_download_resumes_matching_range(self):
        self.store.cached(self.item).with_suffix('.part').write_bytes(self.body[:5])
        def opener(request, **kwargs):
            self.assertEqual(request.get_header('Range'), 'bytes=5-')
            return Response(self.body[5:], 206, {'Content-Range': f'bytes 5-{len(self.body)-1}/{len(self.body)}'})
        self.store.opener = opener; self.ready()
        self.assertEqual(self.store.cached(self.item).read_bytes(), self.body)

    def test_server_ignoring_range_restarts_cleanly(self):
        self.store.cached(self.item).with_suffix('.part').write_bytes(self.body[:4])
        self.ready(); self.assertTrue(self.store.ready(self.item))

    def test_wrong_range_is_rejected(self):
        self.store.opener = lambda *a, **k: Response(self.body, 206, {'Content-Range': 'bytes 1-22/23'})
        with self.assertRaises(ValueError): self.ready()
        self.assertFalse(self.store.ready(self.item))

    def test_truncated_download_retained_but_not_ready(self):
        self.store.opener = lambda *a, **k: Response(self.body[:5])
        with self.assertRaises(ValueError): self.ready()
        self.assertEqual(self.store.cached(self.item).with_suffix('.part').stat().st_size, 5)
        self.assertFalse(self.store.ready(self.item))

    def test_bad_hash_is_not_installed_and_retry_repairs_cache(self):
        self.store.opener = lambda *a, **k: Response(b'X' * len(self.body))
        with self.assertRaises(ValueError): self.ready()
        self.assertFalse(self.store.cached(self.item).with_suffix('.part').exists())
        self.store.cached(self.item).write_bytes(b'bad cache')
        self.store.opener = lambda *a, **k: Response(self.body)
        self.ready(); self.assertTrue(self.store.ready(self.item))

    def test_catalog_and_path_allowlist(self):
        for value in ['../x', '/x', 'C:/x', 'a\\b', 'a/../b']:
            with self.assertRaises(ValueError): under(self.root, value)
        for ids in [[], ['other'], ['mobilenet_v2', 'mobilenet_v2']]:
            with self.assertRaises(ValueError): self.catalog.select(ids)

    def test_export_import_roundtrip_and_no_overwrite(self):
        self.ready(); package = self.root / 'models.tar'
        self.store.export_bundle(['mobilenet_v2'], package)
        with self.assertRaises(ValueError): self.store.export_bundle(['mobilenet_v2'], package)
        dest = ModelStore(self.root / 'other', self.catalog)
        self.assertEqual(dest.import_bundle(package)['models'], ['mobilenet_v2'])
        self.assertEqual(dest.cached(self.item).read_bytes(), self.body)

    def archive(self, name, content, kind=tarfile.REGTYPE):
        path = self.root / 'bad.tar'
        with tarfile.open(path, 'w') as archive:
            manifest = json.dumps({'schema_version': 1, 'models': ['mobilenet_v2']}).encode()
            member = tarfile.TarInfo('modeldeck-bundle.json'); member.size = len(manifest)
            archive.addfile(member, io.BytesIO(manifest))
            member = tarfile.TarInfo(name); member.size = len(content); member.type = kind; member.linkname = '../../outside'
            archive.addfile(member, io.BytesIO(content))
        return path

    def test_offline_rejects_unknown_paths_links_and_corruption(self):
        for name, content, kind in [('../outside', b'bad', tarfile.REGTYPE), ('files/' + self.sha, self.body, tarfile.SYMTYPE), ('files/' + self.sha, b'X' * len(self.body), tarfile.REGTYPE)]:
            with self.assertRaises(ValueError): self.store.import_bundle(self.archive(name, content, kind))
        self.assertFalse((self.root / 'outside').exists())
        self.assertFalse(self.store.ready(self.item))

    def test_install_without_activation_never_starts(self):
        result = self.inst.install(['mobilenet_v2'], False, lambda _: None)
        self.assertFalse(result['activated'])
        self.assertEqual(under(self.home, self.item['target']).read_bytes(), self.body)
        record = self.inst.state()['models']['mobilenet_v2']
        self.assertTrue(record['installed']); self.assertFalse(record['enabled'])
        self.assertFalse(any(c[0] == 'start' for c in self.runtime.calls))

    def test_activation_only_opens_admission_after_probe(self):
        self.inst.install(['mobilenet_v2'], False, lambda _: None)
        original = self.runtime.probe
        def probe(model):
            self.assertFalse(self.inst.state()['models']['mobilenet_v2']['enabled'])
            return original(model)
        self.runtime.probe = probe
        self.inst.manage(['mobilenet_v2'], 'activate', lambda _: None)
        self.assertTrue(self.inst.state()['models']['mobilenet_v2']['enabled'])
        self.inst.manage(['mobilenet_v2'], 'disable', lambda _: None)
        self.assertFalse(self.runtime.running)

    def test_failed_probe_restores_old_files_and_leaves_disabled(self):
        target = under(self.home, self.item['target']); target.parent.mkdir(parents=True); target.write_bytes(b'old')
        self.inst.save({'schema_version': 1, 'models': {'mobilenet_v2': {'installed': True, 'enabled': True}}})
        self.runtime.failed = True
        with self.assertRaises(InstallFailure): self.inst.install(['mobilenet_v2'], True, lambda _: None)
        self.assertEqual(target.read_bytes(), b'old')
        self.assertFalse(self.runtime.running)
        self.assertEqual(self.inst.state()['models']['mobilenet_v2']['status'], 'rolled_back')
        self.assertFalse(self.inst.state()['models']['mobilenet_v2']['enabled'])

    def test_interrupted_transaction_cannot_be_overwritten_after_disable(self):
        self.inst.save({'schema_version': 1, 'models': {'mobilenet_v2': {'installed': False, 'enabled': False, 'status': 'recovery_required'}}})
        self.inst.manage(['mobilenet_v2'], 'disable', lambda _: None)
        self.assertFalse(self.inst.plan(['mobilenet_v2'], False)['valid'])

    def test_rollback_new_install_removes_only_its_known_targets(self):
        self.inst.install(['mobilenet_v2'], False, lambda _: None)
        untouched = self.home / 'keep.txt'; untouched.write_text('keep')
        self.inst.manage(['mobilenet_v2'], 'rollback', lambda _: None)
        self.assertFalse(under(self.home, self.item['target']).exists())
        self.assertEqual(untouched.read_text(), 'keep')
        self.assertFalse(self.inst.state()['models']['mobilenet_v2']['enabled'])

    def test_activate_rejects_tampered_installed_weights(self):
        self.inst.install(['mobilenet_v2'], False, lambda _: None)
        under(self.home, self.item['target']).write_bytes(b'corrupted')
        with self.assertRaises(ValueError): self.inst.manage(['mobilenet_v2'], 'activate', lambda _: None)
        self.assertFalse(any(c[0] == 'start' for c in self.runtime.calls))

    def test_preflight_mode_and_running_service_do_not_mutate(self):
        self.runtime.errors = ['wrong mode']
        self.assertFalse(self.inst.plan(['mobilenet_v2'], True)['valid'])
        self.runtime.running = True
        self.assertFalse(self.inst.plan(['mobilenet_v2'], False)['valid'])
        self.assertEqual(self.runtime.calls, [])

    def test_shared_cv_peers_are_probed_and_last_disable_stops_container(self):
        peer = copy.deepcopy(self.catalog.models['mobilenet_v2']); peer['id'] = 'peer'
        self.catalog.models['peer'] = peer
        self.inst.install(['mobilenet_v2'], True, lambda _: None)
        self.inst.install(['peer'], True, lambda _: None)
        self.assertIn(('probe', 'mobilenet_v2'), self.runtime.calls[-2:])
        self.assertIn(('probe', 'peer'), self.runtime.calls[-2:])
        self.inst.manage(['peer'], 'disable', lambda _: None)
        self.assertTrue(self.runtime.running)
        self.inst.manage(['mobilenet_v2'], 'disable', lambda _: None)
        self.assertFalse(self.runtime.running)

    def test_managed_recovery_selects_only_enabled_existing_models(self):
        from agent.device_recovery import Recovery
        engine = Recovery(self.inst.deploy)
        self.inst.save({'schema_version': 1, 'models': {
            'bge-m3': {'installed': True, 'enabled': True},
            'copaw-flash-9b': {'installed': True, 'enabled': False}}})
        self.assertEqual(engine.selected_names('multi-model'), ['houmo-bge-m3'])
        with self.assertRaises(ValueError): engine.selected_names('qwen38')

    def test_stop_failure_keeps_journal_and_never_overwrites_live_files(self):
        self.ready()
        target = under(self.home, self.item['target']); target.parent.mkdir(parents=True); target.write_bytes(b'old')
        with patch.object(self.runtime, 'stop', side_effect=RuntimeError('cannot stop')):
            with self.assertRaises(InstallFailure): self.inst.install(['mobilenet_v2'], True, lambda _: None)
        self.assertEqual(target.read_bytes(), b'old')
        self.assertEqual(self.inst.state()['models']['mobilenet_v2']['status'], 'recovery_required')
        self.inst.manage(['mobilenet_v2'], 'rollback', lambda _: None)
        self.assertEqual(target.read_bytes(), b'old')

    def test_restart_interrupted_file_copy_can_be_rolled_back(self):
        self.ready()
        target = under(self.home, self.item['target']); target.parent.mkdir(parents=True); target.write_bytes(b'old')
        with patch.object(self.runtime, 'start', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.inst.install(['mobilenet_v2'], True, lambda _: None)
        self.assertEqual(target.read_bytes(), self.body)
        self.assertFalse(self.inst.state()['models']['mobilenet_v2']['enabled'])
        restarted = Installer(self.store, self.home, self.runtime)
        self.assertFalse(restarted.plan(['mobilenet_v2'], False)['valid'])
        restarted.manage(['mobilenet_v2'], 'rollback', lambda _: None)
        self.assertEqual(target.read_bytes(), b'old')

    def test_runtime_import_rejects_unknown_tag_before_docker(self):
        path = self.root / 'runtime.tar'
        with tarfile.open(path, 'w') as archive:
            body = json.dumps([{'RepoTags': ['unknown:latest'], 'Config': 'config.json'}]).encode()
            member = tarfile.TarInfo('manifest.json'); member.size = len(body); archive.addfile(member, io.BytesIO(body))
        runtime = ModelRuntime(self.home, self.catalog)
        with patch.object(runtime, 'run') as command, self.assertRaises(ValueError): runtime.import_runtime(path)
        command.assert_not_called()

    def test_upload_restart_resume_size_limits_and_deletion(self):
        uploads = Uploads(self.store.root)
        row = uploads.create(Upload(name='models.tar', size=6, kind='models'))
        uploads.append(row['id'], 0, b'abc')
        restarted = Uploads(self.store.root)
        self.assertEqual(restarted.get(row['id'])['offset'], 3)
        with self.assertRaises(ValueError): restarted.append(row['id'], 0, b'abc')
        with self.assertRaises(ValueError): restarted.append(row['id'], 3, b'abcd')
        restarted.append(row['id'], 3, b'def')
        with file_lock(uploads.root / (row['id'] + '.lock')):
            with self.assertRaises(RuntimeError): uploads.remove(row['id'])
        uploads.remove(row['id'])
        with self.assertRaises(FileNotFoundError): uploads.get(row['id'])

    def test_job_results_survive_restart_and_running_becomes_unknown(self):
        with patch.dict(os.environ, {'MODELDECK_STORE_ROOT': str(self.store.root)}):
            for status in ['running', 'succeeded', 'failed']:
                atomic_json(self.store.root / 'jobs' / ('a' * 32 + '.json'), {'id': 'a' * 32, 'status': status})
                self.assertEqual(saved_job('a' * 32)['status'], 'unknown' if status == 'running' else status)
            self.assertIsNone(saved_job('../outside'))

    def test_agent_routes_enforce_token_allowlist_and_return_job(self):
        from fastapi.testclient import TestClient
        from agent import main
        with patch.dict(os.environ, {'MODELDECK_STORE_ROOT': str(self.store.root), 'MODELDECK_HOME': str(self.home)}), patch.object(main, 'AGENT_TOKEN', 'test'), TestClient(main.app) as client:
            self.assertEqual(client.get('/v1/model-install').status_code, 401)
            headers = {'X-Agent-Token': 'test'}
            self.assertEqual(client.post('/v1/model-install/actions/arbitrary', headers=headers, json={'ids': ['mobilenet_v2']}).status_code, 422)
            self.assertEqual(client.post('/v1/model-install/actions/install', headers=headers, json={'ids': ['unknown']}).status_code, 422)
            with patch.object(ModelStore, 'fetch', return_value={'status': 'downloaded'}):
                response = client.post('/v1/model-install/actions/download', headers=headers, json={'ids': ['mobilenet_v2']})
                self.assertEqual(response.status_code, 200)
                job_id = response.json()['id']
                for thread in threading.enumerate():
                    if thread.name == 'admin-job-' + job_id[:8]: thread.join(5)
                self.assertEqual(client.get('/v1/jobs/' + job_id, headers=headers).json()['status'], 'succeeded')
                self.assertTrue((self.store.root / 'jobs' / (job_id + '.json')).exists())

    def test_admin_model_routes_require_session_csrf_and_audit_jobs(self):
        import time
        from unittest.mock import AsyncMock
        from fastapi.testclient import TestClient
        from admin_app import main
        job = {'id': 'c' * 32, 'kind': 'model-download', 'target': 'mobilenet_v2', 'status': 'succeeded', 'created_at': '2026-09-08T00:00:00Z'}
        with patch.object(main, 'DATA_DIR', self.root / 'admin'), patch.object(main, 'DB_PATH', self.root / 'admin/modeldeck.db'), patch.object(main, '_agent', AsyncMock(return_value=job)), TestClient(main.app) as client:
            self.assertEqual(client.get('/api/model-install').status_code, 401)
            main.SESSIONS['installation-test'] = {'csrf': 'csrf-test', 'expires_at': time.time() + 3600}
            self.addCleanup(main.SESSIONS.pop, 'installation-test', None)
            client.cookies.set('modeldeck_session', 'installation-test')
            payload = {'ids': ['mobilenet_v2'], 'activate': False}
            self.assertEqual(client.post('/api/model-install/actions/download', json=payload).status_code, 403)
            response = client.post('/api/model-install/actions/download', headers={'X-CSRF-Token': 'csrf-test'}, json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['id'], job['id'])
            self.assertEqual(client.post('/api/model-install/actions/shell', headers={'X-CSRF-Token': 'csrf-test'}, json=payload).status_code, 422)

    def test_config_updates_and_restart_do_not_start_uninstalled_services(self):
        from fastapi.testclient import TestClient
        from agent import main
        with patch.object(main, 'HOUMO_DEPLOY_DIR', self.inst.deploy), patch.object(main, 'AGENT_TOKEN', 'test'):
            with patch.object(main, '_compose_guard') as guard, patch.object(main, '_compose_sensevoice') as asr:
                main._recreate_services({'guard', 'sensevoice'})
                guard.assert_not_called(); asr.assert_not_called()
            with TestClient(main.app) as client:
                result = client.post('/v1/restart/sensevoice', headers={'X-Agent-Token': 'test'})
                self.assertEqual(result.status_code, 409)


if __name__ == '__main__': unittest.main()
