"""Durable, scoped port takeover with private backups and per-scope recovery."""
import errno
import hashlib
import json
import os
import pwd
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import time

from common import atomic_json, no_symlink_parents, sha256
from storage import trusted_directory

PROTECTED_UNITS = {'ssh.service', 'sshd.service', 'docker.service', 'containerd.service',
                   'NetworkManager.service', 'systemd-networkd.service', 'data-probe.service'}
UNIT = re.compile(r'[A-Za-z0-9_@.\\:-]+\.(?:service|socket|timer|path)\Z')


def tree_manifest(path):
    path = Path(path)
    result = []
    for p in ([path] if not path.is_dir() else [path, *sorted(path.rglob('*'))]):
        s = p.lstat()
        row = {'path': '.' if p == path else p.relative_to(path).as_posix(),
               'mode': s.st_mode & 0o7777, 'uid': s.st_uid, 'gid': s.st_gid}
        if p.is_symlink(): row.update(kind='link', target=os.readlink(p))
        elif p.is_file(): row.update(kind='file', bytes=s.st_size, sha256=sha256(p))
        elif p.is_dir(): row.update(kind='directory')
        else: raise RuntimeError('备份目录含不支持的特殊文件：' + str(p))
        result.append(row)
    return result


def free_ports(ports):
    for port in ports:
        for family, address in ((socket.AF_INET, '0.0.0.0'), (socket.AF_INET6, '::')):
            try:
                with socket.socket(family, socket.SOCK_STREAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    if family == socket.AF_INET6: sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    sock.bind((address, port))
            except OSError as exc:
                if exc.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL): continue
                if exc.errno == errno.EADDRINUSE: return False
                raise
    return True


def listeners(text, ports):
    result = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 5: continue
        try: port = int(fields[3].rsplit(':', 1)[1])
        except ValueError: continue
        if port in ports:
            result.append((port, {int(x) for x in re.findall(r'pid=(\d+)', line)}))
    return result


class Takeover:
    def __init__(self, flow, scope):
        if not re.fullmatch('[a-z0-9-]+', scope): raise ValueError('Invalid takeover scope')
        self.flow = flow
        self.owner = flow.owner
        self.scope = scope
        state = self.owner.state.setdefault('private', {}).setdefault('port_takeover', {})
        if not state:
            bulk = getattr(getattr(self.owner, 'storage', None), 'data_root', self.owner.root)
            parent = Path(bulk).parent / 'ai-platform-backups'
            trusted_directory(parent, os.geteuid())
            parent.mkdir(mode=0o700, exist_ok=True)
            parent.chmod(0o700)
            state.update(token=secrets.token_hex(12), backup=str(parent / secrets.token_hex(16)), scopes={})
            self.owner.update(force=True)
        self.state = state
        self.base = Path(state['backup'])
        trusted_directory(self.base, os.geteuid())
        self.base.mkdir(mode=0o700, exist_ok=True)
        self.record = state['scopes'].get(scope)

    def save(self):
        self.state['scopes'][self.scope] = self.record
        self.owner.update(force=True)
        atomic_json(self.base / 'takeover.json', self.state, 0o600)

    def begin(self):
        if self.record and self.record['status'] not in ('rolled-back', 'committed'): return
        previous = self.record
        self.record = {'status': 'prepared', 'attempt': secrets.token_hex(8),
                       'paths': {}, 'units': {}, 'containers': {}, 'processes': {}, 'created': [], 'images': {}}
        if previous: self.record['previous_attempt'] = previous
        self.save()

    def phase(self, status):
        self.record['status'] = status
        self.save()

    def run(self, *args, **kwargs): return self.flow.run(list(args), **kwargs)

    def archive(self, path):
        """Persist before deleting. A retry never overwrites a verified backup."""
        path = Path(path)
        no_symlink_parents(path, Path('/'))
        if not path.is_absolute() or path == Path('/') or path.is_symlink() or path.is_mount():
            raise RuntimeError('备份目标不能是链接、挂载点或根目录：' + str(path))
        if self.base.is_relative_to(path) or path.is_relative_to(self.base): raise RuntimeError('备份目录重叠')
        key = str(path)
        row = self.record['paths'].get(key)
        if row is None:
            if not path.exists(): return
            # Nested mounts cannot be copied/deleted as ordinary database files.
            mounts = self.run('findmnt', '-rn', '-o', 'TARGET').stdout.splitlines()
            if any(Path(p).is_relative_to(path) for p in mounts): raise RuntimeError('数据目录含挂载点：' + key)
            folder = self.base / self.scope / self.record['attempt']
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            slot = str(len(self.record['paths']))
            manifest = tree_manifest(path)
            row = {'backup': str(folder / slot), 'manifest': manifest, 'phase': 'planned'}
            self.record['paths'][key] = row
            self.save()
        target = Path(row['backup'])
        if row['phase'] == 'planned':
            temporary = target.with_name(target.name + '.partial')
            if not target.exists():
                if temporary.exists():
                    if temporary.is_dir(): shutil.rmtree(temporary)
                    else: temporary.unlink()
                size = sum(x.get('bytes', 0) for x in row['manifest'])
                if shutil.disk_usage(self.base).free < size + 1024**3: raise RuntimeError('旧数据备份空间不足')
                self.run('cp', '-a', '--reflink=auto', '--sparse=always', '--', path, temporary, timeout=1800)
                if tree_manifest(temporary) != row['manifest']: raise RuntimeError('旧数据备份校验失败')
                temporary.rename(target)
            if tree_manifest(target) != row['manifest']: raise RuntimeError('旧数据备份内容改变')
            row['phase'] = 'copied';self.save()
        if row['phase'] == 'copied':
            if tree_manifest(target) != row['manifest']: raise RuntimeError('旧数据备份内容改变')
            if path.is_dir(): shutil.rmtree(path)
            elif path.exists(): path.unlink()
            row['phase'] = 'archived';self.save()
            self.owner.event('已备份并隔离旧数据或配置：' + str(path))

    def unitctl(self, uid, *args, **kwargs):
        if uid is None: return self.run('systemctl', *args, **kwargs)
        return self.run('runuser', '-u', pwd.getpwuid(uid).pw_name, '--', 'env',
                        'XDG_RUNTIME_DIR=/run/user/' + str(uid),
                        'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/' + str(uid) + '/bus',
                        'systemctl', '--user', *args, **kwargs)

    def stop_unit(self, name, uid=None):
        if not UNIT.fullmatch(name) or name in PROTECTED_UNITS: raise RuntimeError('端口关联到基础系统服务：' + name)
        key = name if uid is None else str(uid) + ':' + name
        override = (Path('/etc/systemd/system') if uid is None else Path(pwd.getpwuid(uid).pw_dir) / '.config/systemd/user') / name
        row = self.record['units'].get(key)
        if row is None:
            props = self.unitctl(uid, 'show', name, '-p', 'LoadState', '-p', 'ActiveState',
                             '-p', 'UnitFileState', '-p', 'FragmentPath', '-p', 'TriggeredBy').stdout
            data = dict(x.split('=', 1) for x in props.splitlines() if '=' in x)
            row = {'active': data.get('ActiveState') == 'active', 'enabled': data.get('UnitFileState', ''),
                   'fragment': data.get('FragmentPath'), 'phase': 'planned', 'released': False, 'name': name, 'uid': uid,
                   'original_link': os.readlink(override) if override.is_symlink() else None}
            self.record['units'][key] = row;self.save()
            for trigger in data.get('TriggeredBy', '').split(): self.stop_unit(trigger, uid)
        if row['phase'] == 'stopped': return
        self.unitctl(uid, 'stop', name, timeout=180, check=False)
        self.unitctl(uid, 'disable', name, check=False)
        row['override'] = str(override);self.save()
        if override.is_symlink():
            if os.readlink(override) != '/dev/null':
                override.unlink()
        elif override.exists(): self.archive(override)
        dropins = override.with_name(name + '.d')
        if dropins.exists(): self.archive(dropins)
        self.unitctl(uid, 'mask', name)
        row['phase'] = 'stopped';self.save()
        self.owner.event('已停用旧服务及自动启动入口：' + name)

    def release_unit(self, name):
        row = self.record['units'].get(name)
        if row:
            self.run('systemctl', 'unmask', name)
            row['released'] = True;self.save()

    def containers(self):
        ids = self.run('docker', 'ps', '-aq', '--no-trunc').stdout.split()
        return json.loads(self.run('docker', 'inspect', *ids).stdout) if ids else []

    def stop_container(self, info):
        identifier = info['Id']
        if not re.fullmatch('[0-9a-f]{64}', identifier): raise RuntimeError('无效容器 ID')
        row = self.record['containers'].get(identifier)
        if row is None:
            row = {'inspect': json.loads(json.dumps(info)), 'phase': 'planned',
                   'archive_name': 'ai-platform-old-' + info['Name'].strip('/')[:80] + '-' + self.record['attempt']}
            self.record['containers'][identifier] = row;self.save()
        if row['phase'] == 'stopped': return
        self.run('docker', 'update', '--restart=no', identifier)
        self.run('docker', 'stop', '--time=30', identifier, timeout=90)
        self.run('docker', 'rename', identifier, row['archive_name'])
        row['phase'] = 'stopped';self.save()
        self.owner.event('已停用并归档旧容器：' + info['Name'].strip('/'))

    def stop_pid(self, pid):
        if pid <= 1 or pid == os.getpid(): raise RuntimeError('无法终止系统端口持有者')
        proc = Path('/proc') / str(pid)
        try:
            identity = (proc / 'stat').read_text().rpartition(')')[2].split()[19]
            groups = (proc / 'cgroup').read_text()
        except FileNotFoundError: return
        for info in self.containers():
            if info['Id'] in groups:
                self.stop_container(info);return
        units = re.findall(r'/([^/\n]+\.service)(?:/|$)', groups)
        units = [u for u in units if not re.fullmatch(r'user@\d+\.service', u)]
        if units:
            user = re.search(r'/user-(\d+)\.slice/', groups)
            self.stop_unit(units[-1], int(user[1]) if user else None)
            return
        key = str(pid)
        if key not in self.record['processes']:
            self.record['processes'][key] = {'start': identity, 'argv': (proc/'cmdline').read_bytes().decode().strip('\0').split('\0'),
                'environment': (proc/'environ').read_bytes().decode().strip('\0').split('\0'),
                'cwd': os.readlink(proc/'cwd'), 'uid': proc.stat().st_uid,
                'gid': proc.stat().st_gid, 'executable': os.readlink(proc/'exe'), 'exe_sha256': sha256(proc/'exe')}
            self.save()
        # pidfd pins the process identity while TERM/KILL are issued.
        try: fd = os.pidfd_open(pid)
        except ProcessLookupError: return
        try:
            if (proc/'stat').read_text().rpartition(')')[2].split()[19] != identity: return
            signal.pidfd_send_signal(fd, signal.SIGTERM)
            limit = time.monotonic() + 10
            while time.monotonic() < limit:
                try:
                    state = (proc/'stat').read_text().rpartition(')')[2].split()
                    if state[0] == 'Z' or state[19] != identity: return
                except FileNotFoundError: return
                time.sleep(.2)
            signal.pidfd_send_signal(fd, signal.SIGKILL)
        except ProcessLookupError: pass
        finally: os.close(fd)
        self.owner.event('已终止占用安装端口的进程：PID ' + str(pid))

    def claim(self, ports, *, names=(), units=(), owned=()):
        ports = set(ports)
        if any(type(p) != int or not 1 <= p <= 65535 or p in (22, 9090) for p in ports):
            raise RuntimeError('安装端口范围无效')
        owned = set(owned)
        for unit in units: self.stop_unit(unit)
        for info in self.containers():
            published = {int(p['HostPort']) for values in (info['HostConfig'].get('PortBindings') or {}).values()
                         for p in values or [] if str(p.get('HostPort', '')).isdigit()}
            if info['Id'] not in owned and (info['Name'].strip('/') in names or (info['State']['Running'] and published & ports)):
                self.stop_container(info)
        for _ in range(5):
            current = listeners(self.run('ss', '-H', '-ltnp').stdout, ports)
            if not current and free_ports(ports): self.phase('released');return
            for port, pids in current:
                if 1 in pids:
                    sockets = self.run('systemctl', 'list-sockets', '--all', '--no-legend', '--no-pager').stdout
                    matched = False
                    for line in sockets.splitlines():
                        endpoint = line.split()[0] if line.split() else ''
                        if endpoint != str(port) and not endpoint.endswith(':' + str(port)): continue
                        for name in line.split():
                            if name.endswith('.socket') and UNIT.fullmatch(name):
                                triggers = self.run('systemctl','show',name,'-p','Triggers','--value').stdout.split()
                                self.stop_unit(name)
                                for trigger in triggers: self.stop_unit(trigger)
                                matched = True
                    if not matched: raise RuntimeError('无法识别持有端口的 systemd socket：' + str(port))
                for pid in pids - {1}: self.stop_pid(pid)
            time.sleep(.3)
        raise RuntimeError('端口占用者仍在重新启动，请查看接管诊断记录：' + ','.join(map(str, sorted(ports))))

    def created(self, info):
        if info['Id'] not in self.record['created']: self.record['created'].append(info['Id']);self.save()

    def commit(self): self.phase('committed')

    def rollback(self):
        self.phase('rolling-back')
        for identifier in self.record['created']:
            self.run('docker', 'rm', '-f', identifier, check=False)
        for key, row in self.record['units'].items():
            name=row['name'];uid=row['uid']
            if row['released']:
                self.unitctl(uid, 'stop', name, check=False)
                self.unitctl(uid, 'disable', name, check=False)
                target = Path(row['override'])
                if target.is_file() and not target.is_symlink(): target.unlink()
            self.unitctl(uid, 'unmask', name, check=False)
        for value, row in reversed(list(self.record['paths'].items())):
            source = Path(row['backup']);target = Path(value)
            if row['phase'] == 'planned' and not source.exists(): continue
            if tree_manifest(source) != row['manifest']: raise RuntimeError('回滚备份校验失败')
            no_symlink_parents(target, Path('/'))
            if target.is_symlink(): raise RuntimeError('回滚目标是链接')
            if target.is_dir(): shutil.rmtree(target)
            elif target.exists(): target.unlink()
            target.parent.mkdir(parents=True, exist_ok=True)
            self.run('cp', '-a', '--reflink=auto', '--sparse=always', '--', source, target, timeout=1800)
        for key, row in self.record['units'].items():
            if row.get('original_link'):
                target = Path(row['override'])
                if not os.path.lexists(target): target.symlink_to(row['original_link'])
        self.run('systemctl', 'daemon-reload')
        for key, row in self.record['units'].items():
            name=row['name'];uid=row['uid']
            if uid is not None: self.unitctl(uid, 'daemon-reload')
            if row['enabled'].startswith('enabled'): self.unitctl(uid, 'enable', name)
            if row['active']: self.unitctl(uid, 'start', name)
        for identifier, row in self.record['containers'].items():
            info = row['inspect'];name = info['Name'].strip('/')
            actual = json.loads(self.run('docker', 'inspect', identifier).stdout)[0]
            if actual['Name'].strip('/') != name: self.run('docker', 'rename', identifier, name)
            policy = info['HostConfig']['RestartPolicy']
            restart = policy['Name'] or 'no'
            if restart == 'on-failure' and policy.get('MaximumRetryCount'): restart += ':' + str(policy['MaximumRetryCount'])
            self.run('docker', 'update', '--restart=' + restart, identifier)
            if info['State']['Running']: self.run('docker', 'start', identifier)
        for tag, identifier in self.record.get('images', {}).items(): self.run('docker', 'tag', identifier, tag)
        for row in self.record['processes'].values():
            argv = row['argv']
            executable = row['executable']
            if not argv or not Path(executable).is_file() or sha256(executable) != row['exe_sha256']: continue
            env = dict(x.split('=', 1) for x in row['environment'] if '=' in x)
            subprocess.Popen(argv, executable=executable, cwd=row['cwd'], env=env, user=row['uid'], group=row['gid'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.phase('rolled-back')
        self.owner.event('已恢复本组件接管前的旧服务和数据；修复原因后可继续安装')
