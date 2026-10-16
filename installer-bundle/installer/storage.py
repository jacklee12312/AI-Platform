"""Persistent bulk storage selection; task metadata stays in the system private directory."""
import hashlib
import os
from pathlib import Path
import shutil
import stat

from common import atomic_json, no_symlink_parents, read_json

CANDIDATES = (Path('/opt/ai-platform-data'), Path('/home/.ai-platform-data'),
              Path('/data/ai-platform-installer'), Path('/var/lib/ai-platform-data'))
WORK_RESERVE = 8 * 1024**3
METADATA_RESERVE = 1024**3


def existing(path):
    path = Path(path)
    while not path.exists():path = path.parent
    return path


def trusted_directory(path, uid=0):
    path = Path(path)
    if not path.is_absolute():raise RuntimeError('安装工作目录必须为绝对路径')
    no_symlink_parents(path, Path('/'))
    if path.is_symlink():raise RuntimeError('安装工作目录不能是符号链接')
    for parent in (path, *path.parents):
        if parent.exists():
            value = parent.stat()
            if not parent.is_dir() or value.st_uid not in (0, uid):
                raise RuntimeError('安装工作目录必须由管理员拥有')
            if value.st_mode & 0o022 and not value.st_mode & stat.S_ISVTX:
                raise RuntimeError('安装工作目录不能由其他用户写入')


def default_data_root(candidates=None):
    """Read-only selection also used by the GUI; ties keep the stable candidate order."""
    choices = [];devices = set()
    for path in CANDIDATES if candidates is None else candidates:
        try:
            trusted_directory(path)
            device = existing(path).stat().st_dev
            if device in devices:continue
            devices.add(device)
            choices.append((shutil.disk_usage(existing(path)).free, Path(path)))
        except (OSError, RuntimeError):continue
    if not choices:raise RuntimeError('未找到安全可用的大容量安装工作目录')
    return max(enumerate(choices), key=lambda item: (item[1][0], -item[0]))[1][1]


def default_cache():
    return default_data_root() / 'cache' if os.name == 'posix' else Path('/var/cache/ai-platform')


class Storage:
    def __init__(self, state_root, release, data_root=None):
        self.state_root = Path(state_root)
        self.record = self.state_root / 'private/storage.json'
        no_symlink_parents(self.record, self.state_root)
        if self.record.is_symlink():raise RuntimeError('安装工作目录记录不能是符号链接')
        saved = read_json(self.record, {})
        if saved and saved.get('schema') != 1:raise RuntimeError('安装工作目录记录格式错误')
        self.data_root = Path(saved['data_root']) if saved else Path(data_root or default_data_root())
        trusted_directory(self.data_root, os.geteuid())
        identity = str(self.state_root.absolute()) + '\0' + release['release_id'] + '\0' + release.get('installer_version', 'legacy')
        token = hashlib.sha256(identity.encode()).hexdigest()[:24]
        self.cache = self.data_root / 'cache'
        self.work = self.data_root / 'work' / token
        self.resources = self.work / 'resources'
        self.runtime = self.work / 'runtime'
        self.workflow = self.work / 'workflow'

    def ensure(self):
        for path, mode in ((self.data_root, 0o755), (self.cache, 0o755),
                           (self.data_root / 'work', 0o700), (self.work, 0o700)):
            trusted_directory(path, os.geteuid())
            path.mkdir(parents=True, exist_ok=True, mode=mode)
            path.chmod(mode)
        no_symlink_parents(self.record, self.state_root)
        atomic_json(self.record, {'schema': 1, 'data_root': str(self.data_root),
                                 'cache': str(self.cache), 'work': str(self.work)})


def budgets(entries):
    """Sum only reservations on each actual filesystem, even across bind mounts."""
    result = {}
    for path, need, purpose in entries:
        p = existing(path);device = p.stat().st_dev
        row = result.setdefault(device, {'path': str(p), 'need_bytes': 0,
                                        'free_bytes': shutil.disk_usage(p).free, 'purposes': []})
        row['need_bytes'] += max(0, int(need));row['purposes'].append(purpose)
    return list(result.values())


def require_space(rows):
    for row in rows:
        if row['free_bytes'] < row['need_bytes']:
            raise RuntimeError(f"{row['path']} 所在分区空间不足：需约 {row['need_bytes']/1e9:.1f} GB，"
                               f"可用 {row['free_bytes']/1e9:.1f} GB（{'、'.join(row['purposes'])}）")
