"""Bounded host-side evidence capture for a single monitored NPU soak run.

Uses existing journals, Docker logs, procfs and an isolated tracefs instance.
Does not patch drivers, enable kprobes, change model settings or restart models.
The runner explicitly invokes containment only after a confirmed NPU fatal.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEVICE = ('houmo-copaw', 'houmo-qwen3-vl-8b', 'houmo-glm-ocr-0.9b',
          'houmo-bge-m3', 'houmo-reranker', 'houmo-qwen38-27b', 'houmo-cosyvoice3')
FATAL = re.compile(r'ERR_FATAL|SyncYieldErr|group sync timeout|exec timeout!|'
                   r'xh2a_ipu_sync_group failed|auto reset scheduled|'
                   r'indicator \d+[^\n]{0,100}not synchronized', re.I)
WARNING = re.compile(r'copy from user fail|hdma[^\n]{0,150}(?:fail|timeout|interrupt)|'
                     r'RV-PC|RV-ERR|StaticOpIdx|out of memory|oom-kill|Killed process', re.I)


def stamp() -> dict:
    return {'at': datetime.now(timezone.utc).isoformat(timespec='microseconds'),
            'monotonic_ns': time.monotonic_ns()}


def journal_timestamp(value: str | None = None) -> str:
    # Debian's systemd parser does not accept Docker's ISO offset/nanosecond form.
    moment = datetime.fromisoformat(value.replace('Z', '+00:00')) if value else datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def read_text(path: str | Path, limit: int = 65536) -> str | None:
    try:
        with Path(path).open(errors='replace') as stream:
            return stream.read(limit).strip()
    except OSError:
        return None


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{threading.get_ident()}.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def command(args: list[str], timeout: float = 10, limit: int = 2_000_000) -> dict:
    """Capture output on disk and bound returned data instead of accumulating a pipe in memory."""
    import tempfile
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(args, stdout=output, stderr=subprocess.STDOUT)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass  # A kernel D-state task may not reap until its syscall ends.
            code = -999
        size = output.tell()
        output.seek(0)
        text = output.read(limit).decode('utf-8', 'replace')
    return {'code': code, 'out': text, 'truncated': size > limit, 'bytes': size}


def inspect_containers(names: list[str]) -> list[dict]:
    result = command(['docker', 'inspect', *names], limit=4_000_000)
    if result['code'] or result['truncated']:
        raise RuntimeError('container inspect failed or exceeded bound')
    return json.loads(result['out'])


def safe_container(item: dict, clean=lambda text: text) -> dict:
    state = item['State']
    return {'name': item['Name'].lstrip('/'), 'id': item['Id'], 'image_id': item['Image'],
            'image_tag': item['Config']['Image'], 'pid': state['Pid'],
            'started_at': state['StartedAt'], 'finished_at': state['FinishedAt'],
            'status': state['Status'], 'health': state.get('Health', {}).get('Status'),
            'oom_killed': state.get('OOMKilled'), 'restart_count': item['RestartCount'],
            'command': json.loads(clean(json.dumps(item['Config'].get('Cmd', [])))),
            'restart_policy': item['HostConfig']['RestartPolicy'],
            'memory_limit': item['HostConfig'].get('Memory'),
            'mounts': [{'source': m['Source'], 'destination': m['Destination']} for m in item['Mounts']]}


class SegmentWriter:
    """Keep every segment; reaching the budget fails monitoring instead of rotating evidence away."""
    def __init__(self, root: Path, name: str, limit: int = 512 * 1024**2, segment: int = 32 * 1024**2):
        self.root, self.name, self.limit, self.segment = root, name, limit, segment
        self.total = self.current = self.index = 0
        self.stream = None
        self.last_flush = time.monotonic()
        self.lock = threading.Lock()

    def write(self, line: str) -> None:
        raw = line.encode('utf-8') + b'\n'
        with self.lock:
            if self.total + len(raw) > self.limit:
                raise RuntimeError(f'evidence budget reached: {self.name}')
            if self.stream is None or self.current + len(raw) > self.segment:
                if self.stream:
                    self.stream.flush(); os.fsync(self.stream.fileno()); self.stream.close()
                self.index += 1
                target = self.root / f'{self.name}-{self.index:04d}.ndjson'
                self.stream = target.open('xb')
                os.chmod(target, 0o600)
                self.current = 0
            self.stream.write(raw)
            self.current += len(raw); self.total += len(raw)
            if time.monotonic() - self.last_flush >= 1:
                self.stream.flush()
                self.last_flush = time.monotonic()

    def close(self) -> None:
        with self.lock:
            if self.stream:
                self.stream.flush(); os.fsync(self.stream.fileno()); self.stream.close()
                self.stream = None


class Monitor:
    def __init__(self, root: Path, containers: list[dict], secrets: list[str], *, smi_interval: float = 15):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=False)
        os.chmod(self.root, 0o700)
        self.secrets = [value for value in secrets if value]
        self.containers = containers
        self.names = [x['Name'].lstrip('/') for x in containers if x['State']['Running']]
        self.identity = {x['Name'].lstrip('/'): (x['Id'], x['State']['StartedAt'], x['RestartCount']) for x in containers}
        self.pids = [x['State']['Pid'] for x in containers if x['State']['Pid'] > 0]
        self.boot_id = read_text('/proc/sys/kernel/random/boot_id')
        self.abort = threading.Event(); self.closed = threading.Event()
        self.fault_lock = threading.RLock(); self.context_lock = threading.Lock()
        self.first_fault = None; self.hardware_fault = None
        self.context: dict = {'stage': 'monitor_setup', 'request_id': None}
        self.followers: dict[str, dict] = {}
        self.threads: list[threading.Thread] = []
        self.writers: dict[str, SegmentWriter] = {}
        self.event_writer = SegmentWriter(root, 'timeline', limit=128 * 1024**2)
        self.last_sample = {}; self.last_smi = {}; self.smi_interval = smi_interval
        self.trace_root: Path | None = None
        self.capabilities: dict = {'driver_kprobes': False, 'dma_channel_generation_trace': False,
                                   'limits': ['No driver STOP completion/IRQ generation tracing; kernel lacks kprobes.']}
        self.suspect_count = 0

    def clean(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, '[REDACTED]')
        return re.sub(r'(?i)Bearer\s+[^\s\"\']+', 'Bearer [REDACTED]', text)

    def save(self, name: str, value: Any) -> None:
        atomic_json(self.root / name, json.loads(self.clean(json.dumps(value, ensure_ascii=False))))

    def event(self, kind: str, **fields) -> None:
        self.event_writer.write(self.clean(json.dumps({'kind': kind, **fields, **stamp()}, ensure_ascii=False)))

    def phase(self, stage: str, **fields) -> None:
        with self.context_lock:
            self.context = {'stage': stage, **fields}
        self.event('phase', **self.context)
        if self.trace_root:
            with contextlib.suppress(OSError):
                (self.trace_root / 'trace_marker').write_text('copaw_soak ' + json.dumps(self.context) + '\n')

    def fail(self, kind: str, source: str, detail: str) -> None:
        with self.fault_lock:
            fault = {**stamp(), 'kind': kind, 'source': source, 'detail': self.clean(detail[:4000]),
                     'context': dict(self.context), 'last_sample': self.last_sample, 'last_smi': self.last_smi}
            if kind == 'npu_fatal' and self.hardware_fault is None:
                self.hardware_fault = fault
                self.save('first-npu-fault.json', fault)
            if self.first_fault is None:
                self.first_fault = fault
                self.abort.set()  # No new inference may start after this point.
                self.save('first-fault.json', fault)
                self.event('first_fault', fault=fault)

    def ingest(self, source: str, line: str) -> None:
        # A source line is already bounded by the follower's readline limit.
        line = self.clean(line.rstrip('\r\n'))
        try:
            if FATAL.search(line) and (source == 'kernel' or source in DEVICE):
                self.fail('npu_fatal', source, line)
            self.writers[source].write(json.dumps({**stamp(), 'line': line}, ensure_ascii=False))
            if WARNING.search(line) and self.suspect_count < 200:
                self.suspect_count += 1
                self.event('diagnostic_signal', source=source, line=line[:3000], context=dict(self.context))
            if source == 'docker_events':
                try:
                    item = json.loads(line)
                    name = item.get('Actor', {}).get('Attributes', {}).get('name')
                    action = item.get('Action', item.get('status'))
                    if name in self.names and action in {'start', 'stop', 'die', 'kill', 'destroy', 'oom', 'restart', 'pause'}:
                        self.fail('container_lifecycle_change', source, line)
                except ValueError:
                    self.fail('monitor_error', source, 'Docker event JSON could not be decoded')
        except OSError as exc:
            self.abort.set()
            self.fail('monitor_error', source, str(exc))
        except RuntimeError as exc:
            self.fail('monitor_error', source, str(exc))

    def follow(self, source: str, args: list[str], *, required: bool = True) -> None:
        self.writers[source] = SegmentWriter(self.root, source)
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        info = {'process': proc, 'required': required, 'lines': 0, 'started': stamp()}
        self.followers[source] = info

        def read_loop():
            try:
                while not self.closed.is_set():
                    raw = proc.stdout.readline(65537)
                    if not raw:
                        break
                    if len(raw) > 65536:
                        self.fail('monitor_error', source, 'source line exceeded 64 KiB; evidence incomplete')
                        break
                    info['lines'] += 1
                    self.ingest(source, raw.decode('utf-8', 'replace'))
                if required and not self.closed.is_set():
                    self.fail('monitor_error', source, 'log follower ended before the test completed')
            except Exception as exc:
                if not self.closed.is_set():
                    self.fail('monitor_error', source, str(exc))
        thread = threading.Thread(target=read_loop, name=f'capture-{source}', daemon=True)
        thread.start(); self.threads.append(thread)

    def start_trace(self) -> None:
        base = Path('/sys/kernel/tracing')
        events = ('power/device_pm_callback_start', 'power/device_pm_callback_end',
                  'power/suspend_resume', 'oom/mark_victim', 'sched/sched_process_exit')
        try:
            instance = base / 'instances' / ('copaw_' + self.root.name.replace('-', '_'))
            instance.mkdir()  # Never attach to or clear another tracing session.
            self.trace_root = instance
            (instance / 'tracing_on').write_text('0')
            (instance / 'buffer_size_kb').write_text('256')
            clocks = read_text(instance / 'trace_clock') or ''
            if 'mono' in clocks.split():
                (instance / 'trace_clock').write_text('mono')
            enabled = []
            for event in events:
                path = instance / 'events' / event
                if (path / 'enable').exists():
                    if event == 'sched/sched_process_exit':
                        (path / 'filter').write_text('comm == "llama-server" || comm == "hm_smi"')
                    (path / 'enable').write_text('1')
                    enabled.append(event)
            self.follow('kernel_trace', ['cat', str(instance / 'trace_pipe')], required=False)
            (instance / 'tracing_on').write_text('1')
            self.capabilities.update(trace_events=enabled, trace_clock=read_text(instance / 'trace_clock'),
                                     trace_instance=str(instance))
        except OSError as exc:
            self.capabilities['trace_error'] = str(exc)
        self.save('capture-capabilities.json', self.capabilities)

    def start(self) -> None:
        since = datetime.now(timezone.utc).isoformat(timespec='microseconds')
        journal_since = journal_timestamp()
        check = command(['journalctl', '-k', '-b', '-n', '1', '--no-pager', '-o', 'json'])
        if check['code']:
            raise RuntimeError('host kernel journal is not readable; run as root')
        self.follow('kernel', ['journalctl', '-k', '-b', '-f', '--since', journal_since, '-o', 'json', '--no-pager'])
        self.follow('services', ['journalctl', '-f', '--since', journal_since, '-o', 'json', '--no-pager',
                                 '-u', 'docker.service', '-u', 'houmo-ai.service', '-u', 'houmo-xh2-driver.service'])
        self.follow('docker_events', ['docker', 'events', '--since', since, '--filter', 'type=container', '--format', '{{json .}}'])
        for name in self.names:
            self.follow(name, ['docker', 'logs', '-f', '--timestamps', '--since', since, name])
        self.start_trace()
        for name, fn in [('host-samples', self.sample_loop), ('npu-samples', self.smi_loop)]:
            thread = threading.Thread(target=fn, name=name, daemon=True)
            thread.start(); self.threads.append(thread)
        time.sleep(.5)
        for source, item in self.followers.items():
            if item['required'] and item['process'].poll() is not None:
                self.fail('monitor_error', source, 'required follower failed during startup')
        self.event('monitor_started', boot_id=self.boot_id, capabilities=self.capabilities)

    def sample(self) -> dict:
        result = {**stamp(), 'boot_id': read_text('/proc/sys/kernel/random/boot_id'),
                  'loadavg': read_text('/proc/loadavg'), 'meminfo': read_text('/proc/meminfo'),
                  'pressure': {kind: read_text('/proc/pressure/' + kind) for kind in ('cpu', 'memory', 'io')},
                  'processes': [], 'context': dict(self.context)}
        for pid in self.pids:
            cg = read_text(f'/proc/{pid}/cgroup') or ''
            cgroup = next((line[3:] for line in cg.splitlines() if line.startswith('0::')), None)
            row = {'pid': pid, 'stat': read_text(f'/proc/{pid}/stat'), 'wchan': read_text(f'/proc/{pid}/wchan'),
                   'io': read_text(f'/proc/{pid}/io')}
            if cgroup:
                path = Path('/sys/fs/cgroup') / cgroup.lstrip('/')
                row['cgroup'] = {key: read_text(path / key) for key in ('memory.current', 'memory.peak', 'memory.events', 'cpu.stat', 'io.stat')}
            result['processes'].append(row)
        return result

    def sample_loop(self) -> None:
        writer = SegmentWriter(self.root, 'host-samples', limit=256 * 1024**2)
        self.writers['host-samples'] = writer
        index = 0
        try:
            while not self.closed.is_set():
                if shutil.disk_usage(self.root).free < 1024**3:
                    self.fail('monitor_error', 'disk', 'less than 1 GiB remains for evidence')
                self.last_sample = self.sample()
                writer.write(json.dumps(self.last_sample, ensure_ascii=False))
                if self.last_sample['boot_id'] != self.boot_id:
                    self.fail('boot_changed', 'host', 'host boot ID changed')
                if index % 8 == 0 and not self.abort.is_set():
                    current = inspect_containers(list(self.identity))
                    for item in current:
                        name = item['Name'].lstrip('/')
                        if (item['Id'], item['State']['StartedAt'], item['RestartCount']) != self.identity[name]:
                            self.fail('container_lifecycle_change', 'snapshot', name)
                index += 1
                self.closed.wait(2)
        except Exception as exc:
            if not self.closed.is_set():
                self.fail('monitor_error', 'host-samples', str(exc))

    def smi_loop(self) -> None:
        while not self.closed.is_set() and not self.abort.is_set():
            self.event('smi_begin')
            value = command(['hm_smi', '-a'], timeout=4, limit=100000)
            self.last_smi = {**stamp(), **value}
            self.event('smi_end', result=self.last_smi)
            if value['code'] or value['truncated']:
                self.capabilities['npu_telemetry_degraded'] = value['code']
                self.save('capture-capabilities.json', self.capabilities)
                # Do not repeatedly interrupt a stuck diagnostic ioctl.
                break
            self.closed.wait(self.smi_interval)

    def snapshot(self, label: str, *, stacks: bool = False) -> dict:
        result = {**stamp(), 'boot_id': self.boot_id, 'context': dict(self.context),
                  'host': self.sample(), 'interrupts': read_text('/proc/interrupts', 256000),
                  'vmstat': read_text('/proc/vmstat'), 'kernel_taint': read_text('/proc/sys/kernel/tainted')}
        if stacks:
            rows = []
            for pid in self.pids:
                directory = Path(f'/proc/{pid}/task')
                for task in list(directory.iterdir())[:128] if directory.exists() else []:
                    rows.append({'pid': pid, 'tid': task.name, 'comm': read_text(task/'comm'),
                                 'wchan': read_text(task/'wchan'), 'stack': read_text(task/'stack', 16000),
                                 'syscall': read_text(task/'syscall'), 'stat': read_text(task/'stat')})
            result['threads'] = rows
        try:
            result['containers'] = [safe_container(item, self.clean) for item in inspect_containers(list(self.identity))]
        except Exception as exc:
            result['inspect_error'] = str(exc)
        self.save(label + '.json', result)
        return result

    def contain_npu_fault(self) -> dict:
        if self.hardware_fault is None:
            return {'executed': False}
        # Capture waiting threads BEFORE stopping the affected shared-device group.
        self.snapshot('fault-before-stop', stacks=True)
        targets = [name for name in self.names if name in DEVICE]
        self.event('containment_begin', containers=targets)
        result = command(['docker', 'stop', '--time', '10', *targets], timeout=55, limit=10000)
        result['containers'] = targets
        self.save('containment.json', result)
        self.snapshot('fault-after-stop')
        return result

    def close(self, *, before_writer_close=None) -> None:
        self.closed.set()
        if self.trace_root:
            with contextlib.suppress(OSError):
                (self.trace_root/'tracing_on').write_text('0')
            self.save('trace-buffer-stats.json', {p.parent.name: read_text(p) for p in self.trace_root.glob('per_cpu/cpu*/stats')})
        for value in self.followers.values():
            proc = value['process']
            if proc.poll() is None:
                proc.terminate()
        for value in self.followers.values():
            proc = value['process']
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        for thread in self.threads:
            thread.join(timeout=14)
        for value in self.followers.values():
            if value['process'].stdout:
                value['process'].stdout.close()
        # The runner gets one last chance to contain a fatal received during
        # shutdown, while the evidence writers are still open.
        if before_writer_close:
            before_writer_close()
        for writer in self.writers.values():
            writer.close()
        self.event_writer.close()
        if self.trace_root:
            try:
                (self.trace_root/'events/enable').write_text('0')
                self.trace_root.rmdir()
            except OSError as exc:
                self.save('trace-cleanup-error.json', {'error': str(exc), 'instance': str(self.trace_root)})


def observed_status(status: dict, boot_id: str | None = None, *, now: float | None = None) -> dict:
    result = dict(status)
    if result.get('state') in {'running', 'preparing'}:
        current_boot = boot_id if boot_id is not None else read_text('/proc/sys/kernel/random/boot_id')
        if current_boot and result.get('boot_id') != current_boot:
            result.update(state='interrupted', reason='host_reboot', duration_completed=False)
        elif (time.time() if now is None else now) - result.get('heartbeat_epoch', 0) > 90:
            result.update(state='interrupted', reason='heartbeat_stale', duration_completed=False)
    return result
