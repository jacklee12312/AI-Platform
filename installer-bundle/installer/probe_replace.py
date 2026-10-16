"""Private, recoverable replacement of a probe's executable and service files."""
import json
import os
from pathlib import Path
import shutil
import tempfile

from common import atomic_json, copy_atomic, no_symlink_parents, sha256


class Replacement:
    def __init__(self, probe):
        self.probe = probe
        self.flow = probe.flow
        self.targets = [probe.binary, probe.config_root,
                        probe.unit_root / 'data-probe.service',
                        probe.unit_root / 'data-probe-rollback.service',
                        probe.unit_root / 'data-probe.service.d',
                        probe.binary.parent / 'data-probe-rollback.sh',
                        probe.lib_root / 'probe_runner.py']

    def status(self, value):
        self.record['status'] = value
        self.flow.owner.state.setdefault('private', {})['probe_replacement'] = self.record
        self.flow.owner.update(force=True)

    def validate_target(self, path):
        if path not in self.targets or path == Path('/'):
            raise RuntimeError('探针恢复目标不属于本次替换范围')
        no_symlink_parents(path.parent, Path('/'))
        if path.is_symlink():
            raise RuntimeError('探针替换目标不能是链接：' + path.name)
        if path.exists() and not (path.is_file() or path.is_dir()):
            raise RuntimeError('探针替换目标类型不受支持：' + path.name)

    def prepare(self):
        if (self.probe.config_root / '.upgrading').exists():
            raise RuntimeError('探针正在自升级，请完成后再替换')
        for path in self.targets:
            self.validate_target(path)
        directory = self.flow.directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = Path(tempfile.mkdtemp(prefix='probe-replace-', dir=directory))
        backup.chmod(0o700)
        rows = []
        for index, path in enumerate(self.targets):
            row = {'target': str(path), 'exists': path.exists(), 'slot': str(index)}
            if path.exists():
                destination = backup / str(index)
                row['directory'] = path.is_dir()
                if path.is_dir():
                    shutil.copytree(path, destination, symlinks=True)
                else:
                    shutil.copy2(path, destination)
            rows.append(row)
        hashes = {}
        links = {}
        for path in backup.rglob('*'):
            name = path.relative_to(backup).as_posix()
            if path.is_symlink():
                links[name] = os.readlink(path)
            elif path.is_file():
                hashes[name] = sha256(path)
        active = self.flow.run(['systemctl', 'is-active', 'data-probe'], check=False).stdout.strip() == 'active'
        enabled = self.flow.run(['systemctl', 'is-enabled', 'data-probe'], check=False).stdout.strip()
        manifest = {'targets': rows, 'hashes': hashes, 'links': links, 'active': active, 'enabled': enabled}
        atomic_json(backup / 'manifest.json', manifest, 0o600)
        self.record = {'backup': str(backup), 'manifest_sha256': sha256(backup / 'manifest.json')}
        self.status('prepared')

    def remove(self, path):
        self.validate_target(path)
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    def deploy(self, source):
        self.status('replacing')
        self.flow.run(['systemctl', 'stop', 'data-probe'])
        # Configuration, certificates and registration remain in place.
        for path in self.targets:
            if path != self.probe.config_root:
                self.remove(path)
        files = [('build/data-probe', self.probe.binary, 0o755),
                 ('deploy/data-probe.service', self.probe.unit_root / 'data-probe.service', 0o644),
                 ('deploy/data-probe-rollback.service', self.probe.unit_root / 'data-probe-rollback.service', 0o644),
                 ('deploy/data-probe-rollback.sh', self.probe.binary.parent / 'data-probe-rollback.sh', 0o755)]
        for relative, target, mode in files:
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_atomic(source / relative, target, Path('/'), mode=mode)
        self.status('activating')

    def restore(self):
        backup = Path(self.record['backup'])
        if not backup.resolve().is_relative_to(self.flow.directory.resolve()) or not backup.name.startswith('probe-replace-'):
            raise RuntimeError('探针备份路径不属于本任务')
        no_symlink_parents(backup, self.flow.directory)
        if sha256(backup / 'manifest.json') != self.record['manifest_sha256']:
            raise RuntimeError('探针备份清单校验失败，保留现场等待恢复')
        manifest = json.loads((backup / 'manifest.json').read_text('utf-8'))
        if [Path(row['target']) for row in manifest['targets']] != self.targets:
            raise RuntimeError('探针备份恢复目标与当前安装范围不一致')
        for name, expected in manifest['hashes'].items():
            path = backup / name
            if not path.resolve().is_relative_to(backup.resolve()) or path.is_symlink() or sha256(path) != expected:
                raise RuntimeError('探针备份文件校验失败')
        for name, expected in manifest['links'].items():
            if os.readlink(backup / name) != expected:
                raise RuntimeError('探针备份链接校验失败')
        self.flow.run(['systemctl', 'stop', 'data-probe'], check=False)
        self.flow.run(['systemctl', 'disable', 'data-probe'], check=False)
        for row, target in zip(manifest['targets'], self.targets):
            self.remove(target)
            if row['exists']:
                target.parent.mkdir(parents=True, exist_ok=True)
                source = backup / row['slot']
                if row['directory']:
                    shutil.copytree(source, target, symlinks=True)
                else:
                    shutil.copy2(source, target)
        self.flow.run(['systemctl', 'daemon-reload'])
        self.flow.run(['systemctl', 'reset-failed', 'data-probe'], check=False)
        if manifest['enabled'].startswith('enabled'):
            self.flow.run(['systemctl', 'enable', 'data-probe'])
        if manifest['active']:
            self.flow.run(['systemctl', 'start', 'data-probe'])
        self.flow.owner.state.pop('probe_passed', None)
        self.flow.owner.state.pop('probe_replaced', None)
        self.flow.owner.state.pop('probe_config_preserved', None)
        self.status('rolled_back')

    @classmethod
    def recover(cls, probe):
        record = probe.flow.owner.state.get('private', {}).get('probe_replacement', {})
        if record.get('status') not in ('prepared', 'replacing', 'activating'):
            return
        manager = cls(probe)
        manager.record = record
        if record['status'] == 'prepared':
            manager.status('rolled_back')
        else:
            manager.restore()
        probe.flow.owner.event('已恢复上次中断前的探针程序、服务及注册配置')
