"""Place persistent container stores before installation, with verified, resumable copies.

Bind mounts keep Docker volume paths and containerd snapshot paths unchanged. The
original trees remain under the mounts. After activation the new stores are the
authority: recovery never copies the old trees over a store that may have run.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tomllib

from common import atomic_json, read_json
from docker_runtime import DockerRuntime, DOCKER
from storage import budgets, existing, require_space, trusted_directory

RECORD = Path('/var/lib/ai-platform-container-storage/migration.json')
UNITS = Path('/etc/systemd/system')
SERVICES = ('docker.socket', 'docker.service', 'containerd.service')


def layout(run):
    """Use the local daemon and the running containerd's actual CLI/config roots."""
    docker = Path('/var/lib/docker');image_store = 'containerd'
    if shutil.which('docker'):
        result = run(DOCKER + ['info', '--format', '{{json .}}'], check=False, timeout=30)
        if result.returncode == 0:
            info = json.loads(result.stdout);docker = Path(info['DockerRootDir'])
            image_store = 'containerd' if any('io.containerd.snapshotter' in str(x)
                for x in info.get('DriverStatus', [])) else 'docker'
        else:
            config = read_json(Path('/etc/docker/daemon.json'), {})
            docker = Path(config.get('data-root', str(docker)))
    containerd = Path('/var/lib/containerd')
    if shutil.which('containerd'):
        config = '/etc/containerd/config.toml';override = None
        pid = run(['systemctl', 'show', 'containerd.service', '-p', 'MainPID', '--value'], check=False).stdout.strip()
        if pid.isdigit() and int(pid):
            argv = Path('/proc', pid, 'cmdline').read_bytes().decode().strip('\0').split('\0')
            for i, arg in enumerate(argv):
                if arg in ('--config', '-c'):config = argv[i+1]
                elif arg.startswith('--config='):config = arg.split('=', 1)[1]
                elif arg == '--root':override = argv[i+1]
                elif arg.startswith('--root='):override = arg.split('=', 1)[1]
        result = run(['containerd', '--config', config, 'config', 'dump'], timeout=30)
        containerd = Path(override or tomllib.loads(result.stdout)['root'])
    for path in (docker, containerd):
        if not path.is_absolute() or path == Path('/'):raise RuntimeError('容器数据目录必须为有效绝对路径')
        trusted_directory(path)
    stores = {'docker': docker, 'containerd': containerd}
    # Embedded containerd is already included when copying the parent Docker tree.
    roots = [p for p in stores.values() if not any(p != q and p.is_relative_to(q) for q in stores.values())]
    return {'stores': dict.fromkeys(roots), 'docker': docker,
            'images': containerd if image_store == 'containerd' else docker}


def tree_bytes(root):
    """Conservative copy reservation, counting each hard-linked file once."""
    if not root.exists():return 0
    seen = set();total = 0
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in files:
            value = (Path(parent)/name).lstat();identity = (value.st_dev, value.st_ino)
            if stat.S_ISREG(value.st_mode) and identity not in seen:
                total += value.st_size;seen.add(identity)
    return total


def tree_digest(root):
    """Check bytes, owners, modes, mtime, xattrs, symlinks and hard-link relations."""
    digest = hashlib.sha256();links = {};device = root.stat().st_dev
    def visit(path):
        value = path.lstat();relative = path.relative_to(root).as_posix()
        if not path.is_symlink() and value.st_dev != device:raise RuntimeError('容器数据内仍有子挂载，不能安全复制')
        row = [relative, value.st_mode, value.st_uid, value.st_gid, value.st_mtime_ns]
        row.append([(key, base64.b64encode(os.getxattr(path, key, follow_symlinks=False)).decode())
                    for key in sorted(os.listxattr(path, follow_symlinks=False))])
        if stat.S_ISREG(value.st_mode):
            identity = (value.st_dev, value.st_ino)
            row.extend([value.st_size, links.setdefault(identity, relative)])
            with path.open('rb') as stream:row.append(hashlib.file_digest(stream, 'sha256').hexdigest())
        elif stat.S_ISLNK(value.st_mode):row.append(os.readlink(path))
        elif not stat.S_ISDIR(value.st_mode):row.append(value.st_rdev)
        digest.update(json.dumps(row, ensure_ascii=True, separators=(',', ':')).encode()+b'\n')
        if stat.S_ISDIR(value.st_mode):
            for child in sorted(path.iterdir()):visit(child)
    visit(root)
    return digest.hexdigest()


class ContainerStorage:
    def __init__(self, owner, record=RECORD, units=UNITS):
        self.owner = owner;self.flow = owner.workflow;self.run = self.flow.run
        self.runtime = DockerRuntime(self.flow);self.record_path = Path(record);self.units_dir = Path(units)
        trusted_directory(self.record_path.parent)
        if self.record_path.is_symlink():raise RuntimeError('容器存储迁移记录不能是链接')
        if self.record_path.exists():
            value=self.record_path.stat()
            if value.st_uid!=0 or value.st_mode & 0o077:raise RuntimeError('容器存储迁移记录必须仅管理员可读写')
        self.record = read_json(self.record_path, {})

    def save(self, phase):
        self.record['phase'] = phase
        self.record_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_json(self.record_path, self.record)
        self.owner.state.setdefault('private', {})['container_storage'] = {
            'record': str(self.record_path), 'phase': phase, 'target': self.record['base']}
        self.owner.update(force=True)

    def paths(self):
        result = []
        for row in self.record['moves']:
            source = Path(row['source']);target = Path(row['target']);base = Path(self.record['base'])
            trusted_directory(source);trusted_directory(target)
            if not source.is_absolute() or source == Path('/') or target.parent != base:
                raise RuntimeError('容器存储迁移记录路径不合法')
            if '/' in row['unit'] or not row['unit'].endswith('.mount'):
                raise RuntimeError('容器存储挂载单元记录不合法')
            if source == target or source.is_relative_to(base) or base.is_relative_to(source):
                raise RuntimeError('容器存储迁移源与目标重叠')
            result.append((source, target, row['unit']))
        return result

    def mounted(self):
        return all(os.path.ismount(src) and os.path.samefile(src, dst) for src, dst, _ in self.paths())

    def stop(self):
        current = self.runtime.inventory()
        for identifier in current['paused']:self.run(DOCKER + ['unpause', identifier])
        if current['running']:self.run(DOCKER + ['stop', '--time=60', *current['running']], timeout=240)
        for service in SERVICES:
            if self.record['services'][service]['loaded']:self.run(['systemctl', 'stop', service], timeout=120)

    def restore(self):
        for service in reversed(SERVICES):
            if self.record['services'][service]['active']:self.run(['systemctl', 'start', service], timeout=180)
        before = self.record['inventory']
        if before['data_root']:
            # The daemon was accessible when the inventory was captured.
            self.runtime.record = before;self.runtime.restore_containers()

    def unit_files(self):
        # What/Where/AssertPathIsMountPoint are scalar path settings, not shell
        # arguments. Quoting them makes systemd ignore Where on Debian 12.
        def quote(value):
            text=str(value)
            if any(ch.isspace() or ch in '\"\'\\' or ord(ch)<32 for ch in text):
                raise RuntimeError('容器存储挂载路径不能包含空白、引号或反斜杠')
            return text.replace('%', '%%')
        files = {};mounts = [unit for _, _, unit in self.paths()]
        for src, dst, unit in self.paths():
            files[self.units_dir/unit] = ('[Unit]\nDescription=AI Platform persistent container storage\n'
                'RequiresMountsFor='+quote(dst)+'\nAssertPathIsMountPoint='+quote(self.record['anchor'])+
                '\nBefore=docker.service docker.socket containerd.service\n\n[Mount]\nWhat='+quote(dst)+
                '\nWhere='+quote(src)+'\nType=none\nOptions=bind\n\n[Install]\nWantedBy=local-fs.target\n')
        for service in SERVICES:
            files[self.units_dir/(service+'.d')/'50-ai-platform-storage.conf'] = (
                '[Unit]\nRequires='+' '.join(mounts)+'\nAfter='+' '.join(mounts)+'\n')
        return files

    def activate(self):
        # This phase is persisted BEFORE writing boot configuration. From here on
        # the verified new trees must never be overwritten by the old source trees.
        self.save('activating')
        for path, content in self.unit_files().items():
            trusted_directory(path.parent)
            if path.is_symlink() or (path.exists() and path.read_text() != content):
                raise RuntimeError('容器存储 systemd 配置冲突，保留数据等待处理')
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name+'.ai-platform-tmp')
            if temporary.is_symlink():raise RuntimeError('容器存储配置暂存文件不能是链接')
            with temporary.open('w') as stream:
                stream.write(content);stream.flush();os.fsync(stream.fileno())
            temporary.chmod(0o644);os.replace(temporary, path)
        verified=self.run(['systemd-analyze','verify',*[str(self.units_dir/u) for _,_,u in self.paths()]], timeout=60)
        if 'ignoring:' in verified.stderr.lower():raise RuntimeError('容器存储挂载配置解析失败，保留新数据供继续处理')
        self.run(['systemctl', 'daemon-reload'])
        mounts = [unit for _, _, unit in self.paths()]
        self.run(['systemctl', 'enable', *mounts])
        self.run(['systemctl', 'start', *mounts], timeout=180)
        if not self.mounted():raise RuntimeError('容器存储挂载验证失败；新数据保留，继续安装会重试')
        self.restore();self.save('committed')
        self.owner.event('Docker 与 containerd 持久存储已迁入大容量分区，原容器与数据卷检查通过')

    def recover(self):
        if not self.record:return
        if self.record.get('schema') != 1:raise RuntimeError('容器存储迁移记录格式错误')
        if self.record['phase'] == 'committed':
            if not self.mounted():
                # Normal boot is ordered by the persisted mount dependencies. Do
                # not hide a daemon that was manually started against an old tree.
                if any(self.run(['systemctl', 'is-active', '--quiet', x], check=False).returncode == 0 for x in SERVICES):
                    raise RuntimeError('容器存储挂载未就绪且旧服务已运行，不能混用两份数据')
                self.activate()
        elif self.record['phase'] == 'activating':self.activate()
        else:self.copy_and_activate()

    def copy_and_activate(self):
        try:
            require_space(budgets([(Path(self.record['base']),
                sum(tree_bytes(src) for src, _, _ in self.paths())+1024**3, '原容器数据迁移副本')]))
            self.save('copying');self.owner.update('正在迁移 Docker 与 containerd 存储到大容量分区', force=True)
            self.stop()
            nested = self.run(['findmnt', '-rn', '-o', 'TARGET']).stdout.splitlines()
            for src, dst, _ in self.paths():
                if any((Path(p) != src and Path(p).is_relative_to(src)) or Path(p).is_relative_to(dst) for p in nested):
                    raise RuntimeError('容器存储仍有子挂载；原数据保留，请停止相应挂载后重试')
                if os.path.ismount(src):raise RuntimeError('容器存储原目录是独立挂载，不能自动覆盖挂载')
                if dst.exists():
                    # Only our unactivated, recorded partial copy may be replaced.
                    if os.path.ismount(dst):raise RuntimeError('迁移目标不能是其他挂载')
                    shutil.rmtree(dst)
                src.mkdir(parents=True, exist_ok=True);dst.mkdir(parents=True)
                self.run(['cp', '-a', '--reflink=auto', '--sparse=always', '--one-file-system', str(src)+'/.', str(dst)], timeout=3600)
                self.owner.update('正在校验容器存储迁移数据：'+str(src), force=True)
                if tree_digest(src) != tree_digest(dst):raise RuntimeError('容器存储迁移副本校验失败，原数据保留')
                self.run(['sync', '-f', str(dst)], timeout=300)
            self.save('activating')
        except Exception:
            self.restore()
            raise
        self.activate()

    def prepare(self, entries, detected):
        if self.record:
            self.recover()
            if self.record['phase'] == 'committed':return
        rows = budgets(entries)
        failing = {existing(Path(r['path'])).stat().st_dev for r in rows if r['free_bytes'] < r['need_bytes']}
        target_device = existing(self.owner.storage.data_root).stat().st_dev
        sources = [p for p in detected['stores'] if existing(p).stat().st_dev in failing
                   and existing(p).stat().st_dev != target_device]
        if not sources:return
        base = self.owner.storage.data_root.parent/'ai-platform-container-storage'
        if existing(base).stat().st_dev != target_device:base = self.owner.storage.data_root/'container-storage'
        trusted_directory(base)
        if base.exists():raise RuntimeError('容器存储目标已存在但无受信任迁移记录，不能覆盖：'+str(base))
        destinations = {p:base/('store-'+str(i)) for i,p in enumerate(sources)}
        def projected(path):
            for src, dst in destinations.items():
                if path == src or path.is_relative_to(src):return dst/path.relative_to(src)
            return path
        estimated = sum(tree_bytes(p) for p in sources)
        require_space(budgets([(projected(Path(p)), n, purpose) for p,n,purpose in entries]+
                              [(base, estimated+1024**3, '原容器数据迁移副本')]))
        mount = json.loads(self.run(['findmnt', '-J', '-T', str(existing(base)), '-o', 'TARGET,FSTYPE']).stdout)['filesystems'][0]
        if mount['fstype'] not in ('ext4', 'xfs'):
            raise RuntimeError('容器存储需要本机 ext4 或 XFS 大容量分区')
        moves = []
        for src, dst in destinations.items():
            unit = self.run(['systemd-escape', '--path', '--suffix=mount', str(src)]).stdout.strip()
            if '/' in unit or not unit.endswith('.mount'):raise RuntimeError('容器存储挂载单元名称无效')
            moves.append({'source': str(src), 'target': str(dst), 'unit': unit})
        services = {}
        for name in SERVICES:
            loaded = self.run(['systemctl', 'show', name, '-p', 'LoadState', '--value'], check=False).stdout.strip()
            services[name] = {'loaded': loaded not in ('', 'not-found'),
                             'active': self.run(['systemctl', 'is-active', '--quiet', name], check=False).returncode == 0}
        if shutil.which('ctr') and services['containerd.service']['active']:
            namespaces = self.run(['ctr', 'namespaces', 'list', '-q']).stdout.split()
            if set(namespaces)-{'moby'}:raise RuntimeError('containerd 同时被其他系统使用，不能自动迁移其共享存储')
        self.record = {'schema': 1, 'phase': 'prepared', 'base': str(base), 'anchor': mount['target'],
                       'moves': moves, 'services': services, 'inventory': self.runtime.inventory()}
        # Refuse configuration collisions before stopping any service.
        for path in self.unit_files():
            if path.exists() or path.is_symlink():raise RuntimeError('已有容器挂载配置，不能自动覆盖：'+str(path))
        base.mkdir(parents=True, mode=0o700);self.save('prepared');self.copy_and_activate()
