"""Durable observations; deployment events are never inferred from health alone."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from .audit import redact


class DiagnosticsStore:
    def __init__(self, path: Path): self.path = path

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS observations (service TEXT PRIMARY KEY, snapshot TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, at TEXT NOT NULL, category TEXT NOT NULL, body TEXT NOT NULL)')
                db.execute('CREATE INDEX IF NOT EXISTS events_time ON events(at)')
                yield db
        finally: db.close()

    def _event(self, db, identity, at, category, body):
        db.execute('INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?)',
                   (identity, at, category, json.dumps(body, ensure_ascii=False)))

    def observe(self, snapshot):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for item in snapshot['containers']:
                if not item.get('container_id'): continue
                service = item['service']; old = db.execute('SELECT snapshot FROM observations WHERE service=?', (service,)).fetchone()
                previous = json.loads(old[0]) if old else None
                saved = {key: item.get(key) for key in ('service', 'container_id', 'created_at', 'started_at', 'image', 'image_id', 'startup')}
                kind = 'first_observed' if previous is None else 'container_replaced' if previous['container_id'] != saved['container_id'] else 'process_restarted' if previous['started_at'] != saved['started_at'] else None
                if kind:
                    at = (saved['started_at'] if kind == 'process_restarted' else saved['created_at']) or snapshot['collected_at']
                    identity = hashlib.sha256((service + kind + str(saved['container_id']) + str(at)).encode()).hexdigest()
                    body = {'id': identity, 'kind': kind, 'service': service, 'at': at, 'observed_at': snapshot['collected_at'],
                            'image': saved['image'], 'image_id': saved['image_id'], 'previous_image_id': previous['image_id'] if previous else None,
                            'source': 'docker_inspect'}
                    self._event(db, identity, at, 'deployment', body)
                db.execute('INSERT INTO observations VALUES (?, ?) ON CONFLICT(service) DO UPDATE SET snapshot=excluded.snapshot',
                           (service, json.dumps(saved, ensure_ascii=False)))
            for fault in snapshot.get('faults', []):
                self._event(db, fault['id'], fault['at'], 'fault', {**fault, 'observed_at': snapshot['collected_at']})
            db.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY at DESC, id DESC LIMIT 1000)')

    def history(self):
        with self.connection() as db:
            return {category: [json.loads(row[0]) for row in db.execute('SELECT body FROM events WHERE category=? ORDER BY at DESC,id DESC LIMIT 100', (category,))]
                    for category in ('deployment', 'fault')}


def audit_history(store):
    # Select relevant operations before applying the limit, so batch activity does
    # not hide older recovery/configuration failures.
    with store.connection() as db:
        rows = db.execute("SELECT * FROM audit WHERE action IN ('restart','mode-switch','config-apply','device-recovery') OR status IN ('failed','unknown','interrupted') ORDER BY created_at DESC LIMIT 100").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try: detail = json.loads(item['detail'])
        except (ValueError, TypeError): detail = {}
        job = detail.get('job') if isinstance(detail, dict) else None
        if not isinstance(job, dict): job = {}
        result.append({key: item[key] for key in ('id', 'created_at', 'action', 'target', 'status', 'request_id', 'job_id')} |
                      {'error': str(job.get('error') or '')[:500], 'source': 'admin_audit'})
    return redact(result, store.secrets)
