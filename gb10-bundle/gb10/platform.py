"""Reuse the complete gateway/admin applications with a GB10 agent adapter."""
from __future__ import annotations
import asyncio
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

from .core import read

BUNDLE = Path(os.getenv('GB10_BUNDLE', '/bundle'))
PROFILE = read(BUNDLE / 'profile.json')
MODES = {m['mode']: m for m in PROFILE['models']}


class Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/run/ai-platform-gb10/control.sock')


def host(path, body=None, timeout=60):
    connection = Connection('localhost', timeout=timeout)
    try:
        data = json.dumps(body).encode() if body is not None else None
        connection.request('POST' if body is not None else 'GET', path, data,
                           {'Authorization': 'Bearer ' + os.environ['ADMIN_AGENT_TOKEN'], 'Content-Type': 'application/json'})
        response = connection.getresponse()
        value = json.loads(response.read(8 * 1024**2))
        if response.status >= 400:
            raise RuntimeError(value.get('detail', 'GB10 控制服务不可用'))
        return value
    finally:
        connection.close()


def transaction(mode, progress):
    value = host('/switch', {'mode': mode})
    while value['status'] in ('queued', 'running'):
        progress(value.get('progress', {'phase': 'loading', 'stage': '准备加载模型'}))
        time.sleep(2)
        value = host('/jobs/' + value['id'])
    if value['status'] != 'succeeded':
        raise RuntimeError(value.get('error') or '模型切换未完成')
    return value['result']


def agent_app():
    from fastapi import Depends, HTTPException
    from agent import main as owner
    from agent.configuration import RuntimeSettings, CONFIG_SCHEMA
    from agent.model_api import Selection
    globals()['Selection'] = Selection

    owner.VALID_MODES = frozenset(MODES)
    owner.MODE_SERVICES = {mode: {mode} for mode in MODES}
    owner.CONTAINERS = {'gateway': 'ai-platform-gb10-platform', **{m: 'ai-platform-gb10-model' for m in MODES}}
    owner._mode_switch_transaction = transaction
    owner._modelctl_status = lambda: host('/status')['modelctl']
    owner._system_metrics = lambda: host('/status')['system']
    owner._container_rows = lambda: host('/status')['containers']
    owner._startup_verification_state = lambda: None
    owner.RUNTIME_SAFETY = None
    owner.GATEWAY_DIR = Path('/platform')
    owner.MODEL_REGISTRY = Path('/platform/config/models.json')
    owner.HOUMO_DEPLOY_DIR = Path('/platform')

    async def status():
        return await asyncio.to_thread(host, '/status')

    async def configuration():
        raw = read('/platform/config/models.json')
        return {'services': raw.get('services', {}), 'schema': CONFIG_SCHEMA,
                'settings': RuntimeSettings().model_dump(), 'models': raw['models'],
                'hardware_profile': PROFILE['id']}

    async def logs(service: str, tail: int = 200):
        return await asyncio.to_thread(host, '/logs', {'service': service, 'tail': tail})

    async def catalog():
        snapshot = await asyncio.to_thread(host, '/status')
        mode = snapshot['modelctl']['configured_mode']
        recipes = []
        for row in PROFILE['models']:
            manifest = read(BUNDLE / 'models' / row['manifest'])
            total = sum(f['size'] for f in manifest['files'])
            recipes.append({'id': row['id'], 'capability': 'chat', 'device': 'NVIDIA GB10',
                'version': manifest['revision'], 'modes': [row['mode']], 'image': PROFILE['runtime']['image'],
                'download_bytes': total, 'cached_bytes': total, 'downloaded': True, 'installed': True,
                'enabled': True, 'status': 'installed', 'image_ready': True, 'mode_compatible': mode == row['mode'],
                'rollback_available': False, 'running': mode == row['mode'], 'sources': ['local-verified'],
                'last_probe': snapshot['runtime'].get('evidence') if mode == row['mode'] else None})
        return {'models': recipes, 'mode': mode, 'managed': True, 'storage_free_bytes': shutil.disk_usage('/platform').free, 'uploads': []}

    def selection(body):
        ids = {m['id']: m['mode'] for m in PROFILE['models']}
        if len(set(body.ids)) != len(body.ids) or any(m not in ids for m in body.ids):
            raise HTTPException(400, '未知或重复的模型')
        if body.activate and len(body.ids) != 1:
            raise HTTPException(409, '120B 模型互斥运行，一次只能激活一个')
        return ids

    async def plan(body: Selection):
        selection(body)
        return {'valid': True, 'errors': [], 'notices': ['复用已校验的本地权重，120B 模型互斥运行。'],
                'download_bytes': 0, 'required_free_bytes': 0, 'steps': ['确认权重校验记录', '按需切换模型', '实际对话检查']}

    async def action(action: str, body: Selection):
        ids = selection(body)
        if action == 'activate' or (action == 'install' and body.activate):
            if len(body.ids) != 1:
                raise HTTPException(409, '一次只能激活一个模型')
            return owner._start_job('model-activate', body.ids[0], lambda progress: transaction(ids[body.ids[0]], progress), with_progress=True)
        if action in ('install', 'download'):
            return owner._start_job('model-' + action, ','.join(body.ids), lambda: {
                'status': 'succeeded', 'models': body.ids, 'source': 'existing-verified-local-weights'})
        raise HTTPException(409, '本地复用的权重保留；此候选版使用运行模式切换，不删除或回滚原文件。')

    async def blocked():
        raise HTTPException(409, '此操作需要相应的 GB10 运行配方，请使用模型模式切换；不会执行 NPU 操作。')

    # Preserve the platform's authentication, audit, jobs, evaluations and supply
    # routes. Only replace hardware-specific operations; never expose old NPU commands.
    replacements = {'/v1/status': status, '/v1/config': configuration, '/v1/logs': logs,
                    '/v1/model-install': catalog, '/v1/model-install/plan': plan,
                    '/v1/model-install/actions/{action}': action,
                    '/v1/device-recovery': blocked, '/v1/startup-verification': blocked,
                    '/v1/restart/{service}': blocked, '/v1/config/apply': blocked,
                    '/v1/config/validate': blocked}
    from fastapi.routing import APIRoute
    for route in owner.app.routes:
        if isinstance(route, APIRoute) and route.path in replacements:
            endpoint = replacements[route.path]
            # Rebuild the dependency graph, retaining the existing token boundary.
            replacement = APIRoute(route.path, endpoint, methods=route.methods,
                                   dependencies=[Depends(owner.require_token)])
            index = owner.app.routes.index(route)
            owner.app.routes[index] = replacement
    return owner.app


def gateway_app():
    from app import main as owner
    from .safety import RuntimeSafety
    owner.RUNTIME_SAFETY = RuntimeSafety('/platform/state/safety.json')
    original = owner.model_pools
    owner.model_pools = lambda metadata: ['GB10'] if 'GB10' in str(metadata.get('device', '')) else original(metadata)
    return owner.app


def admin_app():
    from admin_app import main as owner
    owner.DEVICE_MODES = frozenset(MODES)
    return owner.app


def main():
    """Supervise all three existing ASGI applications in the CPU-only container."""
    environment = dict(os.environ, PYTHONPATH='/bundle:/bundle/platform/ai-model-gateway:/bundle/platform/gateway-admin:/bundle/python')
    processes = []
    for factory, address, port in [('gateway_app', '0.0.0.0', 17700), ('admin_app', '0.0.0.0', 17780), ('agent_app', '127.0.0.1', 17781)]:
        processes.append(subprocess.Popen([sys.executable, '-m', 'uvicorn', 'gb10.platform:' + factory, '--factory',
                                          '--host', address, '--port', str(port), '--no-access-log'], env=environment))
    try:
        while all(process.poll() is None for process in processes):
            time.sleep(2)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
    raise SystemExit(1)


if __name__ == '__main__':
    main()
