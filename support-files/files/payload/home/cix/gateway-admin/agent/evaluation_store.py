"""Small, durable evaluation history. No lifecycle state is stored or changed here."""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .model_store import now

TERMINAL = frozenset({'completed', 'failed', 'cancelled', 'interrupted'})
ACTIVE = frozenset({'queued', 'running', 'cancelling'})
CASE_TERMINAL = frozenset({'passed', 'failed', 'skipped', 'interrupted'})
MAX_RUNS = 1000
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024


class EvaluationConflict(ValueError):
    """A valid request conflicts with existing work or retained history."""
    status_code = 409
    status = 409


def identifier(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{32}', value) is None:
        raise ValueError('Evaluation id must be 32 lowercase hexadecimal characters')
    return value


def selection(value):
    if not isinstance(value, dict) or set(value) != {'id', 'models', 'suite', 'repeats'}:
        raise ValueError('Evaluation requires id, models, suite and repeats')
    identifier(value['id'])
    models = value['models']
    if (not isinstance(models, list) or not 1 <= len(models) <= 23
            or any(not isinstance(m, str) or re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}', m) is None for m in models)
            or len(set(models)) != len(models)):
        raise ValueError('Select between 1 and 23 distinct model ids')
    suite, repeats = value['suite'], value['repeats']
    if not isinstance(suite, str) or suite not in {'smoke', 'quality', 'repeat'}:
        raise ValueError('Unknown evaluation suite')
    if type(repeats) is not int or not 1 <= repeats <= 3 or (suite != 'repeat' and repeats != 1):
        raise ValueError('Invalid evaluation repeat count')
    return {**value, 'models': list(models)}


def same_selection(run, request):
    if any(run[key] != request[key] for key in ('id', 'models', 'suite', 'repeats')):
        raise EvaluationConflict('Evaluation id already belongs to different parameters')


def summary(cases):
    counts = {key: sum(c['status'] == key for c in cases) for key in ('passed', 'failed', 'skipped')}
    # Interrupted and in-flight cases are unknown, not failed or silently skipped.
    counts.update(total=len(cases), pending=len(cases) - sum(counts.values()))
    values = sorted(float(c['latency_ms']) for c in cases
                    if c['status'] in {'passed', 'failed'}
                    and type(c.get('latency_ms')) in (int, float)
                    and math.isfinite(c['latency_ms']) and c['latency_ms'] >= 0)
    def percentile(fraction):
        position = (len(values) - 1) * fraction
        lower, upper = math.floor(position), math.ceil(position)
        return values[lower] + (values[upper] - values[lower]) * (position - lower)
    counts['latency_ms'] = ({'count': len(values), 'mean': sum(values) / len(values),
                            'p50': percentile(.5), 'p95': percentile(.95),
                            'min': values[0], 'max': values[-1]} if values else
                           {'count': 0, 'mean': None, 'p50': None, 'p95': None, 'min': None, 'max': None})
    return counts


def refresh(run):
    run['summary'] = summary(run['cases'])
    counts = run['summary']
    if run['status'] in ACTIVE:
        run['verdict'] = 'pending'
    elif run['status'] == 'completed' and counts['failed']:
        run['verdict'] = 'failed'
    elif run['status'] == 'completed' and counts['total'] > 0 and counts['passed'] == counts['total']:
        run['verdict'] = 'passed'
    else:
        run['verdict'] = 'incomplete'


def encode(value):
    body = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    if len(body.encode('utf-8')) > MAX_DOCUMENT_BYTES:
        raise ValueError('Evaluation record exceeds storage limit')
    return body


class EvaluationStore:
    """Transactions always modify the latest record; callers never save stale runs.

    Constructing a store does not recover or execute work. The manager calls
    recover() once at startup. History is retained until explicitly managed by
    the owner; reaching 1,000 records refuses new ids without evicting old ones.
    """
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'evaluation-runs.db'
        self.lock = threading.RLock()
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS evaluation_runs (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL, document TEXT NOT NULL)''')

    @contextmanager
    def db(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=10)
            try:
                with db:
                    yield db
            finally:
                db.close()

    def _get(self, db, run_id):
        row = db.execute('SELECT document FROM evaluation_runs WHERE id=?', (identifier(run_id),)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return json.loads(row[0])

    def get(self, run_id):
        with self.db() as db:
            return self._get(db, run_id)

    def list(self, limit=30):
        if type(limit) is not int or limit < 1:
            raise ValueError('Evaluation history limit must be a positive integer')
        with self.db() as db:
            return [json.loads(row[0]) for row in db.execute(
                'SELECT document FROM evaluation_runs ORDER BY seq DESC LIMIT ?', (min(limit, 100),))]

    def create(self, request, cases, version):
        request = selection(request)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                old = self._get(db, request['id'])
            except KeyError:
                old = None
            if old is not None:
                same_selection(old, request)
                return old, False
            if db.execute('SELECT count(*) FROM evaluation_runs').fetchone()[0] >= MAX_RUNS:
                raise EvaluationConflict('Evaluation history is full (1000 runs); no history was deleted')
            run = {**request, 'status': 'queued', 'verdict': 'pending', 'created_at': now(),
                   'started_at': None, 'finished_at': None, 'version': version,
                   'cases': cases, 'snapshot': {}}
            refresh(run)
            body = encode(run)
            db.execute('INSERT INTO evaluation_runs(id,document) VALUES(?,?)', (run['id'], body))
            return json.loads(body), True

    def update(self, run_id, change):
        """Apply a narrow mutation under a transaction, with terminal/cancel guards."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            run = self._get(db, run_id)
            old_status = run['status']
            if old_status in TERMINAL:
                return run
            change(run)
            if old_status == 'cancelling' and run['status'] not in {'cancelling', 'cancelled', 'interrupted'}:
                run['status'] = 'cancelled' if run['status'] in TERMINAL else 'cancelling'
            refresh(run)
            body = encode(run)
            db.execute('UPDATE evaluation_runs SET document=? WHERE id=?', (body, run_id))
            return json.loads(body)

    def start(self, run_id):
        def change(run):
            if run['status'] == 'queued':
                run.update(status='running', started_at=now())
        return self.update(run_id, change)

    def snapshot(self, run_id, phase, value):
        return self.update(run_id, lambda run: run['snapshot'].__setitem__(phase, value))

    def start_case(self, run_id, case_id):
        def change(run):
            if run['status'] != 'running':
                return
            case = next(c for c in run['cases'] if c['id'] == case_id)
            if case['status'] == 'pending':
                case['status'] = 'running'
        return self.update(run_id, change)

    def finish_case(self, run_id, case_id, result):
        def change(run):
            case = next(c for c in run['cases'] if c['id'] == case_id)
            if case['status'] in {'pending', 'running'}:
                if result.get('status') not in CASE_TERMINAL:
                    raise ValueError('Invalid evaluation case outcome')
                case.update({key: value for key, value in result.items()
                             if key in {'status', 'reason', 'actual', 'latency_ms', 'metrics'}})
        return self.update(run_id, change)

    def cancel(self, run_id):
        return self.update(run_id, lambda run: run.update(status='cancelling'))

    def finish(self, run_id, status, error=None, reason=None):
        if status not in TERMINAL:
            raise ValueError('Invalid terminal evaluation status')
        def change(run):
            run.update(status=status, finished_at=now())
            if error:
                run['error'] = error
            for case in run['cases']:
                if case['status'] == 'running':
                    case.update(status='interrupted', reason=reason or 'execution_outcome_unknown')
                elif case['status'] == 'pending' and reason:
                    case['reason'] = reason
        return self.update(run_id, change)

    def recover(self):
        """A restart never retries requests whose outcome might be unknown."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT id,document FROM evaluation_runs').fetchall()
            for run_id, document in rows:
                run = json.loads(document)
                if run['status'] not in ACTIVE:
                    continue
                run.update(status='interrupted', finished_at=now(), error='EvaluationRestarted')
                for case in run['cases']:
                    if case['status'] == 'running':
                        case.update(status='interrupted', reason='restart_outcome_unknown')
                    elif case['status'] == 'pending':
                        case['reason'] = 'restart_before_execution'
                refresh(run)
                db.execute('UPDATE evaluation_runs SET document=? WHERE id=?', (encode(run), run_id))
