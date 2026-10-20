"""Reuse CPU component recipes and health checks with GB10-owned containers."""
from __future__ import annotations
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace

from .core import ROOT, LABEL, atomic, exclusive, read, run

TITLES = {'mysql': 'MySQL', 'redis': 'Redis', 'paradedb': 'ParadeDB', 'neo4j': 'Neo4j',
          'minio': 'MinIO', 'milvus': 'Milvus', 'nginx': 'Nginx', 'etcd': 'etcd'}
PORTS = {'mysql': [3306], 'redis': [6379], 'paradedb': [5432], 'neo4j': [7474, 7687],
         'minio': [9000, 9001], 'milvus': [19530, 9091], 'nginx': [80], 'etcd': [2379, 2380]}


def name(component):
    if component not in TITLES:
        raise ValueError('未知的组件')
    return 'ai-platform-gb10-' + component


def inspect(component):
    result = run(['docker', 'inspect', name(component)], check=False)
    if result.returncode:
        run(['docker', 'info'], timeout=15)
        return None
    info = json.loads(result.stdout)[0]
    if info.get('Config', {}).get('Labels', {}).get(LABEL) != 'true':
        raise ValueError('同名组件不属于本安装器，未接管')
    return info


def initialize(bundle):
    sys.path.insert(0, str(bundle / 'platform/gateway-admin'))
    from host_ops import runtime, server
    from host_ops.core.components import Components, compose_spec, data_owner, escape_compose
    from host_ops.core.workflow import resolved_components

    base = ROOT / 'components'
    runtime.HOME = base
    runtime.STATE = ROOT / 'ops'
    base.mkdir(parents=True, exist_ok=True)
    lock = read(bundle / 'parent-metadata/catalog/workflow.json')
    recipes = {r['id']: r for r in lock['images']}
    recipes.update({r['id']: r for r in read(bundle / 'component-extras.json')['images']})
    connections = base / 'AI-Platform-1.1-connections.json'

    def command(args, **kwargs):
        values = [str(a) for a in args]
        if values[:2] == ['docker', 'exec']:
            for index in range(2, len(values)):
                if values[index] in TITLES:
                    values[index] = name(values[index])
                    break
        return run(values, **kwargs)

    class Manager(Components):
        def __init__(self, ids, supplied=None):
            self.settings = runtime.settings_for(ids, supplied)
            self.selected = resolved_components(self.settings)
            self.images = {k: recipes[k] for k in self.selected}
            self.data_root, self.config_root = base / 'data', base
            self.compose = base / 'compose.json'
            self.data_owners = {}
            self.flow = SimpleNamespace(run=command, settings={'components': self.settings})
            self.spec = compose_spec(lock['images'], self.settings, self.data_root)
            self.spec['name'] = 'ai-platform-gb10-components'
            if 'nginx' in self.selected:
                self.spec['services']['nginx'] = {'image': recipes['nginx']['config_id'], 'ports': ['80:80']}
            if 'etcd' in self.selected:
                self.spec['services']['etcd'] = {'image': recipes['etcd']['config_id'],
                    'ports': ['2379:2379', '2380:2380'], 'volumes': [str(self.data_root / 'etcd') + ':/etcd'],
                    'entrypoint': ['/usr/local/bin/etcd'], 'command': ['--data-dir=/etcd',
                    '--listen-client-urls=http://0.0.0.0:2379', '--advertise-client-urls=http://host.docker.internal:2379',
                    '--listen-peer-urls=http://0.0.0.0:2380']}
            for component, service in self.spec['services'].items():
                service.update(container_name=name(component), restart='unless-stopped')
                service.setdefault('labels', {}).update({LABEL: 'true', 'ai-platform.installer.component': component})

        def inspect(self, component):
            return inspect(component)

        def compatible(self, component, info):
            if info.get('Config', {}).get('Labels', {}).get(LABEL) != 'true':
                raise ValueError('组件不属于本安装器')
            if component in ('nginx', 'etcd'):
                if info['Image'] != recipes[component]['config_id']:
                    raise ValueError('组件镜像与本次发行不同')
                return
            return super().compatible(component, info)

        def healthy(self, component):
            if component == 'etcd':
                return self.etcd_healthy()
            if component == 'nginx':
                from urllib.request import urlopen
                with urlopen('http://127.0.0.1/', timeout=5) as response:
                    return response.status == 200
            return super().healthy(component)

        def preflight(self):
            super().preflight()
            for component in self.selected:
                info = self.inspect(component)
                if info:
                    self.compatible(component, info)
                    continue
                for port in PORTS[component]:
                    with socket.socket() as connection:
                        try:
                            connection.bind(('0.0.0.0', port))
                        except OSError:
                            raise ValueError(f'端口 {port} 已被原有服务占用，未接管或停止') from None

        def image(self, component):
            row = self.images[component]
            found = run(['docker', 'image', 'inspect', row['config_id']], check=False)
            if found.returncode:
                if not row.get('pinned'):
                    raise ValueError(component + ' 需要导入固定 ARM64 镜像：' + row['config_id'])
                run(['docker', 'pull', '--platform=linux/arm64', row['pinned']], timeout=1800)
                found = run(['docker', 'image', 'inspect', row['config_id']])
            info = json.loads(found.stdout)[0]
            if info['Id'] != row['config_id'] or info['Architecture'] != 'arm64' or info['RootFS']['Layers'] != row['rootfs_diff_ids']:
                raise ValueError('组件镜像摘要或架构不符')
            self.data_owners[component] = data_owner(self.flow, info, component)

        def install(self):
            self.preflight()
            for component in self.selected:
                self.image(component)
            if self.compose.exists():
                for component, service in read(self.compose).get('services', {}).items():
                    if component not in self.spec['services']:
                        if service.get('labels', {}).get(LABEL) != 'true':
                            raise ValueError('已有组件配置不属于 GB10 安装器')
                        # The stored Compose JSON already has escaped dollar signs.
                        def unescape(value):
                            if isinstance(value, str): return value.replace('$$', '$')
                            if isinstance(value, list): return [unescape(v) for v in value]
                            if isinstance(value, dict): return {k: unescape(v) for k, v in value.items()}
                            return value
                        self.spec['services'][component] = unescape(service)
            atomic(self.compose, escape_compose(self.spec))
            for component in self.selected:
                directory = self.data_root / component
                if directory.is_symlink():
                    raise ValueError('组件数据目录不能是链接')
                if not directory.exists():
                    directory.mkdir(parents=True, mode=0o700)
                    os.chown(directory, *self.data_owners[component])
                elif not self.inspect(component) and any(directory.iterdir()):
                    raise ValueError('发现未登记的旧数据，保留原目录并中止')
                run(['docker', 'compose', '-f', self.compose, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', component], timeout=180)
                deadline = time.monotonic() + 360
                while time.monotonic() < deadline:
                    try:
                        if self.healthy(component):
                            break
                    except Exception:
                        pass
                    time.sleep(2)
                else:
                    raise RuntimeError(component + ' 认证/健康检查未通过；保留数据供排查')
            previous = runtime.read_connections()
            previous.update(schema=1, host='127.0.0.1', container_host='host.docker.internal')
            users = {'mysql': 'root', 'redis': 'default', 'paradedb': 'paradedb', 'neo4j': 'neo4j', 'minio': self.settings.get('minio_user')}
            for component in self.selected:
                row = {'ports': PORTS[component], 'data': str(self.data_root / component), 'image': recipes[component].get('pinned') or recipes[component]['config_id']}
                if component in users:
                    row.update(username=users[component], password=self.settings[component + '_password'])
                if component == 'paradedb':
                    row['database'] = 'paradedb'
                previous['components'][component] = row
            previous['selected_components'] = list(dict.fromkeys(previous.get('selected_components', []) + self.selected))
            atomic(connections, previous)
            return {'status': 'installed', 'components': self.selected}

    def component_rows():
        rows = []
        for component, title in TITLES.items():
            info = inspect(component)
            running = bool(info and info['State']['Running'])
            healthy = None
            if running:
                try:
                    healthy = Manager([component]).healthy(component)
                except Exception:
                    healthy = False
            rows.append({'id': component, 'name': title, 'managed': bool(info), 'running': running, 'healthy': healthy,
                         'status': 'not_installed' if not info else ('healthy' if healthy else 'unhealthy') if running else 'stopped',
                         'image': info['Config']['Image'] if info else '', 'ports': PORTS[component]})
        return rows

    def plan(ids, supplied=None):
        instance = Manager(ids, supplied)
        instance.preflight()
        return {'valid': True, 'selected': instance.selected, 'dependencies': [i for i in instance.selected if i not in ids],
                'steps': ['检查端口与已有数据', '按摘要准备 ARM64 镜像', '部署 GB10 专属组件', '认证与健康检查']}

    def install(ids, supplied=None):
        with exclusive(ROOT / 'ops/components.lock'):
            plan(ids, supplied)
            instance = Manager(ids, supplied)
            atomic(ROOT / 'ops/credentials.json', instance.settings)
            return instance.install()

    def action(component, verb):
        if verb not in ('start', 'stop', 'restart'):
            raise ValueError('未知组件操作')
        if not inspect(component):
            raise ValueError('该组件尚未由 GB10 安装器部署')
        rows = {r['id']: r for r in component_rows()}
        if component in ('etcd', 'minio') and verb in ('stop', 'restart') and rows['milvus']['running']:
            raise ValueError('请先停止使用该依赖的 Milvus')
        if component == 'milvus' and verb in ('start', 'restart') and any(not rows[c]['running'] for c in ('etcd', 'minio')):
            raise ValueError('请先启动 etcd 和 MinIO')
        with exclusive(ROOT / 'ops/components.lock'):
            run(['docker', verb, name(component)], timeout=90)
        return {'id': component, 'action': verb, 'status': 'completed'}

    def logs(component):
        if not inspect(component):
            raise ValueError('组件尚未部署')
        result = run(['docker', 'logs', '--tail', '200', name(component)], check=False)
        return {'id': component, 'lines': (result.stdout + result.stderr)[-50000:].splitlines()}

    runtime.component_rows = component_rows
    runtime.plan, runtime.install, runtime.action, runtime.logs = plan, install, action, logs
    return runtime, server
