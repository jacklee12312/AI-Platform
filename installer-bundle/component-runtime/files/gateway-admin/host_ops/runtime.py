"""Reuse installer component deployment and health checks on the actual host."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace

from .core.common import atomic_json, sha256
from .core.components import Components, PORTS
from .core.workflow import COMPONENTS, PASSWORD_FIELDS, resolved_components, Workflow

HOME = Path('/home/cix')
ROOT = Path(__file__).resolve().parent
STATE = Path('/var/lib/modeldeck-ops')


def run(args, *, env=None, input=None, timeout=180, check=True):
    result = subprocess.run([str(x) for x in args], env=env, input=input, text=True,
                            capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError('组件操作未完成，请查看对应组件日志：'+str(args[0]))
    return result


def read_connections():
    path = HOME / 'AI-Platform-1.1-connections.json'
    if path.is_symlink():
        raise ValueError('组件连接配置不能是链接')
    return json.loads(path.read_text()) if path.is_file() else {'components': {}, 'selected_components': []}


def settings_for(ids, supplied=None):
    if not ids or len(set(ids)) != len(ids) or any(i not in COMPONENTS for i in ids):
        raise ValueError('请选择不重复的已适配组件')
    previous = read_connections()
    settings = {'selected': ids}
    pending = STATE/'credentials.json'
    if pending.is_file() and not pending.is_symlink():
        settings.update(json.loads(pending.read_text()))
    for name, row in previous['components'].items():
        if row.get('password'):
            settings[name+'_password'] = row['password']
        if name == 'minio':
            settings['minio_user'] = row.get('username', '')
    # Existing credentials are immutable here; password changes require a database operation.
    for key, value in (supplied or {}).items():
        if key not in PASSWORD_FIELDS + ('minio_user',):
            raise ValueError('未知的组件配置项')
        name = key.removesuffix('_password')
        existing = previous['components'].get('minio' if key=='minio_user' else name, {})
        if existing and key in settings and settings[key] != value:
            raise ValueError('已有组件凭据不同，请使用现有凭据')
        settings[key] = value
    for name in resolved_components(settings):
        key = name+'_password'
        if key in PASSWORD_FIELDS:
            value = settings.get(key, '')
            if not isinstance(value, str) or not 12 <= len(value) <= 128 or any(c in value for c in '\r\n\x00'):
                raise ValueError('请为 '+name+' 设置 12–128 位密码')
    if 'minio' in resolved_components(settings):
        import re
        if not re.fullmatch(r'[A-Za-z0-9_.-]{3,64}', settings.get('minio_user', '')):
            raise ValueError('请填写 MinIO 用户名（3–64 位字母、数字、点、下划线）')
    return settings


class Flow:
    def __init__(self, settings):
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = json.loads((ROOT/'workflow.json').read_text())
        self.settings = {'components': settings}
        self.directory = STATE
        self.state_path = STATE/'components.json'
        state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.owner = SimpleNamespace(home=HOME, state=state, release={'installer_version': '1.2.0'},
                                     update=self.update, event=lambda _: None, pause_point=lambda: None)
        self.run = run

    def update(self, *args, **kwargs):
        atomic_json(self.state_path, self.owner.state, 0o600)

    def wait(self, check, label, timeout=300):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            try:
                if check(): return
            except (OSError, ValueError, RuntimeError):
                pass
            time.sleep(2)
        raise RuntimeError(label+' 未通过；已有数据保留，可查看日志后重试')

    def asset(self, row):
        path = ROOT/'resources'/row['path']
        if not path.is_file() or path.is_symlink() or path.stat().st_size != row['size'] or sha256(path) != row['sha256']:
            raise ValueError('配套组件依赖缺失或摘要不符：'+row['path'])
        return path

    host_packages = Workflow.host_packages


class ManagedComponents(Components):
    def export_connections(self):
        previous = read_connections()
        result = {**previous, 'schema': 1, 'installer_version': '1.2.0', 'host': '127.0.0.1',
                  'container_host': 'host.docker.internal', 'components': dict(previous['components'])}
        users = {'mysql':'root', 'redis':'default', 'paradedb':'paradedb', 'neo4j':'neo4j', 'minio':self.settings.get('minio_user')}
        for name, image in self.images.items():
            row = {'ports':PORTS[name], 'data':str(self.data_root/name), 'image':image['pinned']}
            if name in users: row.update(username=users[name], password=self.settings[name+'_password'])
            if name=='paradedb': row['database']='paradedb'
            result['components'][name]=row
        for name, ports in [('nginx',[80]),('etcd',[2379,2380])]:
            if name in self.selected:
                result['components'][name]={'ports':ports,'endpoint':'http://127.0.0.1:'+str(ports[0])}
        if 'etcd' in result['components']: result['etcd']={'endpoint':'http://127.0.0.1:2379'}
        for key in ('requested_components','selected_components'):
            result[key]=list(dict.fromkeys(previous.get(key,[])+self.selected))
        path=HOME/'AI-Platform-1.1-connections.json'
        atomic_json(path,result,0o600)
        import pwd
        user=pwd.getpwnam('cix');os.chown(path,user.pw_uid,user.pw_gid)

    def image(self, name):
        row = self.images[name]
        result = run(['docker', 'image', 'inspect', row['config_id']], check=False)
        if result.returncode:
            result = run(['docker', 'image', 'inspect', row['pinned']], check=False)
        if result.returncode:
            archive = ROOT/'resources'/row['archive']['path']
            if archive.is_file():
                if archive.is_symlink() or sha256(archive) != row['archive']['sha256']:
                    raise ValueError('本地组件镜像摘要不符')
                run(['docker', 'load', '-i', archive], timeout=1800)
            else:
                run(['docker', 'pull', '--platform=linux/arm64', row['pinned']], timeout=1800)
            result = run(['docker', 'image', 'inspect', row['pinned']], check=False)
            if result.returncode: result = run(['docker', 'image', 'inspect', row['config_id']])
        info = json.loads(result.stdout)[0]
        if (info['Id'] not in {row['config_id'], row['manifest_digest']} or info['Architecture'] != 'arm64' or info['Os'] != 'linux'
                or info['RootFS']['Layers'] != row['rootfs_diff_ids']):
            raise ValueError('组件镜像与固定 ARM64 摘要不符：'+name)
        self.spec['services'][name]['image'] = info['Id']


def manager(ids, supplied=None):
    return ManagedComponents(Flow(settings_for(ids, supplied)))


def component_rows():
    connections = read_connections()
    rows = []
    # Only registered component names can ever reach Docker or systemd.
    for name, title in COMPONENTS.items():
        managed = False; running = False; health = None; image = ''; present = False
        if name in ('nginx', 'etcd'):
            present = bool(shutil.which(name))
            running = run(['systemctl', 'is-active', '--quiet', name], check=False).returncode == 0
            managed = name in connections['components']
            if running and managed:
                try:
                    instance = manager([name]); health = instance.nginx_healthy() if name == 'nginx' else instance.etcd_healthy()
                except Exception: health = False
        else:
            result = run(['docker', 'inspect', name], check=False)
            if result.returncode == 0:
                info = json.loads(result.stdout)[0]; present = True
                managed = info.get('Config', {}).get('Labels', {}).get('ai-platform.installer.component') == name
                running = bool(info.get('State', {}).get('Running')); image = info.get('Config', {}).get('Image', '')
                if running and managed:
                    try: health = manager([name]).healthy(name)
                    except Exception: health = False
            elif run(['docker', 'info'], check=False).returncode:
                rows.append({'id': name, 'name': title, 'status': 'unknown', 'managed': False,
                             'running': False, 'healthy': None, 'image': '', 'ports': PORTS.get(name, [])})
                continue
        status = 'not_installed' if not present else ('external' if not managed else ('healthy' if health else 'unhealthy') if running else 'stopped')
        rows.append({'id': name, 'name': title, 'status': status, 'managed': managed, 'running': running,
                     'healthy': health, 'image': image, 'ports': PORTS.get(name, [80] if name == 'nginx' else [2379, 2380])})
    return rows


def plan(ids, supplied=None):
    instance = manager(ids, supplied)
    rows = {r['id']: r for r in component_rows()}
    for name in instance.selected:
        if rows[name]['status'] in ('external', 'unknown'):
            raise ValueError(name+' 不属于本安装器管理或状态无法确认，不能自动接管')
    instance.preflight()
    return {'valid': True, 'selected': instance.selected, 'dependencies': [n for n in instance.selected if n not in ids],
            'steps': ['检查端口与现有数据', '按摘要获取镜像或依赖', '部署所选组件', '认证与健康检查', '更新连接配置']}


def install(ids, supplied=None):
    plan(ids, supplied)
    instance = manager(ids, supplied)
    # Preserve credentials for a retry even if image pulling or initialization fails.
    atomic_json(STATE/'credentials.json', {k:v for k,v in instance.settings.items() if k in PASSWORD_FIELDS+('minio_user',)}, 0o600)
    # Preserve previously configured services in the shared Compose file.
    compose = instance.compose
    if compose.is_file():
        current = json.loads(compose.read_text())
        for name, service in current.get('services', {}).items():
            if name not in instance.spec['services']:
                instance.spec['services'][name] = service
    instance.install()
    return {'components': instance.selected, 'status': 'installed', 'message': '组件安装及健康检查通过'}


def action(name, verb):
    if name not in COMPONENTS or verb not in ('start', 'stop', 'restart'):
        raise ValueError('不支持的组件操作')
    rows = {r['id']: r for r in component_rows()}
    if not rows[name]['managed']:
        raise ValueError('该组件尚未安装或不属于本安装器管理')
    if verb in ('stop', 'restart') and name in ('etcd', 'minio') and rows['milvus']['running']:
        raise ValueError('Milvus 正在使用该组件，请先停止 Milvus')
    if name=='milvus' and verb in ('start','restart') and any(not rows[dep]['running'] for dep in ('etcd','minio')):
        raise ValueError('请先启动 Milvus 依赖的 etcd 和 MinIO')
    run(['systemctl', verb, name] if name in ('nginx', 'etcd') else ['docker', verb, name], timeout=120)
    if verb != 'stop':
        instance = manager([name])
        check = instance.nginx_healthy if name == 'nginx' else instance.etcd_healthy if name == 'etcd' else lambda: instance.healthy(name)
        instance.flow.wait(check, name+' 健康检查')
    return {'component': name, 'action': verb, 'status': 'succeeded'}


def logs(name):
    if name not in COMPONENTS:
        raise ValueError('未知组件')
    result = run(['journalctl', '-u', name, '--no-pager', '-n', '200'] if name in ('nginx', 'etcd')
                 else ['docker', 'logs', '--tail', '200', '--timestamps', name], check=False)
    text = (result.stdout+result.stderr)[-100000:]
    for row in read_connections()['components'].values():
        secret = row.get('password')
        if secret: text = text.replace(secret, '[已隐藏]')
    pending=STATE/'credentials.json'
    if pending.is_file() and not pending.is_symlink():
        for key,secret in json.loads(pending.read_text()).items():
            if key in PASSWORD_FIELDS and secret:text=text.replace(secret,'[已隐藏]')
    return {'component': name, 'lines': text.splitlines()[-200:]}
