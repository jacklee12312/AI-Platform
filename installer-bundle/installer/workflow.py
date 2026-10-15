"""Signed three-stage workflow, private settings, and verified prerequisite assets."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
from urllib.parse import urlsplit

from common import atomic_json, no_symlink_parents, read_json, safe_relative, sha256

MINIO_DIGEST = 'sha256:2f25127f784f002f1b98df5ae40cb1a67e0b8a877c90b6113c14b223a2ac65a5'
REGISTER_FIELDS = ('owner', 'project', 'city', 'location', 'deploymentPurpose', 'accessPolicy', 'hardwareModel')
PASSWORD_FIELDS = ('mysql_password', 'redis_password', 'paradedb_password', 'neo4j_password', 'minio_password')
COMPONENTS = {'nginx': 'Nginx', 'etcd': 'etcd', 'mysql': 'MySQL', 'redis': 'Redis',
              'paradedb': 'ParadeDB', 'neo4j': 'Neo4j', 'minio': 'MinIO', 'milvus': 'Milvus'}
COMPONENT_DEPENDENCIES = {'milvus': ('etcd', 'minio')}


def requested_components(settings):
    # Older onsite configurations installed everything; new configurations always carry an explicit list.
    selected = settings.get('selected', list(COMPONENTS))
    if (not isinstance(selected, list) or any(not isinstance(x, str) or x not in COMPONENTS for x in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError('组件选择必须为不重复的组件标识列表')
    return [name for name in COMPONENTS if name in selected]


def resolved_components(settings):
    selected = set(requested_components(settings))
    for name in tuple(selected):
        selected.update(COMPONENT_DEPENDENCIES.get(name, ()))
    return [name for name in COMPONENTS if name in selected]


def load_workflow(bundle, catalog):
    expected = catalog.get('workflow_sha256')
    if expected is None:
        return None
    path = Path(bundle) / 'catalog/workflow.json'
    if path.is_symlink() or sha256(path) != expected:
        raise RuntimeError('三阶段安装清单与发行签名不符')
    value = read_json(path)
    if value.get('schema') != 1 or value.get('target') != 'linux-arm64' or value.get('order') != ['probe', 'components', 'models']:
        raise RuntimeError('三阶段安装顺序或目标平台错误')
    images = value.get('images', [])
    if {r['id'] for r in images} != {'mysql', 'redis', 'paradedb', 'neo4j', 'minio', 'milvus'} or len(images) != 6:
        raise RuntimeError('组件库镜像清单不完整')
    for row in images:
        if not re.fullmatch('sha256:[0-9a-f]{64}', row['manifest_digest']) or not re.fullmatch('sha256:[0-9a-f]{64}', row['config_id']):
            raise RuntimeError('组件镜像缺少固定摘要')
        if row['pinned'] != row['image'].rsplit(':', 1)[0] + '@' + row['manifest_digest']:
            raise RuntimeError('组件镜像来源与摘要不一致')
        if not row.get('archive') or not row.get('rootfs_diff_ids'):
            raise RuntimeError('组件镜像缺少完整离线资源或文件系统摘要')
    minio = next(r for r in images if r['id'] == 'minio')
    if minio['image'] != 'dappros/minio:RELEASE.2025-09-07T16-13-09Z' or minio['manifest_digest'] != MINIO_DIGEST:
        raise RuntimeError('MinIO 不属于已批准的版本与来源')
    for row in workflow_assets(value):
        safe_relative(row['path'])
        if not re.fullmatch('[0-9a-f]{64}', row['sha256']) or type(row['size']) is not int or row['size'] < 0:
            raise RuntimeError('前置资源大小或摘要无效')
    if not value.get('host_packages'):
        raise RuntimeError('前置依赖闭包尚未准备完整')
    return value


def workflow_assets(lock):
    return [lock['probe']['archive'], *lock.get('host_packages', []),
            *(row['archive'] for row in lock['images'] if row.get('archive'))]


def validate_settings(data):
    if not isinstance(data, dict) or not isinstance(data.get('probe'), dict) or not isinstance(data.get('components'), dict):
        raise ValueError('请填写探针接入信息和组件库密码，或导入完整安装配置')
    probe = data['probe'];components = data['components']
    requested = requested_components(components)
    selected = resolved_components(components)
    result = {'probe': {}, 'components': {'selected': requested}}
    url = probe.get('platform_url', '').strip()
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('探针平台地址应为 HTTP/HTTPS 地址，不包含账号或查询参数')
    result['probe']['platform_url'] = url.rstrip('/')
    reuse = probe.get('reuse_existing', False)
    if type(reuse) is not bool:
        raise ValueError('探针复用选项应为布尔值')
    result['probe']['reuse_existing'] = reuse
    for key in REGISTER_FIELDS:
        value = probe.get(key, '')
        if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(x in value for x in '\r\n\x00'):
            raise ValueError('请完整填写探针设备注册信息：' + key)
        result['probe'][key] = value.strip()
    token = probe.get('bindActivateToken', '')
    if not isinstance(token, str) or len(token) > 8192 or (not reuse and not token.strip()) or any(x in token for x in '\r\n\x00'):
        raise ValueError('新安装探针需要有效的接入令牌；复用已注册探针时可留空')
    result['probe']['bindActivateToken'] = token.strip()
    for key in PASSWORD_FIELDS:
        if key.removesuffix('_password') not in selected:
            continue
        password = components.get(key, '')
        if not isinstance(password, str) or not 12 <= len(password) <= 128 or any(x in password for x in '\r\n\x00'):
            raise ValueError('组件密码需为 12–128 个字符，不含换行：' + key)
        result['components'][key] = password
    if 'minio' in selected:
        user = components.get('minio_user', '')
        if not isinstance(user, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{3,64}', user):
            raise ValueError('MinIO 用户名需为 3–64 位字母、数字、下划线、点或短横线')
        result['components']['minio_user'] = user
    return result


def mqtt_connected(log):
    """Current systemd invocation only; a later loss invalidates earlier success."""
    connected = False
    for line in log.splitlines():
        if 'MQTT connected to ' in line:
            connected = True
        elif any(message in line for message in ('MQTT disconnected', 'MQTT connection lost', 'MQTT connect failed', 'MQTTAsync_connect failed', 'MQTT connect called with existing client handle')):
            connected = False
    return connected


class Workflow:
    def __init__(self, installer):
        self.owner = installer
        self.lock = load_workflow(installer.bundle, installer.catalog)
        self.request_path = installer.root / 'private/request.json'
        self.settings = validate_settings(read_json(self.request_path).get('workflow'))
        self.directory = installer.root / 'private/workflow'
        no_symlink_parents(self.directory, installer.root)
        if self.directory.is_symlink():
            raise RuntimeError('前置安装暂存目录不能是链接')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def run(self, args, *, env=None, input=None, timeout=180, check=True):
        """Never publish commands, credentials, or arbitrary subprocess output."""
        completed = subprocess.run([str(x) for x in args], input=input, env=env, text=True,
                                   capture_output=True, timeout=timeout)
        if check and completed.returncode:
            from probe_runner import redact_line
            values=[self.settings['probe']['bindActivateToken'],*self.settings['components'].values()]
            values=[v for v in values if isinstance(v,str) and v]
            values+= [v.replace('$','$$') for v in values]
            detail=redact_line((completed.stderr or completed.stdout)[-3000:],values)
            print('WORKFLOW '+Path(str(args[0])).name+': '+detail,flush=True)
            raise RuntimeError(Path(str(args[0])).name + ' 执行失败（退出码 ' + str(completed.returncode) + '）；请检查对应服务状态')
        return completed

    def wait(self, check, description, timeout=240):
        limit = time.monotonic() + timeout
        while True:
            self.owner.pause_point()
            try:
                if check():
                    return
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                pass
            if time.monotonic() >= limit:
                raise RuntimeError(description + '未在规定时间内通过；保留已完成阶段，修复后可继续')
            self.owner.update('等待' + description)
            time.sleep(2)

    def asset(self, row):
        from artifacts import BlobStore, source_config
        request = read_json(self.request_path)
        config = source_config(self.owner.bundle, request['sources'])
        # Publisher archives are local resources; raw official Debian debs can be downloaded by their pinned checksum.
        config = json.loads(json.dumps(config))
        config.setdefault('file_urls', {}).setdefault(row['path'], row.get('urls', []))
        resource = dict(row, category='platform', models=[])
        store = BlobStore(self.owner.bundle, config, progress=lambda r, n, p: self.owner.update(
            p + '前置资源：' + Path(r['path']).name), pause=self.owner.pause_point, rows=[resource])
        return store.ensure(resource)

    def host_packages(self, names):
        stage = self.directory / 'host-debs';stage.mkdir(exist_ok=True)
        versions = {r['package']: r['version'] for r in self.lock['host_packages'] if r.get('package')}
        needed = []
        for name in names:
            result = self.run(['dpkg-query', '-W', '-f=${Status}|${Version}', name], check=False)
            actual=result.stdout.strip()
            compatible=actual=='install ok installed|'+versions[name]
            if not compatible and actual.startswith('install ok installed|') and name not in self.lock['component_packages']:
                compatible=self.run(['dpkg','--compare-versions',actual.split('|',1)[1],'ge',versions[name]],check=False).returncode==0
            if result.returncode or not compatible:needed.append(name)
        if not needed:
            return
        selected = resolved_components(self.settings['components'])
        excluded = set()
        if 'nginx' not in selected: excluded.update(('nginx', 'nginx-common'))
        if 'etcd' not in selected: excluded.update(('etcd-server', 'etcd-client'))
        for row in self.lock['host_packages']:
            if row.get('package') in excluded:
                continue
            source = self.asset(row)
            target = stage / Path(row['path']).name
            if target.is_symlink():
                raise RuntimeError('依赖暂存文件不能是链接')
            shutil.copyfile(source, target);target.chmod(0o644)
        # Private, file-only APT repository. Existing system repositories and daemon settings are preserved.
        lists = self.directory / 'apt-lists';(lists / 'partial').mkdir(parents=True, exist_ok=True)
        sources = self.directory / 'apt-sources.list'
        sources.write_text('deb [trusted=yes] file:' + str(stage) + ' ./\n')
        arguments = ['apt-get', '-o', 'Dir::Etc::sourcelist=' + str(sources),
                     '-o', 'Dir::Etc::sourceparts=-', '-o', 'Dir::State::lists=' + str(lists),
                     '-o', 'APT::Sandbox::User=root', '-o', 'Acquire::Languages=none',
                     '-o', 'Dpkg::Options::=--force-confold']
        self.run(arguments + ['update'], timeout=180)
        environment = dict(os.environ, DEBIAN_FRONTEND='noninteractive', NEEDRESTART_MODE='l')
        self.run(arguments + ['--yes', '--no-install-recommends', 'install',
                             *(name + '=' + versions[name] for name in needed)], env=environment, timeout=900)

    def docker(self):
        from artifacts import BlobStore, source_config
        rows = [r for r in self.owner.catalog['resources'] if r['category'] == 'docker']
        if len(rows) != 5:
            raise RuntimeError('配套 Docker 软件包应恰好为 5 个')
        store = BlobStore(self.owner.bundle, source_config(self.owner.bundle, read_json(self.request_path)['sources']),
                          rows=rows, pause=self.owner.pause_point)
        paths = [store.ensure(row) for row in rows]
        ordered = []
        pinned = True
        for name in ['containerd.io', 'docker-ce-cli', 'docker-ce', 'docker-buildx-plugin', 'docker-compose-plugin']:
            package = next(p for p in paths if self.run(['dpkg-deb', '-f', p, 'Package']).stdout.strip() == name)
            version = self.run(['dpkg-deb', '-f', package, 'Version']).stdout.strip()
            current = self.run(['dpkg-query', '-W', '-f=${Status}|${Version}', name], check=False)
            pinned = pinned and current.returncode == 0 and current.stdout.strip() == 'install ok installed|' + version
            ordered.append(package)
        if not pinned:
            if shutil.which('docker'):
                existing = self.run(['docker', 'ps', '-aq'], check=False)
                if existing.returncode or existing.stdout.strip():
                    raise RuntimeError('已有容器使用非配套 Docker；不自动升级、停止或接管')
            for package in ordered:
                self.run(['dpkg', '-i', package], timeout=300)
        self.run(['systemctl', 'enable', '--now', 'docker'])
        self.run(['docker', 'info'], timeout=30)
        self.run(['usermod', '-aG', 'docker', 'cix'])

    def probe(self):
        from probe import Probe
        Probe(self).install()

    def components(self):
        from components import Components
        self.docker()
        Components(self).install()

    def finish(self):
        from components import Components
        from probe import Probe
        Components(self).acceptance()
        Probe(self).enable_collectors()
        self.owner.state['workflow_passed'] = True
        self.owner.event('探针、组件库及模型阶段均已通过本次安装检查')
