"""Install the exact Docker package set, retaining data and resumable container state."""
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from common import atomic_json, no_symlink_parents, sha256

PACKAGES = ('containerd.io', 'docker-ce-cli', 'docker-ce', 'docker-buildx-plugin', 'docker-compose-plugin')
REPLACED = (*PACKAGES, 'docker-ce-rootless-extras', 'docker.io', 'docker-engine', 'podman-docker', 'containerd', 'runc')
DOCKER = ['docker', '--host', 'unix:///var/run/docker.sock']


class DockerRuntime:
    def __init__(self, workflow, config_roots=(Path('/etc/docker'), Path('/etc/containerd'))):
        self.flow = workflow
        self.config_roots = tuple(map(Path, config_roots))

    def save(self, status):
        self.record['status'] = status
        self.flow.owner.state.setdefault('private', {})['docker_replacement'] = self.record
        self.flow.owner.update(force=True)

    def package_state(self, name):
        result = self.flow.run(['dpkg-query', '-W', '-f=${Status}|${Version}', name], check=False)
        return result.stdout.strip() if result.returncode == 0 else ''

    def installed(self, expected):
        return all(self.package_state(name) == 'install ok installed|' + version for name, version in expected.items())

    def inventory(self):
        empty = {'containers': [], 'running': [], 'paused': [], 'images': [], 'volumes': [], 'data_root': None}
        if not shutil.which('docker'):return empty
        info = self.flow.run(DOCKER + ['info', '--format', '{{json .}}'], check=False, timeout=30)
        if info.returncode:
            # An interrupted replacement already has its original inventory in the private journal.
            if shutil.which('docker'):
                raise RuntimeError('旧 Docker 服务不可访问，无法记录原容器状态；请恢复服务后重试')
            return empty
        data = json.loads(info.stdout)
        if data.get('OSType') != 'linux' or data.get('Architecture') not in ('arm64', 'aarch64'):
            raise RuntimeError('现有 Docker 不是本机 Linux ARM64 环境')
        ids = self.flow.run(DOCKER + ['ps', '-aq', '--no-trunc']).stdout.split()
        if any(not re.fullmatch('[0-9a-f]{64}', value) for value in ids):
            raise RuntimeError('旧 Docker 返回了无效容器标识')
        running = [];paused = []
        for identifier in ids:
            state = json.loads(self.flow.run(DOCKER + ['inspect', '--format', '{{json .State}}', identifier]).stdout)
            if state.get('Running'):running.append(identifier)
            if state.get('Paused'):paused.append(identifier)
        return {'containers': ids, 'running': running, 'paused': paused,
                'images': self.flow.run(DOCKER + ['image', 'ls', '-aq', '--no-trunc']).stdout.split(),
                'volumes': self.flow.run(DOCKER + ['volume', 'ls', '-q']).stdout.split(),
                'data_root': data.get('DockerRootDir')}

    def backup(self):
        directory = self.flow.directory
        no_symlink_parents(directory, Path('/'))
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = Path(tempfile.mkdtemp(prefix='docker-replace-', dir=directory));backup.chmod(0o700)
        rows = []
        for index, root in enumerate(self.config_roots):
            no_symlink_parents(root, Path('/'))
            if root.exists():
                if not root.is_dir() or root.is_symlink():raise RuntimeError('Docker 配置目录类型不受支持')
                shutil.copytree(root, backup / str(index), symlinks=True)
                for source in (backup / str(index)).rglob('*'):
                    if source.is_symlink():raise RuntimeError('Docker 配置含链接，请先改为本机普通配置文件')
                    if source.is_file():
                        rows.append({'slot': source.relative_to(backup).as_posix(),
                                     'target': str(root / source.relative_to(backup / str(index))),
                                     'sha256': sha256(source), 'mode': source.stat().st_mode & 0o777})
        atomic_json(backup / 'manifest.json', rows)
        self.record.update(backup=str(backup), backup_manifest_sha256=sha256(backup / 'manifest.json'))

    def restore_config(self):
        backup = Path(self.record['backup'])
        no_symlink_parents(backup, self.flow.directory)
        if not backup.resolve().is_relative_to(self.flow.directory.resolve()):raise RuntimeError('Docker 配置备份路径越界')
        if sha256(backup / 'manifest.json') != self.record['backup_manifest_sha256']:raise RuntimeError('Docker 配置备份清单损坏')
        rows = json.loads((backup / 'manifest.json').read_text('utf-8'))
        for row in rows:
            source = backup / row['slot'];target = Path(row['target'])
            if not source.resolve().is_relative_to(backup.resolve()) or not any(target.is_relative_to(root) for root in self.config_roots):
                raise RuntimeError('Docker 配置备份目标越界')
            if source.is_symlink() or sha256(source) != row['sha256']:raise RuntimeError('Docker 配置备份校验失败')
        for row in rows:
            target = Path(row['target']);no_symlink_parents(target, Path('/'))
            if target.is_symlink():raise RuntimeError('Docker 配置目标不能是链接')
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.docker-config-', dir=target.parent)
            os.close(fd)
            try:
                shutil.copy2(backup / row['slot'], temporary)
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):os.unlink(temporary)

    def restore_containers(self):
        current = self.inventory()
        for key in ('containers', 'images', 'volumes'):
            if not set(self.record[key]).issubset(current[key]):
                raise RuntimeError('Docker 更换后原' + key + '未完整识别；数据目录保留，请检查后继续')
        if self.record['data_root'] and current['data_root'] != self.record['data_root']:
            raise RuntimeError('Docker 数据目录与替换前不同，停止继续安装')
        for identifier in self.record['containers']:
            was_running = identifier in self.record['running'];is_running = identifier in current['running']
            if was_running and not is_running:self.flow.run(DOCKER + ['start', identifier], timeout=180)
            if not was_running and is_running:self.flow.run(DOCKER + ['stop', '--time=60', identifier], timeout=180)
            if identifier in self.record['paused'] and identifier not in current['paused']:
                self.flow.run(DOCKER + ['pause', identifier])
        final = self.inventory()
        if (set(self.record['running']) != set(final['running']) & set(self.record['containers'])
                or not set(self.record['paused']).issubset(final['paused'])):
            raise RuntimeError('原容器运行状态尚未恢复，请检查容器后继续安装')

    def install(self, paths):
        packages = {};expected = {}
        for path in paths:
            name = self.flow.run(['dpkg-deb', '-f', path, 'Package']).stdout.strip()
            version = self.flow.run(['dpkg-deb', '-f', path, 'Version']).stdout.strip()
            arch = self.flow.run(['dpkg-deb', '-f', path, 'Architecture']).stdout.strip()
            if name not in PACKAGES or name in packages or arch != 'arm64':raise RuntimeError('配套 Docker 包名称或架构错误')
            packages[name] = path;expected[name] = version
        if set(packages) != set(PACKAGES):raise RuntimeError('配套 Docker 软件包应恰好为 5 个')
        self.record = self.flow.owner.state.get('private', {}).get('docker_replacement', {})
        pending = bool(self.record) and self.record.get('status') != 'committed'
        pinned = self.installed(expected)
        if pending and self.record['expected'] != expected:raise RuntimeError('未完成的 Docker 替换属于其他配套版本')
        if not pinned:
            if not pending:
                self.record = dict(self.inventory(), expected=expected)
                self.backup();self.save('prepared')
            # Check reverse dependencies while the original containers are still available.
            states = {name: self.package_state(name) for name in REPLACED}
            old = [name for name, state in states.items() if state and 'not-installed' not in state and 'config-files' not in state]
            if old:self.flow.run(['dpkg', '--no-act', '--remove', *old], timeout=120)
            self.flow.owner.event('Docker 配套版本不一致：保留容器数据，自动卸载旧软件包并安装包内版本')
            self.save('stopping')
            if shutil.which('docker') and self.flow.run(DOCKER + ['info'], check=False, timeout=30).returncode == 0:
                current = self.inventory()
                for identifier in current['paused']:
                    self.flow.run(DOCKER + ['unpause', identifier])
                if current['running']:
                    self.flow.run(DOCKER + ['stop', '--time=60', *current['running']], timeout=240)
            for unit in ('docker.socket', 'docker.service', 'containerd.service'):
                result = self.flow.run(['systemctl', 'show', unit, '-p', 'LoadState', '--value'], check=False)
                if result.stdout.strip() not in ('', 'not-found'):self.flow.run(['systemctl', 'stop', unit], timeout=120)
            self.save('removing')
            if old:self.flow.run(['dpkg', '--remove', *old], timeout=300)
            self.save('installing')
            environment = dict(os.environ, DEBIAN_FRONTEND='noninteractive', NEEDRESTART_MODE='l')
            self.flow.run(['dpkg', '--force-confold', '--install', *(packages[name] for name in PACKAGES)], env=environment, timeout=600)
        if pending or not pinned:self.restore_config()
        if not self.installed(expected):raise RuntimeError('Docker 软件包版本未全部切换成功，保留进度供继续安装')
        self.flow.run(['systemctl', 'daemon-reload'])
        self.flow.run(['systemctl', 'enable', '--now', 'containerd', 'docker'])
        self.flow.run(DOCKER + ['info'], timeout=30)
        self.flow.run(DOCKER + ['compose', 'version'], timeout=30)
        self.flow.run(DOCKER + ['buildx', 'version'], timeout=30)
        if pending or not pinned:
            self.save('restoring');self.restore_containers();self.save('committed')
            self.flow.owner.event('Docker 五个配套软件包切换完成，原容器运行状态已恢复')
        else:self.flow.owner.event('复用已安装的全部 5 个配套 Docker 软件包')
        self.flow.run(['usermod', '-aG', 'docker', 'cix'])
