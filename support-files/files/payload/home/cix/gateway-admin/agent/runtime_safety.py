"""Persistent admission isolation for the shared Houmo device; no automatic recovery."""
from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .device_recovery import DEVICE
from .diagnostics import bounded_command
from .faults import fatal_record


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def atomic_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        # The gateway is UID 65534 and only mounts this directory read-only.
        os.chmod(temporary, 0o644)
        json.dump(value, stream, ensure_ascii=False)
        stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)


class RuntimeSafety:
    def __init__(self, deploy: Path, docker='/usr/bin/docker', *, command=bounded_command, boot_id=None):
        self.path = deploy/'state/runtime-safety.json'
        self.history = deploy/'state/runtime-incidents'
        self.docker, self.command = docker, command
        self.boot_id = boot_id or Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.lock = threading.RLock()
        self.scan_lock = threading.RLock()
        self.closed = threading.Event()
        self.thread = None
        self.cursors = {}
        self.secrets = []
        self.data = {'schema_version': 1, 'boot_id': self.boot_id, 'revision': 0,
                     'incident': None, 'maintenance': None, 'services': {}, 'monitor_error': None}
        try:
            old = json.loads(self.path.read_text(encoding='utf-8'))
            if (not isinstance(old,dict) or old.get('schema_version') != 1 or not isinstance(old.get('services'), dict)
                    or not isinstance(old.get('revision'),int) or isinstance(old.get('revision'),bool)
                    or not all(name in DEVICE and isinstance(row,dict) for name,row in old['services'].items())):
                raise ValueError('invalid state')
            self.data.update(old, boot_id=self.boot_id, maintenance=None)
            if old.get('boot_id') != self.boot_id or old.get('maintenance'):
                for row in self.data['services'].values(): row['verified'] = False
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            self.data['monitor_error'] = 'saved_state_unreadable'

    def clean(self, text):
        for secret in self.secrets:
            if secret: text = text.replace(secret, '[REDACTED]')
        return re.sub(r'(?i)Bearer\s+[^\s"\']+', 'Bearer [REDACTED]', text)[:1000]

    def publish(self):
        with self.lock:
            self.data.update(heartbeat_epoch=time.time(), updated_at=now())
            atomic_write(self.path, self.data)

    def snapshot(self):
        with self.lock: return copy.deepcopy(self.data)

    def mark_fault(self, source, detail, kind='npu_fatal'):
        with self.lock:
            self.data['revision'] += 1
            if self.data['incident'] is None:
                incident = {'id': uuid.uuid4().hex, 'at': now(), 'source': source,
                            'kind': kind, 'detail': self.clean(detail)}
                self.data['incident'] = incident
                # Close admission before slower archival work.
                self.publish()
                atomic_write(self.history/(incident['id']+'.json'), incident)
            for row in self.data['services'].values(): row['verified'] = False
            self.publish()

    def _command(self, *args, max_bytes=2_000_000):
        result = self.command([self.docker, *args], timeout=5, max_bytes=max_bytes)
        if result.returncode:
            raise RuntimeError('docker_capture_failed_or_truncated')
        return result.stdout

    def scan(self):
        # One finite, bounded window per service; no tail truncation and no
        # optimistic cursor advance when a window cannot be read completely.
        with self.scan_lock:
            try:
                cutoff = now()
                present = set(self._command('ps', '-a', '--format', '{{.Names}}').splitlines())
                names = [name for name in DEVICE if name in present]
                items = json.loads(self._command('inspect', *names)) if names else []
                seen = set()
                for item in items:
                    name = item['Name'].lstrip('/'); seen.add(name)
                    state = item['State']
                    identity = [item['Id'], state['StartedAt'], item.get('RestartCount', 0)]
                    for value in item['Config'].get('Env', []):
                        key, _, secret = value.partition('=')
                        if secret and any(word in key.upper() for word in ('KEY','TOKEN','SECRET','PASSWORD')):
                            if secret not in self.secrets: self.secrets.append(secret)
                    cmd = item['Config'].get('Cmd') or []
                    for i, arg in enumerate(cmd[:-1]):
                        if arg == '--api-key' and cmd[i+1] not in self.secrets: self.secrets.append(cmd[i+1])
                    with self.lock:
                        row = self.data['services'].get(name, {})
                        changed = row.get('identity') != identity
                        if changed:
                            row = {'identity': identity, 'verified': False}
                            self.cursors.pop(name, None)
                        row.update(running=bool(state['Running']), health=state.get('Health',{}).get('Status'),
                                   checked_at=cutoff)
                        self.data['services'][name] = row
                        if changed: self.publish()
                    if not state['Running']: continue
                    since = self.cursors.get(name, state['StartedAt'])
                    text = self._command('logs', '--timestamps', '--since', since, '--until', cutoff, name,
                                         max_bytes=8_000_000 if name not in self.cursors else 2_000_000)
                    for line in text.splitlines():
                        if fatal_record(line):
                            self.mark_fault(name, line)
                            break  # A fatal storm must not cause one fsync per repeated log line.
                    # Inclusive boundary may repeat one line; never skip one.
                    self.cursors[name] = cutoff
                with self.lock:
                    for name, row in self.data['services'].items():
                        if name not in seen: row.update(running=False, verified=False)
                    self.data['monitor_error'] = None
                    self.publish()
            except Exception:
                with self.lock:
                    self.data['monitor_error'] = 'runtime_log_capture_unavailable'
                    self.publish()
                raise

    def start(self):
        # Invalidate the previous heartbeat until retained logs have been read.
        self.data['monitor_error'] = 'checking_retained_logs'
        self.publish()
        def loop():
            while not self.closed.is_set():
                with contextlib.suppress(Exception): self.scan()
                self.closed.wait(2)
        self.thread = threading.Thread(target=loop, name='runtime-safety', daemon=True)
        self.thread.start()

    def close(self):
        self.closed.set()
        if self.thread: self.thread.join(timeout=45)
        with self.lock:
            self.data['monitor_error'] = 'runtime_monitor_stopped'
            self.publish()

    def begin_recovery(self):
        with self.scan_lock, self.lock:
            token = {'id': uuid.uuid4().hex, 'revision': self.data['revision']}
            self.data['maintenance'] = token
            self.publish()
            return dict(token)

    def verify_installation(self, name, identity, revision):
        """Admit one explicitly tested instance without clearing any device fault."""
        with self.scan_lock:
            self.scan()
            with self.lock:
                row = self.data['services'].get(name, {})
                if (self.data['revision'] != revision or self.data.get('incident')
                        or self.data.get('maintenance') or self.data.get('monitor_error')
                        or row.get('identity') != identity or not row.get('running')
                        or row.get('health') != 'healthy'):
                    raise RuntimeError('安装自测期间设备状态变化，保留隔离状态')
                row['verified'] = True
                self.publish()

    def finish_recovery(self, token, verified):
        with self.scan_lock:
            self.scan()  # Include every log through completion of the probes.
            with self.lock:
                if self.data['maintenance'] != token or self.data['revision'] != token['revision']:
                    raise RuntimeError('恢复期间出现新故障，保留隔离状态')
                running = {name: row for name, row in self.data['services'].items() if row.get('running')}
                if not running or set(running) != set(verified):
                    raise RuntimeError('仍有正在运行的同卡模型未经实际推理验证')
                for name, row in running.items():
                    if row['identity'] != verified[name] or row.get('health') != 'healthy':
                        raise RuntimeError('验证后实例发生变化或健康异常，保留隔离状态')
                for row in running.values(): row['verified'] = True
                incident = self.data['incident']
                if incident:
                    atomic_write(self.history/(incident['id']+'.json'),
                                 {**incident, 'resolved_at': now(), 'recovery_id': token['id']})
                self.data.update(incident=None, maintenance=None, last_verified_at=now())
                self.publish()

    def abort_recovery(self, token):
        with self.lock:
            if self.data['maintenance'] == token:
                self.data['maintenance'] = None
                self.mark_fault('recovery', '恢复未完成或实际推理未通过', 'recovery_failed')
