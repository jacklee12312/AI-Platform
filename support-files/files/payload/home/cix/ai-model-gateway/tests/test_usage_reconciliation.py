import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from app.usage import APIKeyUsageStore
from app.usage_quality import usage_details
from app.config import Registry
with mock.patch.dict(os.environ, {'MODEL_REGISTRY': str(Path(__file__).resolve().parents[1] / 'config/models.json')}):
    from app import main as gateway


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = APIKeyUsageStore(Path(self.temp.name) / 'usage.db')
        self.store.initialize()
        self.keys = [self.store.create_key(f'test-{i}') for i in range(3)]

    def record(self, usage, **kw):
        args = dict(api_key_id=self.keys[0]['id'], request_id='same-caller-id', model='chat',
                    endpoint='/v1/chat/completions', usage=usage)
        self.store.record_usage(**(args | kw))

    def test_counter_quality_matrix(self):
        cases = [(None, 'missing'), ({}, 'missing'), ({'foo': 5}, 'missing'),
                 ({'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}, 'reported'),
                 ({'prompt_tokens': 2, 'completion_tokens': 3}, 'derived'),
                 ({'input_tokens': 2}, 'partial'),
                 ({'input_tokens': -1, 'output_tokens': True, 'total_tokens': 2.5}, 'invalid'),
                 ({'input_tokens': 2, 'output_tokens': 3, 'total_tokens': 99}, 'inconsistent'),
                 ({'input_tokens': 0, 'prompt_tokens': 5, 'output_tokens': 0, 'total_tokens': 0}, 'inconsistent')]
        for value, quality in cases:
            with self.subTest(value=value):
                self.assertEqual(usage_details(value)['quality'], quality)
        self.assertEqual(usage_details({'input_tokens': 0, 'prompt_tokens': 5})['tokens']['input_tokens'], 0)

    def test_endpoint_aware_embedding_and_missing_values(self):
        self.record({}, request_id='empty')
        self.record({'prompt_tokens': 7, 'total_tokens': 7}, endpoint='/v1/embeddings')
        report = self.store.usage_report()
        self.assertIsNone(next(r for r in report['recent'] if r['request_id'] == 'empty')['total_tokens'])
        self.assertEqual(report['summary']['total_tokens'], 7)
        self.assertEqual(report['summary']['missing_total_tokens_requests'], 1)
        self.assertEqual(report['recent'][0]['output_tokens'], 0)

    def test_no_guessing_mismatch_and_partial(self):
        self.record({'input_tokens': 2, 'output_tokens': 3, 'total_tokens': 99})
        self.record({'total_tokens': 6})
        report = self.store.usage_report()
        self.assertEqual(report['summary']['total_tokens'], 105)
        self.assertEqual(report['summary']['input_tokens'], 2)
        self.assertEqual(report['summary']['anomaly_requests'], 2)
        self.assertIn('total_mismatch', report['recent'][1]['usage_issues'])
        self.assertEqual(report['recent'][1]['usage_raw']['values']['total_tokens'], 99)

    def test_idempotency_only_internal_id(self):
        for event in ['internal-a', 'internal-a', 'internal-b', None, None]:
            self.record({'input_tokens': 1, 'output_tokens': 2}, event_id=event)
        self.assertEqual(self.store.usage_report()['summary']['request_count'], 4)
        self.record({'total_tokens': 99}, api_key_id=None)
        self.assertEqual(self.store.usage_report()['summary']['request_count'], 4)

    def test_three_keys_pagination_concurrent_snapshot_and_persistence(self):
        def insert(i):
            self.record({'input_tokens': i, 'output_tokens': 1}, api_key_id=self.keys[i % 3]['id'],
                        request_id=f'r-{i}', model=f'm-{i % 2}')
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(insert, range(120)))
        report = self.store.usage_report(limit=20)
        self.assertEqual(report['summary']['request_count'], 120)
        self.assertEqual(report['summary']['total_tokens'], sum(range(120)) + 120)
        self.assertEqual(sum(r['total_tokens'] for r in report['by_key']), report['summary']['total_tokens'])
        self.assertEqual(sum(r['total_tokens'] for r in report['by_model']), report['summary']['total_tokens'])
        page = self.store.usage_report(limit=20, offset=20)
        self.assertFalse({r['id'] for r in report['recent']} & {r['id'] for r in page['recent']})
        self.assertTrue(page['pagination']['has_more'])
        self.assertEqual(self.store.usage_report(request_id='r-1', model='m-1')['summary']['request_count'], 1)
        self.store.revoke_key(self.keys[0]['id'])
        reopened = APIKeyUsageStore(self.store.path)
        reopened.initialize()
        self.assertEqual(reopened.usage_report(key_id=self.keys[0]['id'])['summary']['request_count'], 40)
        self.assertIsNone(reopened.authenticate(self.keys[0]['key']))

    def test_iso_boundary_including_old_second_precision(self):
        with mock.patch('app.usage._now', return_value='2026-09-04T00:00:00Z'):
            self.record({'total_tokens': 1})
        self.assertEqual(self.store.usage_report(start='2026-09-04T08:00:00+08:00', end='2026-09-04T00:00:00.000Z')['summary']['request_count'], 1)
        self.assertEqual(self.store.usage_report(start='2026-09-04T00:00:00.001Z')['summary']['request_count'], 0)
        for filters in [dict(start='2026-09-04'), dict(start='bad'), dict(start='2026-09-05T00:00:00Z', end='2026-09-04T00:00:00Z')]:
            with self.assertRaises(ValueError): self.store.usage_report(**filters)

    def test_old_database_is_migrated_not_rewritten(self):
        path = Path(self.temp.name) / 'old.db'
        with sqlite3.connect(path) as db:
            db.executescript('CREATE TABLE api_keys (id TEXT PRIMARY KEY,name TEXT,key_prefix TEXT,key_hash TEXT UNIQUE,created_at TEXT,last_used_at TEXT,enabled INTEGER,revoked_at TEXT);'
                'CREATE TABLE usage_events (id TEXT PRIMARY KEY,api_key_id TEXT,request_id TEXT,created_at TEXT,endpoint TEXT,model TEXT,input_tokens INTEGER,output_tokens INTEGER,total_tokens INTEGER,usage_reported INTEGER,status TEXT);'
                "INSERT INTO api_keys VALUES ('k','legacy','prefix','hash','2026-01-01',NULL,1,NULL);"
                "INSERT INTO usage_events VALUES ('e','k','r','2026-09-04T00:00:00Z','/v1/chat/completions','m',3,4,7,1,'succeeded');")
        db.close()
        store = APIKeyUsageStore(path)
        store.initialize()
        store.initialize()
        report = store.usage_report()
        self.assertEqual(report['summary']['total_tokens'], 7)
        self.assertEqual(report['summary']['legacy_requests'], 1)
        self.assertEqual(report['recent'][0]['id'], 'e')


class GatewayMeteringTests(unittest.TestCase):
    # HTTP and SSE tests use fake upstreams, never access model hardware.
    def setUp(self):
        LedgerTests.setUp(self)
        registry = Path(self.temp.name) / 'models.json'
        registry.write_text(json.dumps({'models': [{'id': 'chat', 'capability': 'chat', 'base_url': 'http://fake', 'backend': 'openai'}]}))
        for name, value in [('USAGE_STORE', self.store), ('API_KEY', ''), ('REGISTRY', Registry(registry)),
                            ('GUARD_MODE', 'off'), ('MANAGEMENT_TOKEN', 'test-management'),
                            ('LIMITER', gateway.FixedWindowLimiter(1000))]:
            patch = mock.patch.object(gateway, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.client = TestClient(gateway.app)
        self.headers = {'Authorization': 'Bearer ' + self.keys[0]['key'], 'X-Request-ID': 'repeated-request-id'}

    def test_http_normal_errors_management_exclusion(self):
        payload = {'model': 'chat', 'messages': [{'role': 'user', 'content': 'test'}]}
        with mock.patch.object(gateway, '_call_json', new=mock.AsyncMock(return_value={'choices': [], 'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}})):
            for _ in range(2): self.assertEqual(self.client.post('/v1/chat/completions', headers=self.headers, json=payload).status_code, 200)
            self.assertEqual(self.client.post('/v1/chat/completions', headers={'X-Gateway-Admin-Token': 'test-management'}, json=payload).status_code, 200)
        self.assertEqual(self.store.usage_report()['summary']['request_count'], 2)
        self.assertEqual(self.client.post('/v1/chat/completions', headers=self.headers, json={'model': 'chat', 'messages': []}).status_code, 400)
        self.assertEqual(self.store.usage_report(status='rejected')['summary']['request_count'], 1)
        self.assertIsNone(self.store.usage_report(status='rejected')['recent'][0]['input_tokens'])
        self.assertEqual(self.client.post('/v1/chat/completions', json=payload).status_code, 401)
        self.assertEqual(self.store.usage_report()['summary']['request_count'], 3)

    def test_rate_limit_record_and_usage_filter_validation(self):
        with mock.patch.object(gateway, 'LIMITER', gateway.FixedWindowLimiter(0)):
            response = self.client.post('/v1/chat/completions', headers=self.headers, json={})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(self.store.usage_report(status='rejected')['summary']['request_count'], 1)
        response = self.client.get('/internal/usage', headers={'X-Gateway-Admin-Token': 'test-management'}, params={'start': 'yesterday'})
        self.assertEqual(response.status_code, 400)

    def test_sse_usage_error_interruption_and_cancellation(self):
        class Stream:
            status_code = 200
            def __init__(self, lines): self.lines = lines
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def aiter_lines(self):
                for line in self.lines:
                    if isinstance(line, BaseException): raise line
                    yield line
        class Client:
            def __init__(self, lines): self.lines = lines
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def stream(self, *args, **kw): return Stream(self.lines)
        usage = 'data: ' + json.dumps({'choices': [], 'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}})
        async def consume(lines, event_id, guard_ws=None):
            with mock.patch.object(gateway.httpx, 'AsyncClient', return_value=Client(lines)):
                stream = gateway._guarded_chat_events(entry=gateway.REGISTRY.get('chat'), model_id='chat', upstream_payload={}, guard_ws=guard_ws,
                    request_id=event_id, api_key_id=self.keys[0]['id'], usage_event_id=event_id)
                try: return [chunk async for chunk in stream]
                except asyncio.CancelledError: return []
        for lines, expected, quality in [([usage, 'data: [DONE]'], 'succeeded', 'reported'),
                (['data: {"error":{"message":"failed"}}', 'data: [DONE]'], 'upstream_error', 'missing'),
                (['data: {"choices":[]}'], 'interrupted', 'missing'),
                (['data: malformed'], 'invalid_response', 'missing'),
                ([asyncio.CancelledError()], 'cancelled', 'missing')]:
            asyncio.run(consume(lines, expected))
            report = self.store.usage_report(request_id=expected)
            self.assertEqual(report['summary']['request_count'], 1)
            self.assertEqual(report['recent'][0]['status'], expected)
            self.assertEqual(report['recent'][0]['usage_quality'], quality)
        guard = mock.Mock()
        guard.close = mock.AsyncMock(side_effect=asyncio.CancelledError())
        asyncio.run(consume([], 'guard-close-cancelled', guard))
        self.assertEqual(self.store.usage_report(request_id='guard-close-cancelled')['summary']['request_count'], 1)


if __name__ == '__main__': unittest.main()
