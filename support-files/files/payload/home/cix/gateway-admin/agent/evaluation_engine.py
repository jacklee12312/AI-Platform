"""Single-worker, read-only runtime evaluation, with durable client idempotency.

Integration: EvaluationManager(root, deploy, lock, runtime, catalog_version,
make_cases). root owns evaluation-runs.db; deploy owns state/modelctl.lock.
create(selection), get(id), list(limit=30), cancel(id) return run dictionaries
(list returns a list). Conflicts raise EvaluationConflict(ValueError), with
status_code=409; missing ids raise KeyError. close() cooperatively drains work.

runtime.prepare(models) returns a snapshot and never starts/changes services.
runtime.check(model, snapshot) confirms availability and unchanged state.
runtime.execute(case) returns {actual, passed: bool, reason?, metrics?}, or
{status: 'skipped', reason: <static availability message>}. EvaluationSkip is
also accepted; its intentional public message must not contain upstream data.
All runtime operations MUST be bounded to <=180 seconds by the adapter. The
engine reserves that budget before every call, never retries, and never tries
to unload a model or cancel an upstream request. check runs before AND after
each executed case to catch drift during the final request too.

make_cases(models, suite, repeats) returns deterministic dictionaries with id,
model, name, scope, input_sha256, expected, parameters and optional sample.
Extra private input fields reach execute but are not persisted. Snapshot
projection is allowlisted; private runtime snapshots stay in memory for check.
"""
from __future__ import annotations

import copy
import math
import re
import threading
import time

from .evaluation_store import EvaluationConflict, EvaluationStore, TERMINAL, encode, same_selection, selection
from .model_store import file_lock

MAX_OPERATION_SECONDS = 180
MAX_DEADLINE_SECONDS = 1800


class EvaluationSkip(Exception):
    """An intentional public, static availability/unsupported explanation."""
    def __init__(self, reason='unavailable'):
        self.reason = skip_reason(reason)
        super().__init__(self.reason)


class EvaluationFatal(RuntimeError):
    """Infrastructure/state drift: abort remaining cases, keep earlier evidence."""


class EvaluationDeadline(TimeoutError):
    """Insufficient remaining run budget for another bounded runtime operation."""


class _Cancelled(Exception):
    pass


class _Stopped(Exception):
    pass


def skip_reason(value):
    if not isinstance(value, str) or not value.strip():
        return 'unavailable_or_unsupported'
    return ''.join(char for char in value.strip() if char.isprintable())[:300]


def exception_code(exc):
    # Fixed types only: even a custom exception class name can contain secrets.
    for cls in (EvaluationDeadline, EvaluationFatal, TimeoutError, ConnectionError,
                ValueError, TypeError, OSError, RuntimeError):
        if isinstance(exc, cls):
            return cls.__name__
    return 'EvaluationError'


_SNAPSHOT_FIELDS = frozenset({
    'mode', 'revision', 'safety_revision', 'catalog_version', 'version', 'model_version',
    'mode_sha256', 'registry_sha256', 'installations_sha256', 'installstate_sha256',
    'catalog_sha256', 'baseline_sha256', 'grader_sha256', 'config_sha256', 'state_sha256', 'sha256',
    'fingerprint', 'id', 'model', 'container', 'container_id', 'image', 'image_id',
    'identity', 'started_at', 'restart_count', 'installed', 'enabled', 'running',
    'healthy', 'available', 'supported', 'status', 'incident_id', 'maintenance',
    'restarting', 'registered', 'active', 'declared_version', 'declared_image',
    'declared_artifacts', 'target', 'size', 'case_version', 'artifact_hash_scope',
    'execution_path', 'notice',
    'captured_at', 'checked_at', 'boot_id',
})
_SNAPSHOT_MAPS = frozenset({'models', 'services', 'containers', 'images', 'versions', 'identities'})


def public_snapshot(value):
    """Project inventory/identity fields, dropping env, headers, URLs and raw data.

    The adapter must put only nonsecret inventory facts in these documented
    fields. Unknown fields (including arbitrary nested dictionaries) are omitted.
    """
    def visit(item, depth=0, mapping=False):
        if depth > 10:
            return None
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                if not isinstance(key, str):
                    continue
                if mapping:
                    if re.fullmatch(r'[a-zA-Z0-9_.:/-]{1,128}', key) and (child is None or isinstance(child, (dict, list, tuple))):
                        result[key] = visit(child, depth + 1)
                elif key in _SNAPSHOT_FIELDS | _SNAPSHOT_MAPS | {'safety', 'gateway', 'evaluator'}:
                    result[key] = visit(child, depth + 1, key in _SNAPSHOT_MAPS)
            return result
        if isinstance(item, (list, tuple)):
            return [visit(child, depth + 1) for child in item[:256]]
        if item is None or type(item) in (bool, int):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else None
        if isinstance(item, str):
            # Inventory identifiers need neither userinfo nor query strings.
            if any(mark in item.lower() for mark in ('://', 'bearer ', 'basic ', '\n', '\r', '?', '=')):
                return '[redacted]'
            return item[:256]
        return None
    if not isinstance(value, dict):
        raise ValueError('Runtime snapshot must be a dictionary')
    return visit(value)


def _snapshot_state(value):
    """Observation timestamps may change between otherwise identical snapshots."""
    if isinstance(value, dict):
        return {key: _snapshot_state(child) for key, child in value.items()
                if key not in {'captured_at', 'checked_at'}}
    if isinstance(value, list):
        return [_snapshot_state(child) for child in value]
    return value


def case_documents(cases, models):
    if not isinstance(cases, list) or len(cases) > 2048:
        raise ValueError('Invalid evaluation cases')
    documents, seen = [], set()
    for case in cases:
        required = {'id', 'model', 'name', 'scope', 'input_sha256', 'expected', 'parameters'}
        if not isinstance(case, dict) or not required <= case.keys():
            raise ValueError('Incomplete evaluation case')
        if (not isinstance(case['id'], str) or not case['id'] or len(case['id']) > 200
                or case['id'] in seen or case['model'] not in models
                or not isinstance(case['name'], str) or not case['name'] or len(case['name']) > 200
                or case['scope'] not in ('execution', 'quality')
                or not isinstance(case['input_sha256'], str)
                or re.fullmatch(r'[0-9a-f]{64}', case['input_sha256']) is None
                or not isinstance(case['parameters'], dict)):
            raise ValueError('Invalid evaluation case')
        seen.add(case['id'])
        document = {key: copy.deepcopy(case[key]) for key in required | {'sample'} if key in case}
        document['status'] = 'skipped' if case.get('status') == 'skipped' else 'pending'
        if document['status'] == 'skipped':
            document['reason'] = skip_reason(case.get('reason'))
        documents.append(document)
    encode(documents)
    return documents


class EvaluationManager:
    def __init__(self, root, deploy, lock, runtime, catalog_version, make_cases, *,
                 deadline_seconds=MAX_DEADLINE_SECONDS, clock=time.monotonic):
        if (type(deadline_seconds) not in (int, float) or not math.isfinite(deadline_seconds)
                or not 0 < deadline_seconds <= MAX_DEADLINE_SECONDS):
            raise ValueError('Evaluation deadline must be between 0 and 1800 seconds')
        from pathlib import Path
        self.store = EvaluationStore(root)
        self.deploy = Path(deploy)
        self.operation_lock = lock
        self.runtime = runtime
        self.catalog_version = copy.deepcopy(catalog_version)
        self.make_cases = make_cases
        self.deadline_seconds, self.clock = deadline_seconds, clock
        self._guard = threading.Lock()
        self._worker = None
        self._active_id = None
        self._closed = False
        # Owner creates one manager per store during startup, before serving API.
        self.store.recover()

    def get(self, run_id):
        return self.store.get(run_id)

    @property
    def active_run_id(self):
        """The owned worker id, including cancellation, until both locks release."""
        with self._guard:
            return self._active_id

    def list(self, limit=30):
        return self.store.list(limit)

    def create(self, value):
        request = selection(value)
        with self._guard:
            try:
                old = self.get(request['id'])
            except KeyError:
                old = None
            if old is not None:
                same_selection(old, request)
                return old
            if self._closed:
                raise EvaluationConflict('Evaluation manager is closed')
            if self._active_id is not None or not self.operation_lock.acquire(blocking=False):
                raise EvaluationConflict('Another management operation is running')
            created = False
            try:
                cases = copy.deepcopy(self.make_cases(list(request['models']), request['suite'], request['repeats']))
                documents = case_documents(cases, request['models'])
                run, created = self.store.create(request, documents, self.catalog_version)
                if not created:
                    self.operation_lock.release()
                    return run
                self._active_id = run['id']
                self._worker = threading.Thread(target=self._work, args=(run['id'], cases),
                                                daemon=True, name='evaluation-' + run['id'][:8])
                self._worker.start()
                return run
            except BaseException:
                try:
                    if created:
                        self.store.finish(request['id'], 'failed', 'EvaluationWorkerStartError', 'worker_not_started')
                finally:
                    self._active_id = None
                    self._worker = None
                    self.operation_lock.release()
                raise

    def cancel(self, run_id):
        return self.store.cancel(run_id)

    def close(self, *, timeout=190):
        """Stop admission and wait at most 190 seconds for cooperative shutdown.

        No upstream cancellation/unload is implied. Integrations should call
        this off the event loop, like the owner's other blocking close methods.
        If an adapter violates its deadline, mark unknown and retain BOTH locks
        until its worker actually exits. A late result cannot overwrite this.
        """
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 190:
            raise ValueError('Evaluation shutdown timeout must be between 0 and 190 seconds')
        with self._guard:
            self._closed = True
            if self._active_id is not None:
                self.cancel(self._active_id)
            worker = self._worker
            run_id = self._active_id
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout)
            if worker.is_alive() and run_id is not None:
                self.store.finish(run_id, 'interrupted', 'EvaluationShutdownTimeout', 'shutdown_outcome_unknown')

    def _boundary(self, run_id, deadline, *, finishing_request=False, reserve_seconds=MAX_OPERATION_SECONDS):
        status = self.get(run_id)['status']
        if status in TERMINAL:
            raise _Stopped()
        if not finishing_request and status == 'cancelling':
            raise _Cancelled()
        if deadline - self.clock() < reserve_seconds:
            raise EvaluationDeadline()

    def _check(self, run_id, model, snapshot, deadline, *, finishing_request=False):
        self._boundary(run_id, deadline, finishing_request=finishing_request)
        self.runtime.check(model, copy.deepcopy(snapshot))

    def _execute(self, run_id, case, snapshot, deadline):
        case_id = case['id']
        self._boundary(run_id, deadline, reserve_seconds=0)
        if case.get('status') == 'skipped':
            return
        try:
            self._check(run_id, case['model'], snapshot, deadline)
        except EvaluationSkip as exc:
            self.store.finish_case(run_id, case_id, {'status': 'skipped', 'reason': exc.reason})
            return
        self._boundary(run_id, deadline)
        run = self.store.start_case(run_id, case_id)
        if run['status'] != 'running':
            raise _Cancelled()
        # A cancellation racing this boundary reserves at most this one request.
        self._boundary(run_id, deadline)
        started = self.clock()
        try:
            result = self.runtime.execute(copy.deepcopy(case))
        except EvaluationSkip as exc:
            self.store.finish_case(run_id, case_id, {'status': 'skipped', 'reason': exc.reason})
            return
        elapsed = (self.clock() - started) * 1000
        if not isinstance(result, dict):
            raise ValueError('Invalid evaluation result')
        if result.get('status') == 'skipped':
            self.store.finish_case(run_id, case_id, {'status': 'skipped', 'reason': skip_reason(result.get('reason'))})
            return
        if type(result.get('passed')) is not bool or 'actual' not in result:
            raise ValueError('Invalid evaluation result')
        outcome = {'status': 'passed' if result['passed'] else 'failed', 'actual': result['actual']}
        for key in ('reason', 'metrics'):
            if key in result:
                outcome[key] = result[key]
        # Measure execute only: preparation, availability checks and skips are
        # not samples of inference latency. Unknown/exception paths have no zero.
        if math.isfinite(elapsed) and elapsed >= 0:
            outcome['latency_ms'] = elapsed
        measured = result.get('metrics', {})
        measured = measured.get('wall_ms') if isinstance(measured, dict) else None
        if type(measured) in (int, float) and math.isfinite(measured) and measured >= 0:
            outcome['latency_ms'] = measured
        try:
            self._check(run_id, case['model'], snapshot, deadline, finishing_request=True)
        except EvaluationSkip:
            # Availability disappearing after an actual request is state drift.
            raise EvaluationFatal() from None
        self.store.finish_case(run_id, case_id, outcome)

    def _run(self, run_id, cases, deadline):
        self._boundary(run_id, deadline)
        snapshot = copy.deepcopy(self.runtime.prepare(self.get(run_id)['models']))
        start_public = public_snapshot(snapshot)
        self.store.snapshot(run_id, 'start', start_public)
        for case in cases:
            self._execute(run_id, case, snapshot, deadline)
        self._boundary(run_id, deadline)
        end = self.runtime.prepare(self.get(run_id)['models'])
        end_public = public_snapshot(end)
        self.store.snapshot(run_id, 'end', end_public)
        self._boundary(run_id, deadline, reserve_seconds=0)
        if _snapshot_state(end_public) != _snapshot_state(start_public):
            raise EvaluationFatal()
        # Cancellation may arrive during the final snapshot; finish rereads the
        # latest record under its lock, so completed cannot overwrite cancelling.
        self.store.finish(run_id, 'completed')

    def _work(self, run_id, cases):
        try:
            run = self.store.start(run_id)
            if run['status'] == 'cancelling':
                raise _Cancelled()
            deadline = self.clock() + self.deadline_seconds
            with file_lock(self.deploy / 'state' / 'modelctl.lock'):
                self._run(run_id, cases, deadline)
        except _Cancelled:
            self.store.finish(run_id, 'cancelled', reason='cancelled_before_execution')
        except _Stopped:
            pass
        except BaseException as exc:
            status = 'interrupted' if isinstance(exc, (TimeoutError, KeyboardInterrupt, SystemExit)) else 'failed'
            self.store.finish(run_id, status, exception_code(exc), 'execution_aborted_outcome_unknown')
        finally:
            with self._guard:
                self._active_id = None
                self.operation_lock.release()
