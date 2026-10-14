"""Evaluation persistence/concurrency tests; no HTTP, Docker or model dependencies."""
import copy
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.evaluation_engine import (
    EvaluationConflict, EvaluationFatal, EvaluationManager, EvaluationSkip,
    case_documents, public_snapshot,
)
from agent.evaluation_store import EvaluationStore, TERMINAL, encode, selection, summary
from agent.model_store import file_lock


def request(number=1, models=None, suite='smoke', repeats=1):
    return {'id': f'{number:032x}', 'models': models or ['model-a'], 'suite': suite, 'repeats': repeats}


def make_cases(models, suite, repeats):
    return [{'id': f'{model}-{index}', 'model': model, 'name': f'case-{index}',
             'scope': 'quality' if suite == 'quality' else 'execution',
             'input_sha256': hashlib.sha256(b'fixed fixture').hexdigest(),
             'expected': '7', 'parameters': {'temperature': 0}, 'sample': {'text': '3+4'},
             'request': {'private_fixture_payload': 'do-not-persist'}, 'checker': 'equals-7'}
            for model in models for index in range(repeats)]


class Runtime:
    def __init__(self):
        self.prepared = []
        self.checked = []
        self.executed = []
        self.prepare_hook = None
        self.check_hook = None
        self.execute_hook = None
        self.snapshot = {'mode': 'test', 'revision': 1, 'models': {'model-a': {
            'identity': ['container-one', '2026-09-08T00:00:00Z', 0], 'running': True}}}

    def prepare(self, models):
        self.prepared.append(list(models))
        if self.prepare_hook:
            self.prepare_hook(models)
        return copy.deepcopy(self.snapshot)

    def check(self, model, snapshot):
        self.checked.append((model, snapshot))
        if self.check_hook:
            self.check_hook(model, snapshot)

    def execute(self, case):
        self.executed.append(case)
        if self.execute_hook:
            return self.execute_hook(case)
        return {'passed': True, 'actual': '7', 'metrics': {'exact_match': True}}


class EvaluationEngineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.deploy = self.root / 'deployment'
        self.lock = threading.Lock()
        self.runtime = Runtime()
        self.managers = []
        self.addCleanup(self.cleanup_managers)

    def cleanup_managers(self):
        for manager in self.managers:
            manager.close()

    def manager(self, **kwargs):
        manager = EvaluationManager(self.root / 'evaluations', self.deploy, self.lock,
                                    self.runtime, 'fixture-v1', kwargs.pop('make_cases', make_cases), **kwargs)
        self.managers.append(manager)
        return manager

    def finish(self, manager, run_id):
        manager._worker.join(5)
        self.assertFalse(manager._worker.is_alive(), 'evaluation worker did not finish')
        run = manager.get(run_id)
        self.assertIn(run['status'], TERMINAL)
        self.assertFalse(self.lock.locked())
        return run

    def block_execute(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def blocked(case):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test synchronization timed out')
            return {'passed': True, 'actual': '7'}
        self.runtime.execute_hook = blocked
        return entered, release

    def test_success_private_payloads_snapshots_and_all_counts(self):
        manager = self.manager()
        spec = request(models=['model-a', 'model-b'], suite='repeat', repeats=3)
        created = manager.create(spec)
        self.assertEqual(created['id'], spec['id'])
        run = self.finish(manager, spec['id'])
        self.assertEqual((run['status'], run['verdict']), ('completed', 'passed'))
        self.assertEqual({k: run['summary'][k] for k in ('total', 'passed', 'failed', 'skipped', 'pending')},
                         {'total': 6, 'passed': 6, 'failed': 0, 'skipped': 0, 'pending': 0})
        self.assertEqual(run['summary']['latency_ms']['count'], 6)
        self.assertTrue(run['started_at'] and run['finished_at'] and run['created_at'])
        self.assertEqual(run['version'], 'fixture-v1')
        self.assertEqual(set(run['snapshot']), {'start', 'end'})
        self.assertEqual(len(self.runtime.checked), 12)
        self.assertEqual(len(self.runtime.prepared), 2)
        self.assertEqual(self.runtime.executed[0]['request']['private_fixture_payload'], 'do-not-persist')
        for private in ('request', 'checker', 'do-not-persist'):
            self.assertNotIn(private, json.dumps(run))
            self.assertNotIn(private.encode(), manager.store.path.read_bytes())
        self.assertEqual(run, manager.list()[0])

    def test_returned_failure_continues_and_verdict_is_failed(self):
        self.runtime.execute_hook = lambda case: {
            'passed': case['id'] != 'model-a-0', 'actual': 'wrong' if case['id'] == 'model-a-0' else '7',
            'reason': 'fixture mismatch'}
        manager = self.manager()
        spec = request(suite='repeat', repeats=3)
        manager.create(spec)
        run = self.finish(manager, spec['id'])
        self.assertEqual((run['status'], run['verdict']), ('completed', 'failed'))
        self.assertEqual([c['status'] for c in run['cases']], ['failed', 'passed', 'passed'])
        self.assertEqual(run['summary']['latency_ms']['count'], 3)

    def test_availability_skip_preserves_message_and_no_inference_latency(self):
        reason = '模型未安装：请先通过模型管理安装。'
        def check(model, snapshot):
            if model == 'model-a':
                raise EvaluationSkip(reason)
        self.runtime.check_hook = check
        manager = self.manager()
        spec = request(models=['model-a', 'model-b'])
        manager.create(spec)
        run = self.finish(manager, spec['id'])
        self.assertEqual((run['status'], run['verdict']), ('completed', 'incomplete'))
        self.assertEqual(run['cases'][0]['reason'], reason)
        self.assertEqual(run['cases'][0]['status'], 'skipped')
        self.assertNotIn('latency_ms', run['cases'][0])
        self.assertEqual(run['summary']['latency_ms']['count'], 1)
        self.assertEqual([c['model'] for c in self.runtime.executed], ['model-b'])

    def test_execute_skip_conventions_are_explicit_and_bounded(self):
        for raises in (True, False):
            with self.subTest(raises=raises):
                message = 'Unsupported fixture ' + 'x' * 500
                def execute(case):
                    if raises:
                        raise EvaluationSkip(message)
                    return {'status': 'skipped', 'reason': message}
                self.runtime.execute_hook = execute
                manager = self.manager()
                spec = request(number=2 if raises else 3)
                manager.create(spec)
                run = self.finish(manager, spec['id'])
                self.assertEqual((run['status'], run['verdict']), ('completed', 'incomplete'))
                self.assertEqual(run['summary']['skipped'], 1)
                self.assertEqual(len(run['cases'][0]['reason']), 300)
                self.assertEqual(run['summary']['latency_ms']['count'], 0)

    def test_empty_case_set_never_passes(self):
        manager = self.manager(make_cases=lambda *args: [])
        manager.create(request())
        run = self.finish(manager, request()['id'])
        self.assertEqual((run['status'], run['verdict'], run['summary']['total']), ('completed', 'incomplete', 0))

    def test_runtime_failure_preserves_completed_and_aborts_unknown(self):
        def execute(case):
            if case['id'].endswith('-1'):
                raise RuntimeError('Bearer upstream-password-secret')
            return {'passed': True, 'actual': '7'}
        self.runtime.execute_hook = execute
        manager = self.manager()
        spec = request(suite='repeat', repeats=3)
        manager.create(spec)
        run = self.finish(manager, spec['id'])
        self.assertEqual((run['status'], run['verdict'], run['error']), ('failed', 'incomplete', 'RuntimeError'))
        self.assertEqual([c['status'] for c in run['cases']], ['passed', 'interrupted', 'pending'])
        self.assertEqual(len(self.runtime.executed), 2)
        self.assertEqual(run['summary']['pending'], 2)
        self.assertEqual(run['summary']['latency_ms']['count'], 1)
        self.assertNotIn('upstream-password-secret', json.dumps(run))
        self.assertNotIn(b'upstream-password-secret', manager.store.path.read_bytes())

    def test_timeout_never_retries_or_claims_known_outcome(self):
        def execute(case):
            raise TimeoutError('credential in URL')
        self.runtime.execute_hook = execute
        manager = self.manager()
        spec = request(suite='repeat', repeats=3)
        manager.create(spec)
        run = self.finish(manager, spec['id'])
        self.assertEqual((run['status'], run['verdict'], run['error']), ('interrupted', 'incomplete', 'TimeoutError'))
        self.assertEqual([c['status'] for c in run['cases']], ['interrupted', 'pending', 'pending'])
        self.assertEqual(len(self.runtime.executed), 1)
        self.assertEqual(run['summary']['latency_ms']['count'], 0)

    def test_post_request_drift_rejects_current_output_and_stops_remainder(self):
        def check(model, snapshot):
            if len(self.runtime.checked) == 4:
                raise RuntimeError('new identity private secret')
        self.runtime.check_hook = check
        manager = self.manager()
        spec = request(suite='repeat', repeats=3)
        manager.create(spec)
        run = self.finish(manager, spec['id'])
        self.assertEqual([c['status'] for c in run['cases']], ['passed', 'interrupted', 'pending'])
        self.assertNotIn('actual', run['cases'][1])
        self.assertNotIn('latency_ms', run['cases'][1])
        self.assertEqual(run['summary']['latency_ms']['count'], 1)
        self.assertEqual(len(self.runtime.executed), 2)

    def test_last_case_postcheck_is_required_for_pass(self):
        def check(model, snapshot):
            if len(self.runtime.checked) == 2:
                raise EvaluationSkip('service vanished')
        self.runtime.check_hook = check
        manager = self.manager()
        manager.create(request())
        run = self.finish(manager, request()['id'])
        self.assertEqual((run['status'], run['verdict'], run['error']), ('failed', 'incomplete', 'EvaluationFatal'))
        self.assertEqual(run['cases'][0]['status'], 'interrupted')
        self.assertNotIn('actual', run['cases'][0])

    def test_check_failure_before_execute_is_not_a_case_failure(self):
        def check(model, snapshot):
            raise EvaluationFatal('state drift with secret details')
        self.runtime.check_hook = check
        manager = self.manager()
        manager.create(request())
        run = self.finish(manager, request()['id'])
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(run['cases'][0]['status'], 'pending')
        self.assertEqual(run['summary']['failed'], 0)
        self.assertFalse(self.runtime.executed)

    def test_cancel_during_execute_keeps_current_validated_result_and_prevents_next(self):
        entered, release = self.block_execute()
        manager = self.manager()
        spec = request(suite='repeat', repeats=3)
        manager.create(spec)
        self.assertTrue(entered.wait(3))
        self.assertEqual(manager.active_run_id, spec['id'])
        self.assertEqual(manager.cancel(spec['id'])['status'], 'cancelling')
        self.assertEqual(manager.active_run_id, spec['id'])
        self.assertEqual(manager.cancel(spec['id'])['status'], 'cancelling')
        self.assertEqual(manager.create(spec)['status'], 'cancelling')
        with self.assertRaises(EvaluationConflict):
            manager.create(request(2))
        self.assertTrue(self.lock.locked())
        release.set()
        run = self.finish(manager, spec['id'])
        self.assertIsNone(manager.active_run_id)
        self.assertEqual((run['status'], run['verdict']), ('cancelled', 'incomplete'))
        self.assertEqual([c['status'] for c in run['cases']], ['passed', 'pending', 'pending'])
        self.assertEqual(len(self.runtime.executed), 1)
        self.assertEqual(len(self.runtime.checked), 2)
        self.assertEqual(len(self.runtime.prepared), 1)
        self.runtime.execute_hook = None
        manager.create(request(2))
        self.assertEqual(self.finish(manager, request(2)['id'])['verdict'], 'passed')

    def test_cancel_during_last_postcheck_cannot_be_overwritten(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def check(model, snapshot):
            if len(self.runtime.checked) == 2:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError()
        self.runtime.check_hook = check
        manager = self.manager()
        manager.create(request())
        self.assertTrue(entered.wait(3))
        manager.cancel(request()['id'])
        release.set()
        run = self.finish(manager, request()['id'])
        self.assertEqual((run['status'], run['verdict']), ('cancelled', 'incomplete'))
        self.assertEqual(run['cases'][0]['status'], 'passed')

    def test_cancel_while_queued_never_prepares_or_executes(self):
        manager = self.manager()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        start = manager.store.start
        def gated_start(run_id):
            entered.set()
            if not release.wait(5):
                raise TimeoutError()
            return start(run_id)
        with patch.object(manager.store, 'start', side_effect=gated_start):
            manager.create(request())
            self.assertTrue(entered.wait(3))
            self.assertEqual(manager.get(request()['id'])['status'], 'queued')
            manager.cancel(request()['id'])
            release.set()
            run = self.finish(manager, request()['id'])
        self.assertEqual((run['status'], run['verdict']), ('cancelled', 'incomplete'))
        self.assertIsNone(run['started_at'])
        self.assertEqual(run['cases'][0]['status'], 'pending')
        self.assertFalse(self.runtime.prepared or self.runtime.checked or self.runtime.executed)

    def test_cancel_during_final_snapshot_is_monotonic(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def prepare(models):
            if len(self.runtime.prepared) == 2:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError()
        self.runtime.prepare_hook = prepare
        manager = self.manager()
        manager.create(request())
        self.assertTrue(entered.wait(3))
        manager.cancel(request()['id'])
        release.set()
        run = self.finish(manager, request()['id'])
        self.assertEqual((run['status'], run['verdict']), ('cancelled', 'incomplete'))
        self.assertEqual(run['cases'][0]['status'], 'passed')

    def test_cancel_and_timeout_retains_unknown_incomplete(self):
        entered, release = self.block_execute()
        def execute(case):
            entered.set()
            release.wait(5)
            raise TimeoutError('upstream timeout')
        self.runtime.execute_hook = execute
        manager = self.manager()
        manager.create(request())
        self.assertTrue(entered.wait(3))
        manager.cancel(request()['id'])
        release.set()
        run = self.finish(manager, request()['id'])
        self.assertEqual((run['status'], run['verdict']), ('interrupted', 'incomplete'))
        self.assertEqual(run['cases'][0]['status'], 'interrupted')

    def test_concurrent_duplicate_is_one_thread_one_execution(self):
        entered, release = self.block_execute()
        calls = []
        def factory(*args):
            calls.append(args)
            return make_cases(*args)
        manager = self.manager(make_cases=factory)
        barrier = threading.Barrier(8)
        results, errors = [], []
        def create():
            try:
                barrier.wait(3)
                results.append(manager.create(request()))
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=create) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(4)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertTrue(entered.wait(3))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.runtime.executed), 1)
        with self.assertRaises(EvaluationConflict) as caught:
            manager.create(request(models=['model-b']))
        self.assertEqual(caught.exception.status_code, 409)
        release.set()
        run = self.finish(manager, request()['id'])
        self.assertEqual(manager.create(request()), run)
        self.assertEqual(len(calls), 1)

    def test_owner_lock_conflict_does_not_persist_or_call_runtime(self):
        manager = self.manager()
        with self.lock:
            with self.assertRaises(EvaluationConflict):
                manager.create(request())
            self.assertEqual(manager.list(), [])
        self.assertEqual(self.runtime.prepared, [])
        manager.create(request())
        self.finish(manager, request()['id'])

    def test_modelctl_lock_conflict_is_durable_and_owner_lock_released(self):
        manager = self.manager()
        with file_lock(self.deploy / 'state/modelctl.lock'):
            manager.create(request())
            run = self.finish(manager, request()['id'])
        self.assertEqual((run['status'], run['verdict']), ('failed', 'incomplete'))
        self.assertEqual(self.runtime.prepared, [])
        manager.create(request(2))
        self.assertEqual(self.finish(manager, request(2)['id'])['verdict'], 'passed')

    def test_both_locks_held_for_prepare_check_execute_and_end(self):
        manager = self.manager()
        seen = []
        def assert_locked(phase):
            self.assertTrue(self.lock.locked())
            with self.assertRaises(RuntimeError):
                with file_lock(self.deploy / 'state/modelctl.lock'):
                    self.fail('file lock was not held')
            seen.append(phase)
        self.runtime.prepare_hook = lambda models: assert_locked('prepare')
        self.runtime.check_hook = lambda model, snapshot: assert_locked('check')
        def execute(case):
            assert_locked('execute')
            return {'passed': True, 'actual': '7'}
        self.runtime.execute_hook = execute
        manager.create(request())
        run = self.finish(manager, request()['id'])
        self.assertEqual(run['verdict'], 'passed')
        self.assertEqual(seen, ['prepare', 'check', 'execute', 'check', 'prepare'])
        with file_lock(self.deploy / 'state/modelctl.lock'):
            pass
        self.assertEqual([p.name for p in (self.deploy / 'state').iterdir()], ['modelctl.lock'])

    def test_deadline_reserves_next_call_budget_and_preserves_finished_cases(self):
        clock = [0.0]
        def execute(case):
            clock[0] += 150
            return {'passed': True, 'actual': '7'}
        self.runtime.execute_hook = execute
        manager = self.manager(deadline_seconds=400, clock=lambda: clock[0])
        spec = request(suite='repeat', repeats=3)
        manager.create(spec)
        run = self.finish(manager, spec['id'])
        self.assertEqual((run['status'], run['error']), ('interrupted', 'EvaluationDeadline'))
        self.assertEqual([c['status'] for c in run['cases']], ['passed', 'interrupted', 'pending'])
        self.assertEqual(run['cases'][0]['latency_ms'], 150000)
        self.assertNotIn('actual', run['cases'][1])
        self.assertEqual(len(self.runtime.executed), 2)

    def test_deadline_below_operation_budget_never_starts_runtime(self):
        manager = self.manager(deadline_seconds=1)
        manager.create(request())
        run = self.finish(manager, request()['id'])
        self.assertEqual(run['status'], 'interrupted')
        self.assertFalse(self.runtime.prepared)

    def test_close_waits_for_current_request_and_stops_admission(self):
        entered, release = self.block_execute()
        manager = self.manager()
        manager.create(request())
        self.assertTrue(entered.wait(3))
        closed = threading.Event()
        def close():
            manager.close()
            closed.set()
        thread = threading.Thread(target=close)
        thread.start()
        self.assertFalse(closed.wait(.05))
        with self.assertRaises(EvaluationConflict):
            manager.create(request(2))
        release.set()
        thread.join(5)
        self.assertTrue(closed.is_set())
        self.assertFalse(self.lock.locked())
        self.assertEqual(manager.get(request()['id'])['status'], 'cancelled')
        manager.close()
        with self.assertRaises(EvaluationConflict):
            manager.create(request(2))

    def test_worker_start_failure_is_durable_and_releases_owner_lock(self):
        manager = self.manager()
        with patch('agent.evaluation_engine.threading.Thread.start', side_effect=RuntimeError('start failed')):
            with self.assertRaises(RuntimeError):
                manager.create(request())
        self.assertFalse(self.lock.locked())
        self.assertEqual(manager.get(request()['id'])['error'], 'EvaluationWorkerStartError')
        self.assertIsNone(manager.active_run_id)
        manager.create(request(2))
        self.finish(manager, request(2)['id'])

    def test_close_deadline_retains_locks_until_worker_really_exits(self):
        entered, release = self.block_execute()
        manager = self.manager()
        manager.create(request())
        self.assertTrue(entered.wait(3))
        manager.close(timeout=.01)
        run = manager.get(request()['id'])
        self.assertEqual((run['status'], run['verdict'], run['error']),
                         ('interrupted', 'incomplete', 'EvaluationShutdownTimeout'))
        self.assertEqual(run['cases'][0]['status'], 'interrupted')
        self.assertTrue(self.lock.locked())
        self.assertEqual(manager.active_run_id, request()['id'])
        with self.assertRaises(RuntimeError):
            with file_lock(self.deploy / 'state/modelctl.lock'):
                self.fail('timed-out worker must retain file lock')
        release.set()
        self.assertEqual(self.finish(manager, request()['id']), run)
        self.assertIsNone(manager.active_run_id)
        self.assertEqual(len(self.runtime.checked), 1)
        self.assertEqual(len(self.runtime.executed), 1)

    def test_case_factory_can_premark_unsupported_without_runtime_check_or_execute(self):
        def factory(*args):
            cases = make_cases(*args)
            cases[0].update(status='skipped', reason='unsupported: no quality fixture')
            return cases
        manager = self.manager(make_cases=factory)
        manager.create(request())
        run = self.finish(manager, request()['id'])
        self.assertEqual((run['status'], run['verdict']), ('completed', 'incomplete'))
        self.assertEqual(run['cases'][0]['reason'], 'unsupported: no quality fixture')
        self.assertFalse(self.runtime.executed or self.runtime.checked)
        self.assertEqual(run['summary']['latency_ms']['count'], 0)

    def test_adapter_inference_latency_takes_precedence_over_control_overhead(self):
        clock = [0.0]
        def execute(case):
            clock[0] += 3
            return {'passed': True, 'actual': '7', 'metrics': {'wall_ms': 12.5}}
        self.runtime.execute_hook = execute
        manager = self.manager(clock=lambda: clock[0])
        manager.create(request())
        run = self.finish(manager, request()['id'])
        self.assertEqual(run['cases'][0]['latency_ms'], 12.5)
        self.assertEqual(run['summary']['latency_ms']['mean'], 12.5)

    def test_snapshot_projection_covers_published_runtime_shape(self):
        self.runtime.snapshot = {
            'mode': 'qwen38', 'mode_sha256': 'a' * 64, 'registry_sha256': 'b' * 64,
            'installstate_sha256': 'c' * 64, 'catalog_sha256': 'd' * 64, 'baseline_sha256': 'e' * 64,
            'grader_sha256': 'f' * 64,
            'evaluator': {'id': 'agent-one', 'started_at': '2026-09-08T00:00:00Z', 'restart_count': 0,
                          'image_id': 'sha256:' + 'a' * 64, 'image': 'gateway-admin:fixed', 'running': True, 'restarting': False},
            'gateway': {'id': 'gateway-one', 'started_at': '2026-09-08T00:00:00Z', 'restart_count': 0,
                        'image_id': 'sha256:' + 'f' * 64, 'image': 'model-gateway:fixed', 'running': True, 'restarting': False},
            'containers': {'container-one': {'id': 'container-one', 'running': True}, 'container-absent': None},
            'models': {'model-a': {'installed': True, 'enabled': True, 'registered': True, 'container': 'container-one',
                                   'active': True, 'declared_version': 'v1', 'declared_image': 'model:fixed',
                                   'declared_artifacts': [{'target': 'models/weights.bin', 'size': 42, 'sha256': 'a' * 64}]}},
            'safety': {'revision': 3, 'boot_id': 'boot-one'}, 'case_version': '2026-09-08.1',
            'artifact_hash_scope': 'declared_catalog_not_verified_weights',
            'execution_path': 'gateway_management_no_customer_billing', 'notice': '固定用例的局部验收。'}
        manager = self.manager()
        manager.create(request())
        run = self.finish(manager, request()['id'])
        self.assertEqual(run['snapshot']['start'], self.runtime.snapshot)
        self.assertEqual(run['snapshot']['end'], self.runtime.snapshot)

    def test_final_snapshot_drift_prevents_green_but_preserves_completed_cases(self):
        def prepare(models):
            if len(self.runtime.prepared) == 2:
                self.runtime.snapshot['revision'] = 2
        self.runtime.prepare_hook = prepare
        manager = self.manager()
        manager.create(request())
        run = self.finish(manager, request()['id'])
        self.assertEqual((run['status'], run['verdict'], run['error']), ('failed', 'incomplete', 'EvaluationFatal'))
        self.assertEqual(run['cases'][0]['status'], 'passed')
        self.assertEqual(run['snapshot']['start']['revision'], 1)
        self.assertEqual(run['snapshot']['end']['revision'], 2)

    def test_snapshot_observation_time_does_not_count_as_state_drift(self):
        def prepare(models):
            self.runtime.snapshot['captured_at'] = str(len(self.runtime.prepared))
        self.runtime.prepare_hook = prepare
        manager = self.manager()
        manager.create(request())
        run = self.finish(manager, request()['id'])
        self.assertEqual(run['verdict'], 'passed')
        self.assertNotEqual(run['snapshot']['start']['captured_at'], run['snapshot']['end']['captured_at'])

    def test_invalid_factory_does_not_reserve_id_or_leak_lock(self):
        manager = self.manager(make_cases=lambda *args: [{'request': 'private'}])
        with self.assertRaises(ValueError):
            manager.create(request())
        self.assertFalse(self.lock.locked())
        self.assertEqual(manager.list(), [])

    def test_malformed_runtime_result_aborts_without_invented_pass(self):
        self.runtime.execute_hook = lambda case: {'passed': 'true', 'actual': '7'}
        manager = self.manager()
        manager.create(request())
        run = self.finish(manager, request()['id'])
        self.assertEqual((run['status'], run['verdict']), ('failed', 'incomplete'))
        self.assertEqual(run['cases'][0]['status'], 'interrupted')

    def test_snapshot_allowlist_keeps_identities_and_drops_secrets(self):
        self.runtime.snapshot.update(api_key='fixture-secret', Authorization='Bearer fixture-secret',
                                     raw={'password': 'fixture-secret'}, endpoint='https://user:fixture-secret@host')
        self.runtime.snapshot['models']['model-a'].update(
            env=['TOKEN=fixture-secret'], headers={'authorization': 'fixture-secret'},
            config={'password': 'fixture-secret'})
        manager = self.manager()
        manager.create(request())
        run = self.finish(manager, request()['id'])
        self.assertEqual(run['snapshot']['start']['models']['model-a']['identity'],
                         self.runtime.snapshot['models']['model-a']['identity'])
        self.assertNotIn('fixture-secret', json.dumps(run))
        self.assertNotIn(b'fixture-secret', manager.store.path.read_bytes())
        self.assertIn('api_key', self.runtime.checked[0][1])
        self.assertEqual(public_snapshot({'id': 'https://user:secret@host'})['id'], '[redacted]')

    def test_custom_exception_name_and_message_are_not_published(self):
        secret_error = type('secret_in_exception_type', (Exception,), {})
        def execute(case):
            raise secret_error('secret in exception message')
        self.runtime.execute_hook = execute
        manager = self.manager()
        manager.create(request())
        run = self.finish(manager, request()['id'])
        self.assertEqual(run['error'], 'EvaluationError')
        self.assertNotIn('secret', json.dumps(run))


class EvaluationStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = EvaluationStore(self.root)

    def create(self, spec=None):
        spec = spec or request(suite='repeat', repeats=3)
        return self.store.create(spec, case_documents(make_cases(spec['models'], spec['suite'], spec['repeats']),
                                                     spec['models']), 'fixture-v1')[0]

    def test_restart_marks_all_active_runs_and_only_running_cases_interrupted(self):
        for number, status in enumerate(('queued', 'running', 'cancelling'), 1):
            spec = request(number, suite='repeat', repeats=3)
            run = self.create(spec)
            if status != 'queued':
                self.store.start(run['id'])
                self.store.start_case(run['id'], 'model-a-0')
                self.store.finish_case(run['id'], 'model-a-0', {'status': 'passed', 'actual': '7', 'latency_ms': 10})
                self.store.start_case(run['id'], 'model-a-1')
            if status == 'cancelling':
                self.store.cancel(run['id'])
        runtime = Runtime()
        manager = EvaluationManager(self.root, self.root / 'deployment', threading.Lock(), runtime, 'new-version', make_cases)
        self.addCleanup(manager.close)
        self.assertFalse(runtime.prepared or runtime.executed or runtime.checked)
        self.assertIsNone(manager._worker)
        for run in manager.list():
            self.assertEqual((run['status'], run['verdict'], run['error']), ('interrupted', 'incomplete', 'EvaluationRestarted'))
            self.assertEqual(run['version'], 'fixture-v1')
            if run['id'] != request(1)['id']:
                self.assertEqual([c['status'] for c in run['cases']], ['passed', 'interrupted', 'pending'])
                self.assertEqual(run['summary']['passed'], 1)
                self.assertEqual(run['summary']['pending'], 2)
            self.assertEqual(manager.create({k: run[k] for k in ('id', 'models', 'suite', 'repeats')}), run)
        self.assertFalse(runtime.executed)

    def test_terminal_records_cannot_regress_or_accept_late_worker_results(self):
        for number, status in enumerate(sorted(TERMINAL), 1):
            run = self.create(request(number))
            self.store.start(run['id'])
            self.store.start_case(run['id'], 'model-a-0')
            terminal = self.store.finish(run['id'], status)
            self.assertEqual(self.store.cancel(run['id']), terminal)
            self.assertEqual(self.store.start(run['id']), terminal)
            self.assertEqual(self.store.finish_case(run['id'], 'model-a-0', {'status': 'passed', 'actual': 'late'}), terminal)
            self.assertEqual(self.store.finish(run['id'], 'completed'), terminal)
        before = self.store.list()
        self.store.recover()
        self.assertEqual(self.store.list(), before)

    def test_cancellation_survives_stale_worker_update_and_other_store_instance(self):
        run = self.create(request())
        self.store.start(run['id'])
        self.store.start_case(run['id'], 'model-a-0')
        other = EvaluationStore(self.root)
        other.cancel(run['id'])
        after = self.store.finish_case(run['id'], 'model-a-0', {'status': 'passed', 'actual': '7'})
        self.assertEqual(after['status'], 'cancelling')
        self.assertEqual(self.store.finish(run['id'], 'completed')['status'], 'cancelled')
        self.assertEqual(other.get(run['id'])['verdict'], 'incomplete')

    def test_bounded_history_without_eviction_and_durable_idempotency_at_capacity(self):
        first = self.create(request(1))
        # Seed old history in one transaction; exercise the real boundary insert.
        with self.store.db() as db:
            for number in range(2, 1000):
                run = copy.deepcopy(first)
                run['id'] = request(number)['id']
                db.execute('INSERT INTO evaluation_runs(id,document) VALUES(?,?)', (run['id'], encode(run)))
        last = self.create(request(1000))
        self.assertEqual(self.store.get(first['id']), first)
        self.assertEqual(len(self.store.list()), 30)
        self.assertEqual(len(self.store.list(10000)), 100)
        self.assertEqual(self.store.list(1), [last])
        with self.assertRaises(EvaluationConflict):
            self.create(request(1001))
        self.assertEqual(self.create(request(1)), first)
        with self.assertRaises(EvaluationConflict):
            self.create(request(1, models=['model-b']))
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM evaluation_runs').fetchone()[0], 1000)

    def test_get_and_list_return_independent_values(self):
        run = self.create(request())
        run['cases'][0]['expected'] = 'mutated'
        self.store.list()[0]['models'].append('mutated')
        self.assertEqual(self.store.get(run['id'])['cases'][0]['expected'], '7')
        self.assertEqual(self.store.get(run['id'])['models'], ['model-a'])
        with self.assertRaises(KeyError):
            self.store.get(request(999)['id'])
        for limit in (0, -1, True, '30', None):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.store.list(limit)

    def test_summary_uses_only_measured_finite_completed_latencies(self):
        cases = [{'status': 'passed', 'latency_ms': 10}, {'status': 'failed', 'latency_ms': 30},
                 {'status': 'passed'}, {'status': 'skipped', 'latency_ms': 0},
                 {'status': 'pending'}, {'status': 'running'}, {'status': 'interrupted', 'latency_ms': 90},
                 {'status': 'passed', 'latency_ms': float('nan')}, {'status': 'failed', 'latency_ms': -1},
                 {'status': 'passed', 'latency_ms': True}]
        result = summary(cases)
        self.assertEqual(result['latency_ms'], {'count': 2, 'mean': 20, 'p50': 20, 'p95': 29, 'min': 10, 'max': 30})
        self.assertEqual(result['pending'], 3)
        self.assertEqual(sum(result[k] for k in ('passed', 'failed', 'skipped', 'pending')), result['total'])
        self.assertEqual(summary([])['latency_ms'], {'count': 0, 'mean': None, 'p50': None, 'p95': None, 'min': None, 'max': None})

    def test_request_validation_covers_id_model_suite_and_repeat_contract(self):
        invalid = [dict(request(), id='A' * 32), dict(request(), id='a' * 31), dict(request(), id='../bad'),
                   dict(request(), models=[]), dict(request(), models=['a'] * 2),
                   dict(request(), models=[str(i) for i in range(24)]), dict(request(), models='a'),
                   dict(request(), models=[{}]), dict(request(), suite='other'), dict(request(), suite=[]),
                   dict(request(), repeats=True), dict(request(), repeats=0), dict(request(), repeats=4),
                   dict(request(), repeats=2), dict(request(), suite='quality', repeats=3),
                   dict(request(), extra='field')]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                selection(value)
        self.assertEqual(selection(request(suite='repeat', repeats=3))['repeats'], 3)
        self.assertEqual(len(selection(request(models=[str(i) for i in range(23)]))['models']), 23)


if __name__ == '__main__':
    unittest.main()
