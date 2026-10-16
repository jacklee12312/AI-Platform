"""Pinned middleware deployment with authenticated health gates and data preservation."""
from __future__ import annotations
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import urllib.request

from .common import atomic_json, atomic_text, no_symlink_parents, sha256
from .workflow import requested_components, resolved_components

PORTS = {'mysql': [3306], 'redis': [6379], 'paradedb': [5432],
         'neo4j': [7474, 7687], 'minio': [9000, 9001], 'milvus': [19530, 9091]}
MOUNTS = {'mysql': '/var/lib/mysql', 'redis': '/data', 'paradedb': '/var/lib/postgresql',
          'neo4j': '/data', 'minio': '/data', 'milvus': '/var/lib/milvus'}


def http(url, *, headers=None, data=None):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, headers=headers or {}, data=data), timeout=5) as response:
        return response.status, response.read(1024 * 1024)


def check_ports(ports):
    for port in ports:
        try:
            with socket.socket() as connection:
                connection.bind(('0.0.0.0', port))
        except OSError:
            raise RuntimeError('端口 ' + str(port) + ' 被其他程序占用，不停止或覆盖') from None


def compose_spec(images, settings, data_root=Path('/data')):
    services = {}
    selected = resolved_components(settings)
    for row in images:
        name = row['id']
        if name not in selected:
            continue
        services[name] = {'image': row['config_id'], 'container_name': name, 'restart': 'always',
                          'labels': {'ai-platform.installer.component': name, 'ai-platform.installer.version': '1.1'},
                          'ports': [str(p) + ':' + str(p) for p in PORTS[name]],
                          'volumes': [str(data_root / name) + ':' + MOUNTS[name]],
                          'environment': {}}
    if 'mysql' in services:
        services['mysql']['environment'] = {'MYSQL_ROOT_PASSWORD': settings['mysql_password'], 'MYSQL_ROOT_HOST': '%'}
    if 'redis' in services:
        services['redis']['command'] = ['redis-server', '--protected-mode', 'yes', '--appendonly', 'yes',
                                    '--requirepass', settings['redis_password']]
    if 'paradedb' in services:
        services['paradedb']['environment'] = {'POSTGRES_USER': 'paradedb', 'POSTGRES_DB': 'paradedb',
                                          'POSTGRES_PASSWORD': settings['paradedb_password']}
    if 'neo4j' in services:
        services['neo4j']['environment'] = {'NEO4J_AUTH': 'neo4j/' + settings['neo4j_password']}
    if 'minio' in services:
        services['minio']['environment'] = {'MINIO_ROOT_USER': settings['minio_user'],
                                       'MINIO_ROOT_PASSWORD': settings['minio_password']}
        services['minio']['command'] = ['server', '/data', '--console-address', ':9001']
    if 'milvus' in services:
        services['milvus']['extra_hosts'] = ['host.docker.internal:host-gateway']
        services['milvus']['environment'] = {'ETCD_ENDPOINTS': 'host.docker.internal:2379',
                                        'MINIO_ADDRESS': 'host.docker.internal:9000',
                                        'MINIO_ACCESS_KEY_ID': settings['minio_user'],
                                        'MINIO_SECRET_ACCESS_KEY': settings['minio_password']}
        services['milvus']['command'] = ['milvus', 'run', 'standalone']
    return {'name': 'ai-platform-components', 'services': services}


def escape_compose(value):
    if isinstance(value, str):
        return value.replace('$', '$$')
    if isinstance(value, list):
        return [escape_compose(x) for x in value]
    if isinstance(value, dict):
        return {k: escape_compose(v) for k, v in value.items()}
    return value


def minio_authorization(user, password, now=None):
    now = now or datetime.now(timezone.utc)
    timestamp = now.strftime('%Y%m%dT%H%M%SZ');day = now.strftime('%Y%m%d')
    payload = hashlib.sha256(b'').hexdigest()
    headers = 'host:127.0.0.1:9000\nx-amz-content-sha256:' + payload + '\nx-amz-date:' + timestamp + '\n'
    signed = 'host;x-amz-content-sha256;x-amz-date'
    canonical = 'GET\n/\n\n' + headers + '\n' + signed + '\n' + payload
    scope = day + '/us-east-1/s3/aws4_request'
    string = 'AWS4-HMAC-SHA256\n' + timestamp + '\n' + scope + '\n' + hashlib.sha256(canonical.encode()).hexdigest()
    key = ('AWS4' + password).encode()
    for part in [day, 'us-east-1', 's3', 'aws4_request']:
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, string.encode(), hashlib.sha256).hexdigest()
    return {'x-amz-content-sha256': payload, 'x-amz-date': timestamp,
            'Authorization': 'AWS4-HMAC-SHA256 Credential=' + user + '/' + scope
            + ', SignedHeaders=' + signed + ', Signature=' + signature}


class Components:
    def __init__(self, workflow, data_root=Path('/data'), config_root=Path('/etc/ai-platform-components')):
        self.flow = workflow;self.settings = workflow.settings['components']
        self.selected = resolved_components(self.settings)
        self.images = {r['id']: r for r in workflow.lock['images'] if r['id'] in self.selected}
        self.data_root = Path(data_root);self.config_root = Path(config_root)
        self.spec = compose_spec(workflow.lock['images'], self.settings, self.data_root)
        self.compose = self.config_root / 'compose.json'

    def inspect(self, name):
        result = self.flow.run(['docker', 'container', 'inspect', name], check=False)
        if result.returncode:
            # A failed daemon connection is not evidence that a named container is absent.
            self.flow.run(['docker', 'info'], timeout=30)
            return None
        return json.loads(result.stdout)[0]

    def compatible(self, name, info):
        if info.get('Image') not in {self.images[name]['config_id'], self.images[name]['manifest_digest']}:
            raise RuntimeError('已有 ' + name + ' 容器镜像与锁定版本不符，不接管')
        mounts = [r for r in info.get('Mounts', []) if r.get('Destination') == MOUNTS[name]]
        if len(mounts) != 1 or mounts[0].get('Type') != 'bind' or mounts[0].get('Source') != str(self.data_root / name):
            raise RuntimeError('已有 ' + name + ' 数据挂载与安装要求不同，不接管')
        bindings = info.get('HostConfig', {}).get('PortBindings') or {}
        for port in PORTS[name]:
            if not any(str(x.get('HostPort')) == str(port) and x.get('HostIp', '') in ('', '0.0.0.0', '::')
                       for x in bindings.get(str(port) + '/tcp', [])):
                raise RuntimeError('已有 ' + name + ' 端口映射不兼容')
        if info.get('HostConfig', {}).get('RestartPolicy', {}).get('Name') not in ('always', 'unless-stopped'):
            raise RuntimeError('已有 ' + name + ' 未配置兼容的开机重启策略')
        if name == 'milvus':
            actual = dict(x.split('=', 1) for x in info.get('Config', {}).get('Env', []) if '=' in x)
            expected = self.spec['services'][name]['environment']
            if any(actual.get(k) != v for k, v in expected.items()):
                raise RuntimeError('已有 Milvus 的 etcd/MinIO 地址或凭据不一致，不自动修改')

    def preflight(self):
        owned = self.flow.owner.state.setdefault('component_data_owned', [])
        for name in self.images:
            target = self.data_root / name;no_symlink_parents(target, Path('/'))
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise RuntimeError('组件数据目录不能是链接或文件：' + str(target))
            existing = self.inspect(name)
            if existing:
                self.compatible(name, existing)
            else:
                check_ports(PORTS[name])
                if target.exists() and any(target.iterdir()) and name not in owned:
                    raise RuntimeError('检测到已有 ' + name + ' 数据但无对应容器，不自动初始化或重置')

    def host_services(self):
        names = [name for name in self.flow.lock['component_packages']
                 if ('nginx' if name == 'nginx' else 'etcd') in self.selected]
        existing = {}
        for name, executable, expected, port in [('nginx', 'nginx', '1.22.1', 80), ('etcd', 'etcd', '3.4.23', 2379)]:
            if name not in self.selected:
                continue
            version = self.flow.run([executable, '-v' if name == 'nginx' else '--version'], check=False) if shutil.which(executable) else None
            if version is not None:
                if expected not in version.stdout + version.stderr:
                    raise RuntimeError('已有 ' + name + ' 版本不兼容，不升级或覆盖')
                existing[name] = True
            else:
                check_ports([port] if name == 'nginx' else [2379, 2380])
                existing[name] = False
        missing = [name for name in names if not existing['nginx' if name == 'nginx' else 'etcd']]
        if 'etcd' in existing and not existing['etcd']:
            data=Path('/var/lib/etcd')
            if data.exists() and any(data.iterdir()) and not self.flow.owner.state.get('etcd_owned'):
                raise RuntimeError('发现已有 etcd 数据，不自动重新初始化')
            self.flow.owner.state['etcd_owned']=True;self.flow.owner.update(force=True)
        if existing.get('etcd') and not shutil.which('etcdctl'):missing.append('etcd-client')
        if missing:
            self.flow.host_packages(missing)
        if 'etcd' in existing and (not existing['etcd'] or self.flow.owner.state.get('etcd_owned')):
            override = Path('/etc/systemd/system/etcd.service.d/ai-platform-1.1.conf')
            no_symlink_parents(override, Path('/'))
            text = '[Service]\nEnvironment="ETCD_UNSUPPORTED_ARCH=arm64"\nEnvironment="ETCD_LISTEN_CLIENT_URLS=http://0.0.0.0:2379"\nEnvironment="ETCD_ADVERTISE_CLIENT_URLS=http://127.0.0.1:2379"\nEnvironment="ETCD_DATA_DIR=/var/lib/etcd"\n'
            if override.is_symlink() or (override.exists() and override.read_text() != text):
                raise RuntimeError('已有 etcd 前置配置冲突，不覆盖')
            self.flow.owner.state['etcd_owned'] = True;self.flow.owner.update(force=True)
            atomic_text(override,text,0o644)
            self.flow.run(['systemctl', 'daemon-reload'])
            self.flow.run(['systemctl', 'restart', 'etcd'])
        if 'nginx' in existing:
            self.flow.run(['nginx', '-t'])
            self.flow.run(['systemctl', 'enable', '--now', 'nginx'])
            self.flow.wait(self.nginx_healthy, 'Nginx 服务与 80 端口检查')
        if 'etcd' in existing:
            self.flow.run(['systemctl', 'enable', '--now', 'etcd'])
            self.flow.wait(self.etcd_healthy, 'etcd 健康检查')
        if existing:
            self.flow.owner.event('已选主机组件服务检查通过：' + '、'.join(existing))

    def etcd_healthy(self):
        status, body = http('http://127.0.0.1:2379/health')
        return status == 200 and json.loads(body).get('health') in (True, 'true')

    def nginx_healthy(self):
        active=self.flow.run(['systemctl','is-active','--quiet','nginx'],check=False)
        if active.returncode:return False
        if self.flow.run(['nginx','-t'],check=False).returncode:return False
        with socket.create_connection(('127.0.0.1',80),timeout=4):return True

    def image(self, name):
        row = self.images[name]
        current = self.flow.run(['docker', 'image', 'inspect', row['config_id']], check=False)
        if current.returncode:
            # A local archive is always rehashed before loading. If unavailable, pull the exact platform manifest.
            from artifacts import local_candidates, source_config
            config = source_config(self.flow.owner.bundle, __import__('common').read_json(self.flow.request_path)['sources'])
            resource = dict(row['archive'], category='images', models=[])
            candidates = local_candidates(self.flow.owner.bundle, config, resource)
            if candidates:
                source = self.flow.asset(row['archive'])
                old = self.flow.run(['docker', 'image', 'inspect', row['image']], check=False)
                if old.returncode == 0 and json.loads(old.stdout)[0]['Id'] != row['config_id']:
                    raise RuntimeError('已有同名组件镜像内容不同，不覆盖：' + name)
                self.flow.run(['docker', 'load', '-i', source], timeout=900)
            else:
                if config['mode'] == 'local-only':
                    raise RuntimeError('本地组件镜像缺失：' + name)
                self.flow.owner.event('正在联网拉取所选组件 ' + name + '（固定 ARM64 摘要，可重新运行继续）')
                self.flow.run(['docker', 'pull', '--platform=linux/arm64', row['pinned']], timeout=1800)
            current = self.flow.run(['docker', 'image', 'inspect', row['config_id']])
        info = json.loads(current.stdout)[0]
        if info['Id'] != row['config_id'] or info['Architecture'] != 'arm64' or info['Os'] != 'linux' or info['RootFS']['Layers'] != row['rootfs_diff_ids']:
            raise RuntimeError('组件镜像校验失败：' + name)

    def healthy(self, name):
        info = self.inspect(name)
        if not info or not info.get('State', {}).get('Running'):
            return False
        self.compatible(name, info)
        if name in ('mysql', 'redis', 'paradedb'):
            variable, command, expected = {
                'mysql': ('MYSQL_PWD', ['mysql', '--protocol=TCP', '--host=127.0.0.1', '-uroot', '--batch', '--skip-column-names', '-e', 'SELECT 1'], '1'),
                'redis': ('REDISCLI_AUTH', ['redis-cli', '--no-auth-warning', 'PING'], 'PONG'),
                'paradedb': ('PGPASSWORD', ['psql', '-h', '127.0.0.1', '-U', 'paradedb', '-d', 'paradedb', '-Atqc', 'SELECT 1'], '1')
            }[name]
            environment = dict(os.environ);environment[variable] = self.settings[name + '_password']
            result = self.flow.run(['docker', 'exec', '--env', variable, name, *command], env=environment, check=False, timeout=12)
            return result.returncode == 0 and result.stdout.strip() == expected
        if name == 'neo4j':
            authorization = base64.b64encode(('neo4j:' + self.settings['neo4j_password']).encode()).decode()
            status, body = http('http://127.0.0.1:7474/db/neo4j/tx/commit', headers={
                'Authorization': 'Basic ' + authorization, 'Content-Type': 'application/json'},
                data=b'{"statements":[{"statement":"RETURN 1"}]}')
            response = json.loads(body)
            return status == 200 and response.get('errors') == [] and bool(response.get('results'))
        if name == 'minio':
            status, body = http('http://127.0.0.1:9000/', headers=minio_authorization(
                self.settings['minio_user'], self.settings['minio_password']))
            return status == 200 and b'ListAllMyBucketsResult' in body
        if name == 'milvus':
            status, body = http('http://127.0.0.1:9091/healthz')
            with socket.create_connection(('127.0.0.1', 19530), timeout=4):
                return status == 200 and body.strip() == b'OK'
        return False

    def install(self):
        if not self.selected:
            self.flow.owner.event('未选择组件库服务，跳过组件库安装')
            self.export_connections()
            return
        self.preflight();self.host_services()
        if not self.images:
            self.export_connections()
            return
        no_symlink_parents(self.config_root, Path('/'))
        if self.config_root.is_symlink():
            raise RuntimeError('组件配置目录不能是链接')
        self.config_root.mkdir(parents=True, exist_ok=True, mode=0o700);self.config_root.chmod(0o700)
        atomic_json(self.compose, escape_compose(self.spec), 0o600)
        for index, name in enumerate(self.images):
            self.flow.owner.pause_point();self.flow.owner.update('安装组件：' + name, index / len(self.images), force=True)
            info = self.inspect(name)
            if info:
                self.compatible(name, info)
                if not info.get('State', {}).get('Running'):
                    self.flow.run(['docker', 'start', name])
            else:
                self.image(name)
                target = self.data_root / name;no_symlink_parents(target, Path('/'))
                if target.is_symlink():
                    raise RuntimeError('组件数据目录不能是链接')
                target.mkdir(parents=True, exist_ok=True)
                owned = self.flow.owner.state.setdefault('component_data_owned', [])
                if name not in owned:
                    owned.append(name);self.flow.owner.update(force=True)
                self.flow.run(['docker', 'compose', '-f', self.compose, 'up', '-d', '--no-deps',
                               '--no-build', '--pull', 'never', name], timeout=600)
            self.flow.wait(lambda: self.healthy(name), name + ' 认证与健康检查', timeout=360)
            passed = self.flow.owner.state.setdefault('components_passed', [])
            if name not in passed:
                passed.append(name)
            self.flow.owner.event(name + ' 已通过检查')
        self.export_connections()

    def acceptance(self):
        # systemctl with several units succeeds if ANY is active; check each required unit.
        for service in ['docker', *(name for name in ('nginx', 'etcd') if name in self.selected)]:
            self.flow.run(['systemctl','is-active','--quiet',service])
        if 'nginx' in self.selected and not self.nginx_healthy():raise RuntimeError('Nginx 最终服务或端口检查失败')
        if 'etcd' in self.selected and not self.etcd_healthy():
            raise RuntimeError('etcd 最终健康检查失败')
        for name in self.images:
            if not self.healthy(name):
                raise RuntimeError(name + ' 最终认证或健康检查失败')

    def export_connections(self):
        result = {'schema': 1, 'installer_version': self.flow.owner.release['installer_version'], 'host': '127.0.0.1',
                  'container_host': 'host.docker.internal', 'components': {},
                  'requested_components': requested_components(self.settings), 'selected_components': self.selected}
        users = {'mysql': 'root', 'redis': 'default', 'paradedb': 'paradedb', 'neo4j': 'neo4j', 'minio': self.settings.get('minio_user')}
        for name in self.images:
            row = {'ports': PORTS[name], 'data': str(self.data_root / name), 'image': self.images[name]['pinned']}
            if name in users:
                row.update(username=users[name], password=self.settings[name + '_password'])
            if name == 'paradedb':
                row['database'] = 'paradedb'
            result['components'][name] = row
        if 'nginx' in self.selected:
            result['components']['nginx'] = {'ports': [80], 'endpoint': 'http://127.0.0.1:80'}
        if 'etcd' in self.selected:
            result['etcd'] = {'endpoint': 'http://127.0.0.1:2379'}
            result['components']['etcd'] = dict(result['etcd'], ports=[2379, 2380])
        path = self.flow.owner.home / 'AI-Platform-1.1-connections.json'
        no_symlink_parents(path, self.flow.owner.home)
        if path.is_symlink():
            raise RuntimeError('连接配置导出目标不能是链接')
        atomic_json(path, result, 0o600)
        user = pwd.getpwnam('cix');os.chown(path, user.pw_uid, user.pw_gid)
        self.flow.owner.state['connections_file'] = str(path)
        self.flow.owner.event('组件连接配置已保存，仅安装用户可读')
