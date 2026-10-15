"""Authenticated Unix-socket API. The browser never talks to the host directly."""
from __future__ import annotations
import hashlib
import hmac
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import socketserver
import sqlite3
import threading
import time

from . import runtime
from .core.workflow import mqtt_connected

SOCKET = Path('/run/modeldeck-ops/ops.sock')
LOCK = threading.Lock()


def token():
    for line in (runtime.HOME/'gateway-admin/.env').read_text().splitlines():
        if line.startswith('ADMIN_AGENT_TOKEN='):
            return line.split('=', 1)[1].strip().strip("'\"")
    raise RuntimeError('缺少本机代理凭据')


def scalar(path, names):
    if not path.is_file() or path.is_symlink(): return ''
    import re
    for line in path.read_text().splitlines():
        match = re.match(r'\s*([A-Za-z_][A-Za-z_0-9]*)\s*:\s*(.*?)\s*$', line)
        if match and match[1] in names:
            return match[2].strip("'\"")[:256]
    return ''


def probe_status():
    active = runtime.run(['systemctl', 'is-active', '--quiet', 'data-probe'], check=False).returncode == 0
    registration = Path('/etc/data-probe/registration.yaml')
    registered = registration.is_file() and not registration.is_symlink() and registration.stat().st_size > 0
    invocation = runtime.run(['systemctl', 'show', 'data-probe', '-p', 'InvocationID', '--value'], check=False).stdout.strip()
    connected = None
    import re
    if active and re.fullmatch('[0-9a-f]{32}', invocation):
        log = runtime.run(['journalctl', '_SYSTEMD_INVOCATION_ID='+invocation, '--no-pager', '--output=cat', '--lines=2000',
                           '--grep=MQTT connected to|MQTT disconnected|MQTT connection lost|MQTT connect failed'], check=False)
        if log.returncode == 0 and log.stdout:
            connected = mqtt_connected(log.stdout)
    if not active: connected = False
    return {'running': active, 'registered': registered, 'mqtt_connected': connected,
            'device_id': scalar(registration, {'device_id', 'deviceId', 'box_id', 'boxId'}),
            'platform': scalar(Path('/etc/data-probe/probe.yaml'), {'url'}),
            'checked_at': time.time(), 'last_report_at': None}


def metrics():
    def cpu():
        values = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:])); return sum(values), sum(values[3:5])
    first = cpu(); time.sleep(.1); second = cpu()
    memory = {line.split(':')[0]: int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines()}
    disks = []
    import shutil
    for path in ('/', '/home/cix', '/data'):
        if Path(path).exists():
            total, used, free = shutil.disk_usage(path)
            disks.append({'path': path, 'total': total, 'used': used, 'free': free, 'percent': round(used/max(total, 1)*100, 1)})
    temperatures = []
    for p in Path('/sys/class/thermal').glob('thermal_zone*/temp'):
        try: temperatures.append(float(p.read_text())/1000)
        except (ValueError, OSError): pass
    return {'at': time.time(), 'cpu_percent': round(100*(1-(second[1]-first[1])/max(second[0]-first[0], 1)), 1),
            'memory_percent': round(100*(1-memory.get('MemAvailable', 0)/max(memory.get('MemTotal', 1), 1)), 1),
            'memory_total': memory.get('MemTotal', 0), 'disks': disks,
            'temperature_c': max(temperatures) if temperatures else None,
            'uptime_seconds': float(Path('/proc/uptime').read_text().split()[0]), 'hostname': os.uname().nodename}


@contextmanager
def database():
    runtime.STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(runtime.STATE/'monitor.sqlite3', timeout=15)
    try:
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS metrics(at REAL PRIMARY KEY, body TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS alerts(key TEXT PRIMARY KEY, message TEXT, started REAL, resolved REAL)')
            yield db
    finally:
        db.close()


def sample():
    current = metrics(); problems = {}
    for key, label, limit in [('cpu_percent', 'CPU 使用率偏高', 95), ('memory_percent', '内存使用率偏高', 90)]:
        if current[key] >= limit: problems[key] = label
    for disk in current['disks']:
        if disk['percent'] >= 90: problems['disk:'+disk['path']] = disk['path']+' 磁盘空间不足'
    probe = probe_status()
    if not probe['running']: problems['probe'] = '探针服务未运行'
    elif not probe['registered']: problems['probe'] = '探针尚未完成设备注册'
    elif probe['mqtt_connected'] is False: problems['mqtt'] = '探针 MQTT 连接已断开'
    for row in runtime.component_rows():
        if row['status'] == 'unhealthy': problems['component:'+row['id']] = row['name']+' 健康检查失败'
    with database() as db:
        db.execute('INSERT OR REPLACE INTO metrics VALUES(?,?)', (current['at'], json.dumps(current)))
        db.execute('DELETE FROM metrics WHERE at<?', (time.time()-7*86400,))
        active = {row[0] for row in db.execute('SELECT key FROM alerts WHERE resolved IS NULL')}
        for key in active-problems.keys(): db.execute('UPDATE alerts SET resolved=? WHERE key=?', (time.time(), key))
        for key, message in problems.items():
            if key not in active:
                db.execute('INSERT OR REPLACE INTO alerts VALUES(?,?,?,NULL)', (key, message, time.time()))
    return current, probe


def overview():
    current = metrics(); probe = probe_status()
    with database() as db:
        history = [json.loads(row[0]) for row in db.execute('SELECT body FROM metrics ORDER BY at DESC LIMIT 2880')][::-1]
        alerts = [dict(zip(['id', 'message', 'started_at', 'resolved_at'], row)) for row in
                  db.execute('SELECT key,message,started,resolved FROM alerts ORDER BY started DESC LIMIT 100')]
    return {'version': '1.2.0', 'device': current, 'probe': probe, 'components': runtime.component_rows(),
            'history': history[::max(1, len(history)//240)], 'alerts': alerts}


def watch():
    while True:
        try: sample()
        except Exception: pass
        time.sleep(30)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def do_GET(self): self.handle_request()
    def do_POST(self): self.handle_request()

    def handle_request(self):
        status = 200
        try:
            expected = token()
            if not expected or not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer '+expected):
                self.send_error(403); return
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 <= size <= 32768: self.send_error(413); return
            body = json.loads(self.rfile.read(size)) if size else {}
            if self.command == 'GET' and self.path == '/overview': result = overview()
            elif self.command == 'POST' and self.path == '/components/logs': result = runtime.logs(body['id'])
            elif self.command == 'POST' and self.path in ('/components/plan', '/components/install', '/components/action'):
                if not LOCK.acquire(blocking=False): raise ValueError('另一个宿主机运维操作正在执行')
                try:
                    if self.path.endswith('/action'): result = runtime.action(body['id'], body['action'])
                    else:
                        fn = runtime.plan if self.path.endswith('/plan') else runtime.install
                        result = fn(body['ids'], body.get('settings'))
                finally: LOCK.release()
            else: self.send_error(404); return
        except (ValueError, KeyError, RuntimeError) as exc:
            status = 409; result = {'detail': str(exc)[:500]}
        except Exception:
            status = 503; result = {'detail': '宿主机操作未完成，请检查服务和设备端日志'}
        data = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers()
        try: self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError): pass


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main():
    token()
    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    if SOCKET.exists(): SOCKET.unlink()
    with Server(str(SOCKET), Handler) as server:
        os.chmod(SOCKET, 0o600)
        threading.Thread(target=watch, daemon=True).start()
        server.serve_forever()


if __name__ == '__main__': main()
