"""Root service restricted to the signed GB10 recipes and namespaced state."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
import hmac
import json
import os
from pathlib import Path
import socketserver
import threading
import time
import uuid

from .core import CONFIG, ROOT, Controller, atomic, read, run

SOCKET = Path('/run/ai-platform-gb10/control.sock')
WORK = ThreadPoolExecutor(max_workers=1)
LOCK = threading.Lock()
CONTROLLER = None
OPS = None


def job(mode):
    if not LOCK.acquire(blocking=False):
        raise ValueError('模型管理操作正在执行')
    value = {'id': uuid.uuid4().hex, 'kind': 'mode-switch', 'target': mode, 'status': 'queued',
             'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'error': None, 'result': None}
    path = ROOT / 'jobs' / (value['id'] + '.json')
    atomic(path, value, 0o644)

    def execute():
        try:
            value.update(status='running', started_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
            atomic(path, value, 0o644)

            def progress(update):
                value['progress'] = update
                atomic(path, value, 0o644)

            value['result'] = CONTROLLER.switch(mode, progress)
            value['status'] = 'succeeded'
        except Exception as exc:
            value.update(status='failed', error=str(exc)[:2000])
        finally:
            value['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            atomic(path, value, 0o644)
            LOCK.release()

    WORK.submit(execute)
    return value.copy()


def status():
    snapshot = CONTROLLER.snapshot()
    memory = {row.split(':')[0]: int(row.split()[1]) * 1024 for row in Path('/proc/meminfo').read_text().splitlines()}
    gpu = run(['nvidia-smi', '--query-gpu=name,temperature.gpu,utilization.gpu,power.draw', '--format=csv,noheader,nounits'], check=False)
    containers = []
    for key, name in [('gateway', 'ai-platform-gb10-platform'), (snapshot.get('mode', 'model'), 'ai-platform-gb10-model')]:
        info = CONTROLLER.inspect(name)
        containers.append({'service': key, 'name': name, 'state': 'running' if info and info['State']['Running'] else 'stopped',
                           'status': 'running' if info and info['State']['Running'] else 'stopped'})
    mode = snapshot.get('mode', 'loading') if snapshot.get('verified') else 'loading'
    return {'system': {'cpu_cores': os.cpu_count(), 'hostname': os.uname().nodename,
                       'memory_total_bytes': memory['MemTotal'], 'memory_used_bytes': memory['MemTotal']-memory['MemAvailable'],
                       'memory_percent': round(100 * (1-memory['MemAvailable']/memory['MemTotal']), 1)},
            'containers': containers, 'modelctl': {'configured_mode': mode, 'detected_mode': mode, 'healthy': snapshot['verified']},
            'platform': {'id': 'nvidia-gb10', 'name': 'NVIDIA GB10', 'allow_mode_switch': True, 'allow_restart': False,
                         'notice': '128GB 统一内存；120B 模型互斥加载，切换需数分钟。'},
            'gpu': {'raw': gpu.stdout.strip(), 'error': None if not gpu.returncode else 'NVIDIA 监控不可用'}, 'runtime': snapshot}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.handle_api()

    def do_POST(self):
        self.handle_api()

    def handle_api(self):
        code = 200
        try:
            expected = 'Bearer ' + CONTROLLER.config['agent_token']
            if not hmac.compare_digest(self.headers.get('Authorization', ''), expected):
                self.send_error(403)
                return
            size = int(self.headers.get('Content-Length', '0'))
            if size < 0 or size > 32768:
                self.send_error(413)
                return
            body = json.loads(self.rfile.read(size)) if size else {}
            if self.command == 'GET' and self.path == '/status':
                value = status()
            elif self.command == 'POST' and self.path == '/switch':
                if body.get('mode') not in CONTROLLER.models:
                    raise ValueError('未知的模型模式')
                value = job(body['mode'])
            elif self.command == 'POST' and self.path == '/logs':
                service = body.get('service')
                if service not in {'gateway', 'gpt', 'nemotron'}:
                    raise ValueError('未知的服务')
                count = max(20, min(1000, int(body.get('tail', 200))))
                name = 'ai-platform-gb10-platform' if service == 'gateway' else 'ai-platform-gb10-model'
                output = run(['docker', 'logs', '--tail', str(count), name], check=False)
                value = {'service': service, 'container': name, 'lines': (output.stdout + output.stderr)[-250000:].splitlines()[-count:]}
            elif self.command == 'GET' and self.path.startswith('/jobs/'):
                identity = self.path.removeprefix('/jobs/')
                if len(identity) != 32 or any(c not in '0123456789abcdef' for c in identity):
                    raise ValueError('无效任务 ID')
                value = read(ROOT / 'jobs' / (identity + '.json'))
            elif self.command == 'GET' and self.path == '/ops/overview':
                value = OPS[1].overview()
                value['hardware_profile'] = 'nvidia-gb10'
            elif self.command == 'POST' and self.path in ('/ops/components/plan', '/ops/components/install', '/ops/components/action', '/ops/components/logs'):
                runtime = OPS[0]
                if self.path.endswith('/logs'):
                    value = runtime.logs(body['id'])
                elif self.path.endswith('/action'):
                    value = runtime.action(body['id'], body['action'])
                else:
                    function = runtime.plan if self.path.endswith('/plan') else runtime.install
                    value = function(body['ids'], body.get('settings'))
            else:
                self.send_error(404)
                return
        except FileNotFoundError:
            code, value = 404, {'detail': '记录不存在'}
        except (ValueError, RuntimeError) as exc:
            code, value = 409, {'detail': str(exc)[:1000]}
        except Exception as exc:
            code, value = 503, {'detail': type(exc).__name__ + ': 宿主机操作失败'}
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main():
    global CONTROLLER, OPS
    config = read(CONFIG)
    CONTROLLER = Controller(config, read(Path(config['bundle']) / 'profile.json'))
    from .operations import initialize
    OPS = initialize(Path(config['bundle']))
    for path in (ROOT / 'jobs').glob('*.json'):
        value = read(path)
        if value.get('status') in ('running', 'queued'):
            value.update(status='unknown', error='控制服务曾中断，请检查当前模型状态后重试')
            atomic(path, value, 0o644)
    SOCKET.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    SOCKET.unlink(missing_ok=True)

    def watch():
        while True:
            try:
                CONTROLLER.snapshot()
            except Exception:
                atomic(CONTROLLER.state / 'safety.json', {'verified': False, 'heartbeat_epoch': time.time()}, 0o644)
            time.sleep(5)

    threading.Thread(target=watch, daemon=True).start()
    threading.Thread(target=OPS[1].watch, daemon=True).start()

    def restore():
        if (ROOT / 'install-in-progress').exists():
            return
        desired = ROOT / 'state/desired-mode'
        if not desired.exists() or CONTROLLER.snapshot().get('verified'):
            return
        mode = desired.read_text().strip()
        if mode not in CONTROLLER.models:
            return
        # Wait for the gateway's admission/drain API before a boot restoration.
        for attempt in range(60):
            try:
                from .core import http
                if http('http://127.0.0.1:17700/health/live').get('ok'):
                    job(mode)
                    return
            except Exception:
                pass
            time.sleep(2)

    threading.Thread(target=restore, daemon=True).start()
    with Server(str(SOCKET), Handler) as server:
        os.chmod(SOCKET, 0o600)
        server.serve_forever()


if __name__ == '__main__':
    main()
