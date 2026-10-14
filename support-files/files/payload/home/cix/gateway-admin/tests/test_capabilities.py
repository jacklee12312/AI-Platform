from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from admin_app.capabilities import build_catalog, load_catalog, export_markdown
from admin_app.capability_examples import examples_for


def registry():
    original = json.loads((ROOT.parent / 'ai-model-gateway/config/models.json').read_text(encoding='utf-8'))
    return {'data': [{**m, 'status': 'online' if 'qwen38' not in m.get('modes', []) else 'inactive', 'active_mode': 'multi-model'} for m in original['models']]}


class CatalogTests(unittest.TestCase):
    def test_every_registered_model_has_specific_contract_and_sources(self):
        catalog = build_catalog(registry())
        self.assertEqual(len(catalog['models']), 24)
        for item in catalog['models']:
            with self.subTest(model=item['id']):
                self.assertTrue(item['summary']); self.assertTrue(item['streaming']); self.assertTrue(item['boundaries'])
                if item['registered']:
                    self.assertTrue(item['baseline_match']); self.assertTrue(item['examples'])
                    self.assertTrue(item['inputs']); self.assertTrue(item['outputs'])
                for reference in item['verification']['sources']: self.assertIn(reference, catalog['sources'])
        for source in catalog['sources'].values():
            self.assertEqual(hashlib.sha256((ROOT.parent / source['path']).read_bytes()).hexdigest(), source['sha256'])

    def test_health_never_promotes_partial_or_unverified_delivery(self):
        models = {m['id']: m for m in build_catalog(registry())['models']}
        self.assertEqual(models['bytetrack_s']['implementation'], 'partial')
        self.assertEqual(models['yolov8l_seg']['implementation'], 'partial')
        self.assertEqual(models['bge-reranker-v2-m3']['verification']['status'], 'issues')
        self.assertEqual(models['qwen3.8-27b']['runtime_status'], 'inactive')
        self.assertTrue(models['qwen3.8-27b']['examples'])
        self.assertEqual(models['cosyvoice3-0.5b']['examples'], [])
        self.assertFalse(models['cosyvoice3-0.5b']['registered'])

    def test_legacy_registry_can_offer_protocol_examples_without_claiming_verified_versions(self):
        live = registry()
        for item in live['data']:
            item['metadata'].pop('model_version', None); item['metadata'].pop('service_version', None)
        catalog = build_catalog(live)
        for item in catalog['models']:
            if item['registered']:
                self.assertFalse(item['baseline_match']); self.assertEqual(item['differences'], [])
                self.assertEqual(item['evidence_applicability'], 'version_unreported')
                self.assertEqual(item['unreported_versions'], ['model_version', 'service_version'])
                self.assertTrue(item['examples'])

    def test_new_model_and_changed_baseline_have_no_invented_examples(self):
        live = registry()
        live['data'][0]['metadata']['model_version'] = 'new-untested-model'
        live['data'].append({'id': 'new-model', 'status': 'online', 'capability': 'chat'})
        result = {m['id']: m for m in build_catalog(live)['models']}
        self.assertIn('model_version', result['copaw-flash-9b']['differences'])
        self.assertFalse(result['copaw-flash-9b']['examples'])
        self.assertEqual(result['new-model']['implementation'], 'unknown')
        self.assertFalse(result['new-model']['examples'])

    def test_unreachable_registry_preserves_docs_but_not_claims_of_availability(self):
        result = build_catalog(None, registry_error=True, config_error=True)
        self.assertFalse(result['registry_available'])
        for model in result['models']:
            self.assertIsNone(model['registered']); self.assertEqual(model['runtime_status'], 'unknown'); self.assertFalse(model['examples'])
        self.assertTrue(all(limit['source'] == 'source_default' for limit in result['limits']))

    def test_saved_limits_and_export_are_explicit_about_source(self):
        result = build_catalog(registry(), {'settings': {'max_body_mb': 12, 'chat_timeout_seconds': 900, 'API_KEY': 'never-export-me'}})
        limits = {l['id']: l for l in result['limits']}
        self.assertEqual(limits['max_body_mb']['value'], 12)
        self.assertEqual(limits['max_body_mb']['source'], 'saved_config')
        self.assertEqual(limits['asr_max_audio_seconds']['source'], 'source_default')
        document = export_markdown(result)
        self.assertNotIn('never-export-me', document)
        self.assertIn('不是正式交付验收证书', document)
        self.assertIn('track ID', document)
        self.assertIn('mask', document)
        self.assertIn('API_KEY', document)

    def test_streaming_contracts_distinguish_http_sse_vad_and_buffered_audio(self):
        result = {m['id']: m for m in build_catalog(registry())['models']}
        self.assertIn('VAD', ' '.join(result['sensevoice']['streaming']))
        self.assertIn('整段生成完', ' '.join(result['matcha-zh']['streaming']))
        self.assertIn('未提供公共审核 WebSocket', ' '.join(result['qwen3guard-stream-0.6b']['streaming']))
        legacy = next(e for e in result['glm-ocr-0.9b']['examples'] if e['id'] == 'legacy')
        self.assertFalse(legacy['body']['stream'])


class ExampleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.folder.joinpath('image.png').write_bytes(b'PNG-TEST-CONTENT')
        self.folder.joinpath('input.wav').write_bytes(b'RIFF-test-WAVE')
        self.old_cwd = Path.cwd(); os.chdir(self.folder)
        self.env = mock.patch.dict(os.environ, API_KEY='test-public-key', GATEWAY_URL='http://example.invalid:17700')
        self.env.start()

    def tearDown(self):
        self.env.stop(); os.chdir(self.old_cwd); self.temp.cleanup()

    def execute(self, example, body=None):
        requests = []
        def open_request(request, timeout):
            requests.append(request)
            self.assertEqual(request.headers['Authorization'], 'Bearer test-public-key')
            self.assertNotIn('X-gateway-admin-token', request.headers)
            payload = body
            if payload is None:
                payload = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n' if 'sse' in example['transport'] else b'RIFF0000WAVEaudio' if example['transport'] == 'wav' else b'{"ok":true}'
            response = io.BytesIO(payload)
            response.headers = {'Content-Type': 'text/event-stream' if 'sse' in example['transport'] else 'application/json'}
            return response
        with mock.patch('urllib.request.urlopen', side_effect=open_request), redirect_stdout(io.StringIO()):
            exec(compile(example['snippets']['python'], '<example>', 'exec'), {})
        return requests

    def test_every_python_snippet_compiles_and_rest_payloads_are_executable(self):
        for model in load_catalog()['models']:
            if model['implementation'] == 'unavailable': continue
            for example in examples_for(model['id'], model['profile']):
                with self.subTest(model=model['id'], example=example['id']):
                    ast.parse(example['snippets']['python'], feature_version=(3, 11))
                    if example['transport'] == 'websocket': continue
                    request = self.execute(example)[0]
                    self.assertEqual(request.full_url, 'http://example.invalid:17700' + example['path'])
                    if example['transport'] == 'raw_image': self.assertEqual(request.data, b'PNG-TEST-CONTENT')
                    elif example['transport'] == 'multipart':
                        self.assertIn(b'Content-Disposition: form-data; name="file"', request.data)
                        self.assertIn(b'\r\n', request.data); self.assertIn(b'RIFF-test-WAVE', request.data)
                    else:
                        payload = json.loads(request.data)
                        self.assertEqual(payload['model'], model['id'])
                        if example['transport'].startswith('image_'): self.assertIn('data:image/png;base64,', payload['messages'][0]['content'][0]['image_url']['url'])

    def test_python_sse_rejects_missing_done_and_in_stream_error(self):
        sample = next(e for e in examples_for('copaw-flash-9b', 'chat') if e['id'] == 'sse')
        for response in [b'data: {"choices":[]}\n', b'data: {"error":{"message":"blocked"}}\n\ndata: [DONE]\n']:
            with self.assertRaises(RuntimeError): self.execute(sample, response)

    def test_audio_sample_does_not_save_error_as_wav(self):
        sample = examples_for('matcha-zh', 'matcha')[0]
        with self.assertRaises(RuntimeError): self.execute(sample, b'{"error":"bad"}')
        self.assertFalse((self.folder / 'output.wav').exists())

    def test_shell_examples_use_customer_env_and_files_for_unicode_json(self):
        for profile, identity in [('chat', 'copaw-flash-9b'), ('ocr', 'glm-ocr-0.9b'), ('matcha', 'matcha-zh'), ('asr', 'sensevoice')]:
            for example in examples_for(identity, profile):
                if example['transport'] == 'websocket':
                    self.assertNotIn('api_key=', example['snippets']['python']); continue
                bash, powershell = example['snippets']['bash'], example['snippets']['powershell']
                self.assertIn('Bearer $API_KEY', bash); self.assertIn('Bearer $env:API_KEY', powershell)
                self.assertIn('curl.exe', powershell)
                self.assertNotIn('X-Gateway-Admin-Token', bash + powershell)
                if example['body']:
                    self.assertIn('@request.json', powershell); self.assertIn('UTF8Encoding', powershell)


class CapabilityAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from hash_password import encode
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, ADMIN_DATA_DIR=cls.temp.name, ADMIN_PASSWORD_HASH=encode('capability-test-password'))
        cls.env.start(); sys.modules.pop('admin_app.main', None); cls.module = importlib.import_module('admin_app.main')
        cls.context = TestClient(cls.module.app); cls.client = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None); cls.env.stop(); cls.temp.cleanup()

    def test_requires_login_and_performs_only_read_operations(self):
        self.client.cookies.clear()
        for path in ['/api/model-capabilities', '/api/model-capabilities/export.md']: self.assertEqual(self.client.get(path).status_code, 401)
        self.client.post('/api/login', json={'password': 'capability-test-password'})
        with mock.patch.object(self.module, '_gateway', new=mock.AsyncMock(return_value=registry())) as gateway, \
             mock.patch.object(self.module, '_agent', new=mock.AsyncMock(return_value={'settings': {'max_body_mb': 32}})) as agent:
            response = self.client.get('/api/model-capabilities')
            self.assertEqual(response.status_code, 200); self.assertEqual(len(response.json()['models']), 24)
            document = self.client.get('/api/model-capabilities/export.md')
            self.assertIn('attachment', document.headers['Content-Disposition']); self.assertIn('正式交付', document.text)
            self.assertTrue(all(c.args[0] == 'GET' for c in gateway.call_args_list + agent.call_args_list))

    def test_outage_and_invalid_snapshot_do_not_fabricate_online_status(self):
        self.client.post('/api/login', json={'password': 'capability-test-password'})
        with mock.patch.object(self.module, '_gateway', new=mock.AsyncMock(side_effect=RuntimeError('unavailable'))), \
             mock.patch.object(self.module, '_agent', new=mock.AsyncMock(return_value=[])):
            result = self.client.get('/api/model-capabilities')
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()['registry_available']); self.assertFalse(result.json()['config_available'])
        self.assertTrue(all(not item['examples'] for item in result.json()['models']))


if __name__ == '__main__': unittest.main()
