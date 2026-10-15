"""Detect, reuse or install the pinned probe; gate on registration and MQTT."""
from pathlib import Path
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import urllib.request

from common import atomic_json, atomic_text, copy_atomic, no_symlink_parents, sha256
from workflow import REGISTER_FIELDS, mqtt_connected


def detect_probe(binary=Path('/usr/local/bin/data-probe'), config_root=Path('/etc/data-probe'),
                 unit_roots=(Path('/etc/systemd/system'), Path('/lib/systemd/system'), Path('/usr/lib/systemd/system'))):
    """Read-only presence detection; never read private registration information in the GUI."""
    for path in (Path(binary), Path(config_root), *(Path(root)/'data-probe.service' for root in unit_roots)):
        try:
            path.lstat()
            return True
        except FileNotFoundError:
            pass
        except PermissionError:
            # A private installation is present even when its files are not readable by the desktop user.
            return True
    if platform.system() != 'Linux' or not shutil.which('systemctl'):
        return False
    try:
        result = subprocess.run(['systemctl', 'show', 'data-probe.service', '-p', 'LoadState', '--value'],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError('无法自动识别本机探针，请确认 systemd 可用后重试') from exc
    state = result.stdout.strip()
    if state == 'not-found':
        return False
    if state in ('loaded', 'masked', 'error', 'bad-setting', 'merged', 'stub'):
        return True
    raise RuntimeError('无法自动识别本机探针，请确认 systemd 可用后重试')


def read_probe_config(path):
    """Read our JSON-as-YAML config or the supported upstream scalar YAML template."""
    text = Path(path).read_text('utf-8')
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        result = {};section = None
        for line in text.splitlines():
            parent = re.fullmatch(r'([a-zA-Z_]+):\s*(?:#.*)?', line)
            if parent:
                section = parent[1];result[section] = {};continue
            field = re.match(r'^  ([\w]+):\s*(.*)$', line)
            if not field or section is None:
                continue
            raw = field[2].strip()
            if raw.startswith('"'):
                value, _ = json.JSONDecoder().raw_decode(raw)
            elif raw.startswith("'"):
                match = re.match(r"'((?:[^']|'')*)'", raw)
                if not match:
                    raise RuntimeError('已有探针配置包含不支持的 YAML 标量')
                value = match[1].replace("''", "'")
            else:
                value = raw.split(' #', 1)[0].strip()
                if value.lower() in ('true', 'false'):
                    value = value.lower() == 'true'
                elif value.isdecimal():
                    value = int(value)
                elif value.startswith(('&', '*', '!', '|', '>')):
                    raise RuntimeError('已有探针配置包含不支持的 YAML 类型')
            result[section][field[1]] = value
        return result


def make_config(settings, collectors=False):
    return {
        'platform': {'url': settings['platform_url']},
        'register': {**{key: settings[key] for key in REGISTER_FIELDS},
                     'bindActivateToken': settings['bindActivateToken'], 'os': 'debian 12',
                     'arch': 'aarch64', 'max_retries': 0, 'retry_base': 5},
        'mqtt': {'client_id': '', 'metrics_interval': 30, 'reconnect_interval': 5},
        'sm2': {'public_key': '', 'public_key_file': '/etc/data-probe/certs/sm2_pub.pem'},
        'local_server': {'port': 9090},
        'gateway': {'enabled': collectors, 'url': 'http://127.0.0.1:17700',
                    'token_env_file': '/home/cix/houmo-docker-deployment/.env',
                    'interval': 60, 'timeout': 10, 'max_body_bytes': 921600},
        'container_log': {'enabled': collectors, 'interval': 30, 'max_lines': 100,
                          'overlap_ms': 2000, 'timeout': 10},
        'tls': {'ca_file': '/etc/data-probe/certs/ca.pem', 'cert_file': '', 'key_file': '', 'verify_peer': True},
        'log': {'dir': '/var/log/data-probe', 'level': 'info'},
    }


class Probe:
    def __init__(self, workflow, config_root=Path('/etc/data-probe'), binary=Path('/usr/local/bin/data-probe'),
                 unit_root=Path('/etc/systemd/system')):
        self.flow = workflow;self.settings = workflow.settings['probe']
        self.config_root = Path(config_root);self.binary = Path(binary);self.unit_root = Path(unit_root)
        self.config = self.config_root / 'probe.yaml'
        self.automatic_reuse = self.settings.get('mode') == 'auto' and self.settings.get('reuse_existing') is True

    def binding(self):
        if self.config.is_symlink() or not self.config.is_file():
            raise RuntimeError('已有探针缺少有效配置文件')
        config = read_probe_config(self.config)
        if not isinstance(config, dict):
            raise RuntimeError('已有探针配置格式不正确，请修复现有探针后继续')
        if not self.automatic_reuse and config.get('platform', {}).get('url', '').rstrip('/') != self.settings['platform_url']:
            raise RuntimeError('已有探针绑定了其他平台，不自动重新注册')
        for field in (() if self.automatic_reuse else REGISTER_FIELDS):
            if config.get('register', {}).get(field) != self.settings[field]:
                raise RuntimeError('已有探针注册信息与本次设置不一致：' + field)
        if config.get('local_server', {}).get('port') != 9090:
            raise RuntimeError('已有探针的本地端口不兼容，需要 9090')
        return config

    def ready(self):
        service = self.flow.run(['systemctl', 'is-active', 'data-probe'], check=False)
        if service.stdout.strip() != 'active':
            return False
        registration = self.config_root / 'registration.yaml'
        if registration.is_symlink() or not registration.is_file() or registration.stat().st_size == 0:
            return False
        invocation = self.flow.run(['systemctl', 'show', 'data-probe', '-p', 'InvocationID', '--value']).stdout.strip()
        if not re.fullmatch('[0-9a-f]{32}', invocation):
            return False
        logs = self.flow.run(['journalctl', '_SYSTEMD_INVOCATION_ID=' + invocation,
                              '--no-pager', '--output=cat', '--lines=2000',
                              '--grep=MQTT connected to|MQTT disconnected|MQTT connection lost|MQTT connect failed']).stdout
        if not mqtt_connected(logs):
            return False
        # The probe's HTTP endpoint alone cannot prove registration or MQTT connectivity.
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://127.0.0.1:9090/health', timeout=4) as response:
            if response.status != 200:
                return False
        registration.chmod(0o600)
        return True

    def install(self):
        existing = detect_probe(self.binary, self.config_root,
                                (self.unit_root, Path('/lib/systemd/system'), Path('/usr/lib/systemd/system')))
        owned = self.flow.owner.state.get('probe_owned') is True
        if self.settings.get('mode') == 'auto':
            # Recheck at the mutation boundary; a probe that appeared after the GUI check must be preserved.
            self.automatic_reuse = existing and not owned
            if self.automatic_reuse:
                self.settings['reuse_existing'] = True
            if not existing and self.settings.get('reuse_existing') and not owned:
                raise RuntimeError('自动识别时存在的探针已缺失；请恢复原探针后继续，不以空配置重新注册')
        complete=self.binary.is_file() and self.config.is_file()
        for target in [self.config_root,self.binary,self.config]:
            no_symlink_parents(target,Path('/'))
            if target.is_symlink():raise RuntimeError('探针安装目标不能是链接：'+target.name)
        if existing and (complete or not owned):
            if self.binary.is_symlink() or not self.binary.is_file() or sha256(self.binary) != self.flow.lock['probe']['binary_sha256']:
                raise RuntimeError('已有探针版本与锁定的 1.1.2 不兼容，不自动覆盖')
            if not owned and not self.automatic_reuse and not self.settings['reuse_existing']:
                raise RuntimeError('检测到已有探针；请明确选择复用并填写与其一致的设备注册信息')
            self.binding()
            self.config_root.chmod(0o700);self.config.chmod(0o600)
            registration=self.config_root/'registration.yaml'
            if registration.is_symlink():raise RuntimeError('探针注册缓存不能是链接')
            if registration.is_file():registration.chmod(0o600)
            self.flow.owner.event('自动复用本机已有探针，保留原注册信息' if self.automatic_reuse else '复用已存在且注册信息一致的探针')
        else:
            if self.settings['reuse_existing'] and not owned:
                raise RuntimeError('选择了复用探针，但本机尚未安装探针')
            with socket.socket() as listener:
                try:listener.bind(('127.0.0.1',9090))
                except OSError:raise RuntimeError('探针本地端口 9090 已被占用，不停止其他程序') from None
            from engine import safe_extract
            self.flow.host_packages(self.flow.lock['probe_packages'])
            stage = self.flow.directory / 'probe-extract'
            no_symlink_parents(stage, self.flow.directory)
            if stage.exists():
                if stage.is_symlink():
                    raise RuntimeError('探针暂存目录不能是链接')
                shutil.rmtree(stage)
            safe_extract(self.flow.asset(self.flow.lock['probe']['archive']), stage)
            source = stage / 'data-probe-offline'
            # Persist intent before publishing files, so a partial deployment can be resumed.
            self.flow.owner.state['probe_owned'] = True;self.flow.owner.update(force=True)
            self.config_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.config_root.chmod(0o700)
            certs = self.config_root / 'certs';certs.mkdir(exist_ok=True, mode=0o700)
            for name in ['ca.pem', 'sm2_pub.pem']:
                target = certs / name
                if target.is_symlink():
                    raise RuntimeError('探针证书不能是链接')
                shutil.copyfile(source / 'config/certs' / name, target);target.chmod(0o600)
            atomic_json(self.config, make_config(self.settings), 0o600)
            self.binary.parent.mkdir(parents=True, exist_ok=True)
            copy_atomic(source/'build/data-probe',self.binary,Path('/'),mode=0o755)
            for src, target in [('deploy/data-probe.service', self.unit_root / 'data-probe.service'),
                                ('deploy/data-probe-rollback.service', self.unit_root / 'data-probe-rollback.service'),
                                ('deploy/data-probe-rollback.sh', Path('/usr/local/bin/data-probe-rollback.sh'))]:
                if os.path.lexists(target):
                    if target.is_symlink() or not target.is_file() or sha256(target) != sha256(source / src):
                        raise RuntimeError('已有探针服务文件冲突：' + target.name)
                else:
                    copy_atomic(source/src,target,Path('/'),mode=0o755 if target.suffix=='.sh' else 0o644)
        self.activate()

    def activate(self):
        # All ReadWritePaths in the vendor service must exist before systemd starts it.
        for directory in ['/data', '/opt', '/srv', '/mnt', '/var/log/data-probe']:
            path = Path(directory);no_symlink_parents(path, Path('/'))
            if path.is_symlink():
                raise RuntimeError('探针运行目录不能是链接：' + directory)
            path.mkdir(parents=True, exist_ok=True)
        self.secure_logging()
        self.flow.run(['systemctl', 'daemon-reload'])
        self.flow.run(['systemctl', 'enable', '--now', 'data-probe'])
        self.flow.wait(self.ready, '探针设备注册及 MQTT 连接', timeout=300)
        self.flow.owner.state['probe_passed'] = True
        self.flow.owner.event('探针已注册，当前进程 MQTT 连接与本地健康检查通过')

    def secure_logging(self):
        config=self.binding()
        if config.get('log',{}).get('dir')!='/var/log/data-probe':
            raise RuntimeError('已有探针日志位置不兼容，无法保证凭据日志权限')
        log_root=Path('/var/log/data-probe');log_root.chmod(0o700)
        for path in log_root.iterdir():
            if path.is_file() and not path.is_symlink():path.chmod(0o600)
        wrapper=Path('/usr/local/lib/ai-platform-installer/probe_runner.py')
        no_symlink_parents(wrapper,Path('/'))
        if wrapper.is_symlink():raise RuntimeError('探针日志过滤器不能是链接')
        source=Path(__file__).with_name('probe_runner.py')
        if wrapper.exists() and sha256(wrapper)!=sha256(source):raise RuntimeError('已有探针日志过滤器不同，不覆盖')
        wrapper.parent.mkdir(parents=True,exist_ok=True)
        copy_atomic(source,wrapper,Path('/'),mode=0o644)
        dropin=self.unit_root/'data-probe.service.d/ai-platform-log-filter.conf'
        no_symlink_parents(dropin,Path('/'))
        text='[Service]\nExecStart=\nExecStart=/usr/bin/python3 '+str(wrapper)+' '+str(self.config)+'\nUMask=0077\n'
        if dropin.is_symlink() or (dropin.exists() and dropin.read_text()!=text):
            raise RuntimeError('已有探针日志过滤配置冲突，不覆盖')
        changed=not dropin.exists()
        atomic_text(dropin,text,0o644)
        if changed:
            self.flow.run(['systemctl','daemon-reload'])
            active=self.flow.run(['systemctl','is-active','data-probe'],check=False)
            if active.stdout.strip()=='active':self.flow.run(['systemctl','restart','data-probe'])

    def enable_collectors(self):
        config = self.binding()
        gateway = config.get('gateway', {})
        if gateway.get('url') not in (None, '', 'http://127.0.0.1:17700'):
            raise RuntimeError('已有探针采集目标与本平台不一致，不覆盖')
        changed = gateway.get('enabled') is not True or config.get('container_log', {}).get('enabled') is not True
        if changed:
            gateway.update(enabled=True, url='http://127.0.0.1:17700',
                           token_env_file='/home/cix/houmo-docker-deployment/.env')
            config['gateway'] = gateway;config.setdefault('container_log', {})['enabled'] = True
            atomic_json(self.config, config, 0o600)
            self.flow.run(['systemctl', 'restart', 'data-probe'])
        self.flow.wait(self.ready, '模型安装后的探针 MQTT 重连', timeout=180)
        self.flow.owner.event('模型平台就绪，探针网关采集和容器采集已开启')
