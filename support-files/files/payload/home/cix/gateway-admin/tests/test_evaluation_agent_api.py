"""Real evaluation API/worker persistence with a fake bounded model transport."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from agent import evaluation_api
from agent.evaluation_cases import VERSION, make_cases
from agent.evaluation_engine import EvaluationManager


class FakeRuntime:
    def __init__(self):
        self.entered = threading.Event(); self.release = threading.Event(); self.release.set()
        self.calls = 0

    def prepare(self, models):
        return {'mode': 'multi-model', 'models': {m: {'image_id': 'sha256:test'} for m in models}}

    def check(self, model, snapshot):
        pass

    def execute(self, case):
        self.calls += 1; self.entered.set()
        assert self.release.wait(5)
        return {'passed': True, 'actual': 'fake execution, no real model'}

    def catalog_models(self):
        return [{'id': 'mobilenet_v2', 'device': 'CIX-NPU', 'capability': 'cv_classify',
                 'status': 'online', 'available': True, 'reason': None, 'suites': ['smoke', 'repeat']}]


class EvaluationAgentAPITests(unittest.TestCase):
    def setUp(self):
        from agent import main
        self.main = main
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.runtime = FakeRuntime()
        self.addCleanup(self.runtime.release.set)
        self.lock = threading.Lock()
        self.manager = EvaluationManager(root/'evaluations', root/'deploy', self.lock, self.runtime, VERSION, make_cases)
        self.enterContext(patch.object(evaluation_api, 'MANAGER', self.manager))
        self.enterContext(patch.object(main, 'OPERATION_LOCK', self.lock))
        self.enterContext(patch.object(main, 'AGENT_TOKEN', 'evaluation-test-token'))
        self.client = self.enterContext(TestClient(main.app))
        self.headers = {'X-Agent-Token': 'evaluation-test-token'}
        self.payload = {'id': 'd'*32, 'models': ['mobilenet_v2'], 'suite': 'smoke', 'repeats': 1}

    def test_auth_schema_catalog_and_history(self):
        self.assertEqual(self.client.get('/v1/evaluations').status_code, 401)
        catalog = self.client.get('/v1/evaluations/catalog', headers=self.headers).json()
        self.assertEqual(len(catalog['suites']), 3)
        self.assertTrue(catalog['models'][0]['available'])
        for change in [{'models': ['unregistered']}, {'id': '../../anything'}, {'url': 'http://example.invalid'},
                       {'suite': 'recover'}, {'repeats': 2}, {'models': ['mobilenet_v2'] * 2}]:
            result = self.client.post('/v1/evaluations', headers=self.headers, json={**self.payload, **change})
            self.assertEqual(result.status_code, 422)
        self.assertEqual(self.runtime.calls, 0)
        self.assertEqual(self.client.get('/v1/evaluations?limit=101', headers=self.headers).status_code, 422)
        self.assertEqual(self.client.get('/v1/evaluations/invalid', headers=self.headers).status_code, 422)

    def test_persisted_idempotent_result_and_audit_job_projection(self):
        first = self.client.post('/v1/evaluations', headers=self.headers, json=self.payload)
        self.assertEqual(first.status_code, 200)
        self.manager._worker.join(5)
        again = self.client.post('/v1/evaluations', headers=self.headers, json=self.payload)
        self.assertEqual(again.json()['verdict'], 'passed')
        self.assertEqual(self.runtime.calls, 1)
        history = self.client.get('/v1/evaluations', headers=self.headers).json()
        self.assertEqual(len(history['runs']), 1)
        result = self.client.get('/v1/jobs/' + self.payload['id'], headers=self.headers)
        self.assertEqual(result.json()['status'], 'succeeded')
        self.assertEqual(result.json()['result']['evaluation_id'], self.payload['id'])
        conflict = self.client.post('/v1/evaluations', headers=self.headers, json={**self.payload, 'suite': 'repeat'})
        self.assertEqual(conflict.status_code, 409)
        self.assertNotIn('request', again.json()['cases'][0])

    def test_management_mutations_blocked_until_cooperative_cancellation_drains(self):
        self.runtime.release.clear()
        self.client.post('/v1/evaluations', headers=self.headers, json=self.payload)
        self.assertTrue(self.runtime.entered.wait(3))
        try:
            result = self.client.post('/v1/restart/gateway', headers=self.headers)
            self.assertEqual(result.status_code, 409)
            cancel = self.client.post('/v1/evaluations/' + self.payload['id'] + '/cancel', headers=self.headers)
            self.assertEqual(cancel.json()['status'], 'cancelling')
            self.assertTrue(self.lock.locked())
            second = self.client.post('/v1/evaluations', headers=self.headers, json={**self.payload, 'id': 'c'*32})
            self.assertEqual(second.status_code, 409)
        finally:
            self.runtime.release.set()
        self.manager._worker.join(5)
        self.assertFalse(self.lock.locked())
        run = self.client.get('/v1/evaluations/' + self.payload['id'], headers=self.headers).json()
        self.assertEqual(run['status'], 'cancelled')
        self.assertEqual(run['verdict'], 'incomplete')


if __name__ == '__main__':
    unittest.main()
