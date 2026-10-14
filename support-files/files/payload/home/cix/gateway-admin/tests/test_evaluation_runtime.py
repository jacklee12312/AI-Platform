"""Fake Docker/HTTP only: fixed grading, bounded transport and read-only state."""
import base64
import asyncio
import copy
import hashlib
import io
import json
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.evaluation_cases import MAX_CASES, SUITES, VERSION, input_hash, make_cases
from agent.evaluation_engine import EvaluationSkip
from agent.evaluation_runtime import EvaluationRuntime, catalog_models, grade
from agent.model_store import Catalog
from agent.runtime_probes import digit_image


def chat(text):
    return {'choices': [{'message': {'content': text}}]}


def embeddings(vectors):
    return {'data': [{'index': index, 'embedding': vector} for index, vector in enumerate(vectors)]}


def wav(frames=b'\x01\x00' * 160):
    output = io.BytesIO()
    with wave.open(output, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(frames)
    return output.getvalue()


class Safety:
    def __init__(self):
        self.state = {'revision': 1, 'boot_id': 'fixed-boot', 'incident': None,
                      'maintenance': None, 'monitor_error': None}
    def snapshot(self):
        return copy.deepcopy(self.state)
    def scan(self):
        raise AssertionError('no scans')
    def verify_installation(self, *args):
        raise AssertionError('no admission writes')


class CaseTests(unittest.TestCase):
    def test_allowlist_bounds_and_determinism(self):
        ids = list(Catalog().models)
        self.assertEqual(len(ids), 23)
        self.assertEqual(VERSION, '2026-09-08.1')
        self.assertEqual(SUITES['quality'], '固定样例评测')
        for suite, count in [('smoke', 1), ('quality', 1), ('repeat', 3)]:
            rows = make_cases(ids, suite, count)
            self.assertLessEqual(len(rows), MAX_CASES)
            self.assertEqual(rows, make_cases(ids, suite, count))
            self.assertEqual(len({row['id'] for row in rows}), len(rows))
            for row in rows:
                self.assertEqual(row['input_sha256'], input_hash(row['request']))
                self.assertNotIn('base64', json.dumps(row['sample']))
                self.assertIn(row['scope'], {'quality', 'execution'})
        self.assertEqual(len(make_cases(ids, 'repeat', 3)), 69)
        for models, suite, repeats in [([], 'smoke', 1), (['unknown'], 'smoke', 1),
                                      ([ids[0], ids[0]], 'smoke', 1), (ids, 'x', 1),
                                      (ids, 'smoke', 2), (ids, 'quality', 2), (ids, 'repeat', 4),
                                      (ids, 'repeat', True), (ids, 'repeat', 1.0), (ids, 'repeat', 0)]:
            with self.subTest(models=models[:2], suite=suite, repeats=repeats), self.assertRaises(ValueError):
                make_cases(models, suite, repeats)

    def test_image_audio_hashes_and_unsupported_quality(self):
        for mid in ['qwen3-vl-8b', 'glm-ocr-0.9b']:
            row = make_cases([mid], 'quality')[0]
            self.assertIn(digit_image(), json.dumps(row['request']))
            changed = copy.deepcopy(row['request'])
            changed['json']['messages'][0]['content'][0]['image_url']['url'] += 'x'
            self.assertNotEqual(row['input_sha256'], input_hash(changed))
        asr = make_cases(['sensevoice'], 'smoke')[0]
        self.assertEqual(asr['request']['audio']['size'], 178988)
        self.assertEqual(asr['request']['audio']['sha256'], 'b77f1794fe374a0ba1ee1dc458bfaf9349496cbbfc32780c50ba3c5a7ad8e373')
        for model in Catalog().models.values():
            if model['capability'].startswith('cv_') or model['capability'] in {'audio_asr', 'audio_tts'}:
                row = make_cases([model['id']], 'quality')[0]
                self.assertEqual(row['status'], 'skipped')
                self.assertIn('unsupported', row['reason'])
                self.assertEqual(row['request'], {})

    def test_strict_text_no_digit_false_positives(self):
        row = make_cases(['copaw-flash-9b'], 'smoke')[0]
        self.assertEqual(row['parameters']['max_tokens'], 64)
        self.assertTrue(grade(row, chat(' 7\n'))['passed'])
        for value in ['not 7', '7.0', '-7', '17', '7 or seven', '七', '７', 7, True, None]:
            with self.subTest(value=value):
                self.assertFalse(grade(row, chat(value))['passed'])
        arithmetic = make_cases(['copaw-flash-9b'], 'quality')[0]
        self.assertTrue(grade(arithmetic, chat('31'))['passed'])
        self.assertFalse(grade(arithmetic, chat('3'))['passed'])

    def test_strict_json_rejects_duplicate_extra_wrong_type_and_wrappers(self):
        row = make_cases(['copaw-flash-9b'], 'quality')[1]
        self.assertTrue(grade(row, chat('{ "answer": 7 }'))['passed'])
        for value in ['{"answer":7,"answer":7}', '{"answer":7,"extra":0}', '{"answer":true}',
                      '{"answer":7.0}', '{"answer":"7"}', '[{"answer":7}]', '```json\n{"answer":7}\n```',
                      '{"answer":7} trailing', '{"answer":NaN}', '{"answer":Infinity}', '{}']:
            with self.subTest(value=value):
                self.assertFalse(grade(row, chat(value))['passed'])

    def test_embeddings_nonfinite_bool_zero_and_index_rejected(self):
        row = make_cases(['bge-m3'], 'smoke')[0]
        self.assertTrue(grade(row, embeddings([[1.0] * 1024]))['passed'])
        for value in [True, float('nan'), float('inf'), -float('inf'), '1', None, 10 ** 1000]:
            vector = [1.0] * 1024
            vector[0] = value
            with self.subTest(value=str(value)[:20]):
                self.assertFalse(grade(row, embeddings([vector]))['passed'])
        for vectors in [[[0] * 1024], [[1] * 1023], [[1] * 1025], [[1] * 1024] * 2]:
            self.assertFalse(grade(row, embeddings(vectors))['passed'])
        self.assertFalse(grade(row, {'data': [{'index': True, 'embedding': [1] * 1024}]})['passed'])

    def test_semantic_fixed_three_texts_and_finite_cosine(self):
        row = make_cases(['bge-m3'], 'quality')[0]
        self.assertEqual(len(row['sample']), 3)
        a, b = [1e300] + [0] * 1023, [0, 1e-300] + [0] * 1022
        result = grade(row, embeddings([a, a, b]))
        self.assertTrue(result['passed'])
        self.assertEqual(result['metrics']['related_cosine'], 1.0)
        self.assertNotIn(str(a), result['actual'])
        self.assertFalse(grade(row, embeddings([a, b, a]))['passed'])
        self.assertFalse(grade(row, embeddings([a, a, a]))['passed'])
        shuffled = embeddings([a, a, b])
        shuffled['data'].reverse()
        self.assertTrue(grade(row, shuffled)['passed'])

    def test_rerank_accepts_logits_but_requires_exact_indices_and_order(self):
        smoke = make_cases(['bge-reranker-v2-m3'], 'smoke')[0]
        quality = make_cases(['bge-reranker-v2-m3'], 'quality')[0]
        def rows(i=0, a=3.2, j=1, b=-6.7):
            return {'results': [{'index': i, 'relevance_score': a}, {'index': j, 'relevance_score': b}]}
        self.assertTrue(grade(quality, rows())['passed'])
        for body in [rows(i=False), rows(j=0), rows(i=0.0), rows(i=-1), rows(a=True), rows(a=float('nan')),
                     rows(b=float('inf'))]:
            self.assertFalse(grade(smoke, body)['passed'])
        self.assertFalse(grade(quality, rows(a=1, b=1))['passed'])
        self.assertFalse(grade(quality, rows(i=1, j=0))['passed'])

    def test_guard_schema_exact_boolean(self):
        benign, fraud = make_cases(['qwen3guard-stream-0.6b'], 'quality')
        def body(flag):
            return {'results': [{'flagged': flag, 'categories': {}, 'category_scores': {}}]}
        self.assertTrue(grade(benign, body(False))['passed'])
        self.assertTrue(grade(fraud, body(True))['passed'])
        for value in [0, 1, 'false', None, True]:
            self.assertFalse(grade(benign, body(value))['passed'])
        self.assertFalse(grade(fraud, body(False))['passed'])
        bad = body(False)
        bad['results'][0].update(categories={'fraud': False}, category_scores={'fraud': float('nan')})
        self.assertFalse(grade(benign, bad)['passed'])

    def test_cv_and_wav_structures(self):
        row = make_cases(['mobilenet_v2'], 'smoke')[0]
        self.assertTrue(grade(row, {'ok': True, 'model': 'mobilenet_v2', 'runtime_outputs': [{}]})['passed'])
        for output in [None, [], {}, True, 'pretend output']:
            self.assertFalse(grade(row, {'ok': True, 'model': 'mobilenet_v2', 'runtime_outputs': output})['passed'])
        tts = next(m['id'] for m in Catalog().models.values() if m['capability'] == 'audio_tts')
        row = make_cases([tts], 'smoke')[0]
        self.assertTrue(grade(row, wav())['passed'])
        for audio in [b'', b'not audio', wav(b''), wav(b'\0\0' * 160), wav()[:-2]]:
            self.assertFalse(grade(row, audio)['passed'])


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.deploy = self.home / 'houmo-docker-deployment'
        (self.deploy / 'state').mkdir(parents=True)
        self.mode = self.deploy / 'state/active-mode'
        self.mode.write_text('multi-model', encoding='utf-8')
        self.registry = self.home / 'models.json'
        self.catalog = Catalog()
        self.policy = {'models': [{'id': mid, 'enabled': True} for mid in self.catalog.models]}
        self.registry.write_text(json.dumps(self.policy), encoding='utf-8')
        self.installs = self.deploy / 'state/model-installations.json'
        self.install_state = {'schema_version': 1, 'models': {mid: {'installed': True, 'enabled': True} for mid in self.catalog.models}}
        self.installs.write_text(json.dumps(self.install_state), encoding='utf-8')
        self.safety = Safety()
        self.commands, self.requests, self.items = [], [], {}
        self.secret, self.backend_secret = 'private-management-token', 'private-backend-key'
        for name in ['ai-model-gateway', 'gateway-admin-agent', *(m['container'] for m in self.catalog.models.values())]:
            self.items[name] = {'Name': '/' + name, 'Id': name + '-id', 'Image': 'sha256:' + 'a' * 64, 'RestartCount': 0,
                                'Config': {'Image': 'fixed-runtime:1', 'Env': ['GATEWAY_MANAGEMENT_TOKEN=' + self.secret,
                                                                           'HOUMO_API_KEY=' + self.backend_secret],
                                           'Cmd': ['private command data']},
                                'State': {'Running': True, 'Restarting': False, 'StartedAt': '2026-09-08T00:00:00Z'}}
        self.inference = lambda request: httpx.Response(200, json=chat('7'))
        self.status_overrides = {}
        self.runtime = EvaluationRuntime(self.home, self.deploy, self.registry, safety=self.safety,
                                         command=self.command, transport=httpx.MockTransport(self.http))

    def command(self, args, **kwargs):
        self.assertEqual(args[1], 'inspect')
        self.assertGreaterEqual(len(args), 3)
        self.assertLessEqual(kwargs['timeout'], 30)
        self.commands.append(args)
        rows = [self.items[name] for name in args[2:] if name in self.items]
        complete = len(rows) == len(args) - 2
        return SimpleNamespace(returncode=0 if complete else 1, stdout=json.dumps(rows), stderr='' if complete else 'No such container')

    def http(self, request):
        self.assertEqual(str(request.url).split('/v1/')[0], 'http://ai-model-gateway:17700')
        self.assertEqual(request.headers['X-Gateway-Admin-Token'], self.secret)
        self.assertNotIn('Authorization', request.headers)
        self.assertLessEqual(request.extensions['timeout']['read'], 180)
        self.requests.append(request)
        if request.method == 'GET':
            return httpx.Response(200, json={'data': [{'id': mid, 'status': self.status_overrides.get(mid, 'online'),
                                                       'active_mode': self.mode.read_text()} for mid in self.catalog.models]})
        return self.inference(request)

    def prepare(self, models=None):
        return self.runtime.prepare(models or ['copaw-flash-9b'])

    def test_snapshot_redacts_secrets_and_docker_is_inspect_only(self):
        original = self.installs.read_bytes()
        with patch('agent.model_runtime.ModelRuntime.probe', side_effect=AssertionError('no probes')):
            snapshot = self.prepare()
            self.runtime.check('copaw-flash-9b', snapshot)
            result = self.runtime.execute(make_cases(['copaw-flash-9b'], 'smoke')[0])
        self.assertTrue(result['passed'])
        text = json.dumps(snapshot)
        for secret in [self.secret, self.backend_secret, 'private command data', 'Env', 'Cmd']:
            self.assertNotIn(secret, text)
        self.assertEqual(snapshot['execution_path'], 'gateway_management_no_customer_billing')
        self.assertEqual(snapshot['artifact_hash_scope'], 'declared_catalog_not_verified_weights')
        self.assertEqual(len(snapshot['grader_sha256']), 64)
        self.assertEqual(snapshot['evaluator']['image_id'], 'sha256:' + 'a' * 64)
        self.assertEqual(self.installs.read_bytes(), original)
        self.assertEqual(sum(r.method == 'POST' for r in self.requests), 1)

    def test_snapshot_batches_inspection_and_rejects_unknown_targets(self):
        snapshot = self.prepare(list(self.catalog.models))
        self.assertEqual(len(self.commands), 2)  # before and after preparation
        self.assertGreater(len(self.commands[0]), 3)
        self.assertEqual(len(snapshot['models']), 23)
        with self.assertRaises(RuntimeError):
            self.runtime.runtime.run('inspect', 'unrelated-user-container')
        with self.assertRaises(RuntimeError):
            self.runtime.runtime.run('restart', 'ai-model-gateway')
        del self.items['gateway-admin-agent']
        self.assertIsNone(self.prepare()['evaluator'])

    def test_inactive_uninstalled_unavailable_are_skips_after_prepare(self):
        for status in ['inactive', 'uninstalled', 'unavailable']:
            with self.subTest(status=status):
                self.setUp()
                if status == 'inactive':
                    self.install_state['models']['copaw-flash-9b']['enabled'] = False
                elif status == 'uninstalled':
                    self.install_state['models']['copaw-flash-9b']['installed'] = False
                else:
                    self.status_overrides['copaw-flash-9b'] = 'error'
                self.installs.write_text(json.dumps(self.install_state), encoding='utf-8')
                snapshot = self.prepare()
                with self.assertRaises(EvaluationSkip):
                    self.runtime.check('copaw-flash-9b', snapshot)
        snapshot = self.prepare(['qwen3.8-27b'])
        with self.assertRaises(EvaluationSkip):
            self.runtime.check('qwen3.8-27b', snapshot)

    def test_state_and_identity_changes_abort(self):
        for kind in ['mode', 'registry', 'install', 'gateway', 'evaluator', 'model', 'image', 'restart', 'stopped', 'safety']:
            with self.subTest(kind=kind):
                self.setUp()
                snapshot = self.prepare()
                if kind == 'mode': self.mode.write_text('qwen38')
                if kind == 'registry': self.registry.write_text(json.dumps(self.policy) + ' ')
                if kind == 'install': self.installs.write_text(json.dumps(self.install_state) + ' ')
                if kind == 'gateway': self.items['ai-model-gateway']['Id'] += '-changed'
                if kind == 'evaluator': self.items['gateway-admin-agent']['Image'] = 'sha256:new-agent-image'
                if kind == 'model': self.items['houmo-copaw']['Id'] += '-changed'
                if kind == 'image': self.items['houmo-copaw']['Image'] = 'sha256:new-image'
                if kind == 'restart': self.items['houmo-copaw']['RestartCount'] = 1
                if kind == 'stopped': self.items['houmo-copaw']['State']['Running'] = False
                if kind == 'safety': self.safety.state['revision'] += 1
                with self.assertRaises(RuntimeError):
                    self.runtime.check('copaw-flash-9b', snapshot)

    def test_npu_incident_maintenance_monitor_error_abort_without_scans(self):
        for key in ['incident', 'maintenance', 'monitor_error']:
            self.safety.state[key] = {'detail': self.secret}
            with self.assertRaises(RuntimeError) as error:
                self.prepare()
            self.assertNotIn(self.secret, str(error.exception))
            self.safety.state[key] = None

    def test_post_inference_drift_overrides_pass(self):
        self.prepare()
        def infer(request):
            self.items['houmo-copaw']['Id'] += '-changed'
            return httpx.Response(200, json=chat('7'))
        self.inference = infer
        with self.assertRaises(RuntimeError):
            self.runtime.execute(make_cases(['copaw-flash-9b'], 'smoke')[0])

    def test_missing_audio_and_wrong_hash_do_not_download_or_infer(self):
        self.prepare(['sensevoice'])
        case = make_cases(['sensevoice'], 'smoke')[0]
        with self.assertRaises(EvaluationSkip):
            self.runtime.execute(case)
        target = self.home / case['request']['audio']['target']
        target.parent.mkdir(parents=True)
        target.write_bytes(b'x' * case['request']['audio']['size'])
        with self.assertRaises(EvaluationSkip):
            self.runtime.execute(case)
        self.assertFalse(any(r.method == 'POST' for r in self.requests))
        raw = wav()
        target.write_bytes(raw)
        synthetic = copy.deepcopy(case)
        synthetic['request']['audio'].update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        body, content_type = self.runtime._payload(synthetic)
        self.assertIn(raw, body)
        self.assertIn('multipart/form-data', content_type)

    def test_cv_sends_raw_png_to_public_gateway_operation(self):
        self.prepare(['mobilenet_v2'])
        def infer(request):
            self.assertEqual(request.url.path, '/v1/cv/classify')
            self.assertEqual(request.url.params['model'], 'mobilenet_v2')
            self.assertEqual(request.headers['Content-Type'], 'image/png')
            self.assertEqual(request.content, base64.b64decode(digit_image().split(',', 1)[1]))
            return httpx.Response(200, json={'ok': True, 'model': 'mobilenet_v2', 'runtime_outputs': [{}]})
        self.inference = infer
        self.assertTrue(self.runtime.execute(make_cases(['mobilenet_v2'], 'smoke')[0])['passed'])

    def test_checker_failure_continues_and_actual_is_bounded_redacted(self):
        self.prepare()
        self.inference = lambda request: httpx.Response(200, json=chat(self.secret + ' Bearer leaked ' + self.backend_secret + '汉' * 4000))
        case = make_cases(['copaw-flash-9b'], 'smoke')[0]
        result = self.runtime.execute(case)
        self.assertFalse(result['passed'])
        self.assertLessEqual(len(result['actual']), 2000)
        for value in [self.secret, self.backend_secret, 'Bearer leaked']:
            self.assertNotIn(value, result['actual'])
        self.inference = lambda request: httpx.Response(200, json=chat('7'))
        self.assertTrue(self.runtime.execute(case)['passed'])

    def test_secret_spanning_summary_cutoff_is_redacted_before_truncation(self):
        self.prepare()
        self.inference = lambda request: httpx.Response(200, json=chat('字' * 1995 + self.secret))
        result = self.runtime.execute(make_cases(['copaw-flash-9b'], 'smoke')[0])
        self.assertNotIn(self.secret[:5], result['actual'])

    def test_catalog_does_not_replace_active_run_snapshot_or_deadline(self):
        snapshot = self.prepare()
        self.runtime.catalog_models()
        self.assertEqual(self.runtime._snapshot, snapshot)
        self.assertEqual([row['id'] for row in self.runtime._models], ['copaw-flash-9b'])
        self.assertIsNone(self.runtime._deadline)
        self.runtime.check('copaw-flash-9b', snapshot)

    def test_no_redirect_retry_or_upstream_error_leaks(self):
        for status in [302, 401, 503]:
            with self.subTest(status=status):
                self.setUp()
                self.prepare()
                self.inference = lambda request: httpx.Response(status, text=self.secret,
                                                                 headers={'Location': 'http://other.invalid'})
                with self.assertRaises((RuntimeError, EvaluationSkip)) as error:
                    self.runtime.execute(make_cases(['copaw-flash-9b'], 'smoke')[0])
                self.assertNotIn(self.secret, str(error.exception))
                self.assertEqual(sum(r.method == 'POST' for r in self.requests), 1)

    def test_transport_exception_is_static_and_not_retried(self):
        self.prepare()
        def fail(request):
            raise RuntimeError(self.secret)
        self.inference = fail
        with self.assertRaises(RuntimeError) as error:
            self.runtime.execute(make_cases(['copaw-flash-9b'], 'smoke')[0])
        self.assertNotIn(self.secret, str(error.exception))
        self.assertEqual(sum(r.method == 'POST' for r in self.requests), 1)

    def test_malformed_and_nonfinite_response_fail_without_retry(self):
        self.prepare(['bge-m3'])
        self.inference = lambda request: httpx.Response(200, content=b'{"data":[NaN]}')
        result = self.runtime.execute(make_cases(['bge-m3'], 'smoke')[0])
        self.assertFalse(result['passed'])
        self.assertEqual(sum(r.method == 'POST' for r in self.requests), 1)

    def test_response_cap_and_compression_rejected(self):
        for headers in [{'Content-Length': str(16 * 1024 * 1024 + 1)}, {'Content-Encoding': 'gzip'}]:
            self.setUp()
            self.prepare()
            self.inference = lambda request: httpx.Response(200, headers=headers, content=b'7')
            with self.assertRaises(RuntimeError):
                self.runtime.execute(make_cases(['copaw-flash-9b'], 'smoke')[0])

    def test_chunk_limit_is_enforced_without_content_length(self):
        self.prepare()
        class Oversize(httpx.SyncByteStream):
            def __iter__(self):
                yield b'x' * 64
                yield b'x' * 65
        self.inference = lambda request: httpx.Response(200, stream=Oversize())
        with patch('agent.evaluation_runtime.MAX_RESPONSE_BYTES', 128), self.assertRaises(RuntimeError):
            self.runtime._request('/v1/chat/completions', body=b'{}')
        self.assertEqual(sum(r.method == 'POST' for r in self.requests), 1)

    def test_http_client_disables_environment_proxy_and_redirects(self):
        original_client = httpx.Client
        settings = []
        def client(**kwargs):
            settings.append(kwargs)
            return original_client(**kwargs)
        with patch('agent.evaluation_runtime.httpx.Client', side_effect=client), patch.dict('os.environ', HTTP_PROXY='http://invalid.invalid:1'):
            self.prepare()
        self.assertTrue(settings)
        self.assertTrue(all(row['trust_env'] is False and row['follow_redirects'] is False for row in settings))

    def test_wall_clock_bounds_blocked_headers_and_slow_trickle(self):
        self.prepare()
        release = threading.Event()
        self.addCleanup(release.set)
        class Trickle(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(20):
                    time.sleep(.02)
                    yield b' '
        for slow in ['headers', 'trickle']:
            self.runtime._aborted = False
            def infer(request):
                if slow == 'headers':
                    release.wait(.5)
                    return httpx.Response(200, json=chat('7'))
                return httpx.Response(200, stream=Trickle())
            self.inference = infer
            start = time.monotonic()
            with self.assertRaises(RuntimeError):
                self.runtime._request('/v1/chat/completions', body=b'{}', timeout=.07)
            self.assertLess(time.monotonic() - start, .3)
            with self.assertRaises(RuntimeError):
                self.runtime._request('/v1/chat/completions', body=b'{}', timeout=.07)
            release.set()

    def test_production_async_stream_closes_on_wall_clock_timeout(self):
        self.prepare()
        self.runtime.transport = None
        closed = threading.Event()
        original_client = httpx.AsyncClient
        class Stalled(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b' '
                await asyncio.sleep(5)
                yield b' '
            async def aclose(self):
                closed.set()
        async def handle(request):
            return httpx.Response(200, stream=Stalled())
        def client(**kwargs):
            self.assertFalse(kwargs['trust_env'])
            self.assertFalse(kwargs['follow_redirects'])
            return original_client(transport=httpx.MockTransport(handle), **kwargs)
        with patch('agent.evaluation_runtime.httpx.AsyncClient', side_effect=client):
            start = time.monotonic()
            with self.assertRaises(RuntimeError):
                self.runtime._request('/v1/chat/completions', body=b'{}', timeout=.1)
            self.assertLess(time.monotonic() - start, .4)
            self.assertTrue(closed.wait(.5))

    def test_request_tampering_and_unsupported_quality_send_nothing(self):
        self.prepare()
        case = make_cases(['copaw-flash-9b'], 'smoke')[0]
        case['request']['path'] = 'http://external.invalid'
        with self.assertRaises(RuntimeError):
            self.runtime.execute(case)
        with self.assertRaises(EvaluationSkip):
            self.runtime.execute(make_cases(['sensevoice'], 'quality')[0])
        self.assertFalse(any(r.method == 'POST' for r in self.requests))

    def test_catalog_is_complete_request_local_and_infra_failure_is_static(self):
        rows = self.runtime.catalog_models()
        self.assertEqual(len(rows), 23)
        self.assertEqual(set(rows[0]), {'id', 'device', 'capability', 'status', 'available', 'reason', 'suites'})
        self.assertFalse(next(row for row in rows if row['id'] == 'qwen3.8-27b')['available'])
        self.status_overrides['copaw-flash-9b'] = 'error'
        fresh = catalog_models(self.home, self.deploy, self.registry, safety=self.safety,
                               command=self.command, transport=httpx.MockTransport(self.http))
        self.assertFalse(next(row for row in fresh if row['id'] == 'copaw-flash-9b')['available'])
        del self.items['ai-model-gateway']
        with self.assertRaises(RuntimeError):
            self.prepare()
        self.assertTrue(all(not row['available'] for row in self.runtime.catalog_models()))


if __name__ == '__main__':
    unittest.main()
