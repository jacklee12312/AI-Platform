"""Evaluation HTTP and audit contracts, never contact real models."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from fastapi.testclient import TestClient
from admin_app.evaluation_api import Selection, job_snapshot, markdown


def sample_run(status='queued', verdict='pending'):
    return {'id': 'e' * 32, 'status': status, 'verdict': verdict, 'created_at': '2026-09-08T00:00:00Z',
            'suite': 'smoke', 'models': ['mobilenet_v2'], 'repeats': 1, 'version': '2026-09-08.1',
            'summary': {'total': 1, 'passed': 0, 'failed': 0, 'skipped': 0, 'pending': 1, 'latency_ms': {'count': 0, 'mean': None}},
            'cases': [], 'snapshot': {}, 'error': None}


class EvaluationAPITests(unittest.TestCase):
    def setUp(self):
        from admin_app import main
        self.main = main
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(main, 'DATA_DIR', root).start()
        patch.object(main, 'DB_PATH', root / 'modeldeck.db').start()
        self.agent = patch.object(main, '_agent', AsyncMock(return_value=sample_run())).start()
        self.client = self.enterContext(TestClient(main.app))

    def login(self):
        self.main.SESSIONS['eval-test-session'] = {'csrf': 'eval-csrf', 'expires_at': time.time() + 3600}
        self.addCleanup(self.main.SESSIONS.pop, 'eval-test-session', None)
        self.client.cookies.set('modeldeck_session', 'eval-test-session')
        return {'X-CSRF-Token': 'eval-csrf'}

    def payload(self):
        return {'id': 'e' * 32, 'models': ['mobilenet_v2'], 'suite': 'smoke', 'repeats': 1}

    def test_authentication_csrf_and_submission_audit(self):
        self.assertEqual(self.client.get('/api/evaluations/catalog').status_code, 401)
        self.assertEqual(self.client.get('/api/evaluations/' + 'e' * 32 + '/export').status_code, 401)
        headers = self.login()
        self.assertEqual(self.client.post('/api/evaluations', json=self.payload()).status_code, 403)
        response = self.client.post('/api/evaluations', json=self.payload(), headers=headers)
        self.assertEqual(response.status_code, 200)
        audit = self.main._audit_store().read(job_id='e' * 32)
        self.assertTrue(any(row['action'] == 'evaluation-run' and row['status'] == 'queued' for row in audit))

    def test_request_allowlist_and_bounds(self):
        headers = self.login()
        for change in [{'suite': 'shell'}, {'id': '../outside'}, {'models': ['m', 'm']},
                       {'repeats': 4}, {'repeats': True}, {'models': ['m'] * 24}, {'command': 'restart'}, {'repeats': 2}]:
            with self.subTest(change=change):
                response = self.client.post('/api/evaluations', json={**self.payload(), **change}, headers=headers)
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get('/api/evaluations?limit=101').status_code, 422)
        self.assertEqual(self.client.get('/api/evaluations/not-an-id').status_code, 422)

    def test_ambiguous_submission_records_unknown_with_same_recovery_id(self):
        headers = self.login()
        self.agent.side_effect = HTTPException(502, 'connection lost')
        response = self.client.post('/api/evaluations', json=self.payload(), headers=headers)
        self.assertEqual(response.status_code, 502)
        audit = self.main._audit_store().read(job_id='e' * 32)
        self.assertEqual(audit[0]['status'], 'unknown')
        self.assertEqual(self.agent.await_count, 1)

    def test_explicit_denial_and_mismatched_id(self):
        headers = self.login()
        self.agent.side_effect = self.main.AgentRequestError(409, 'busy')
        self.assertEqual(self.client.post('/api/evaluations', json=self.payload(), headers=headers).status_code, 409)
        self.agent.side_effect = None
        self.agent.return_value = {**sample_run(), 'id': 'f' * 32}
        self.assertEqual(self.client.post('/api/evaluations', json=self.payload(), headers=headers).status_code, 502)

    def test_export_does_not_submit_and_cancel_requires_csrf(self):
        headers = self.login()
        for format in ['json', 'md']:
            response = self.client.get('/api/evaluations/' + 'e' * 32 + '/export?format=' + format)
            self.assertEqual(response.status_code, 200)
            self.assertIn('attachment;', response.headers['content-disposition'])
        self.assertEqual(self.client.post('/api/evaluations/' + 'e' * 32 + '/cancel').status_code, 403)
        self.agent.return_value = sample_run('cancelling')
        response = self.client.post('/api/evaluations/' + 'e' * 32 + '/cancel', headers=headers)
        self.assertEqual(response.json()['status'], 'cancelling')

    def test_cancel_interruption_and_partial_results_never_audit_as_success(self):
        for status, verdict in [('completed', 'incomplete'), ('completed', 'failed'), ('cancelled', 'incomplete'), ('interrupted', 'incomplete')]:
            self.assertNotEqual(job_snapshot(sample_run(status, verdict))['status'], 'succeeded')
        self.assertEqual(job_snapshot(sample_run('completed', 'passed'))['status'], 'succeeded')
        self.assertEqual(job_snapshot(sample_run('interrupted', 'incomplete'))['status'], 'unknown')

    def test_markdown_fences_model_output_and_keeps_evidence(self):
        run = sample_run('completed', 'failed')
        run['cases'] = [{'actual': '```\n# forged result\n```', 'expected': '7', 'input_sha256': 'a' * 64}]
        text = markdown(run)
        self.assertIn('````json', text)
        self.assertIn('固定样例', text)
        self.assertIn('input_sha256', text)


if __name__ == '__main__':
    unittest.main()
