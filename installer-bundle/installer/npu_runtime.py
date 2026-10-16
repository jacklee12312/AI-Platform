"""Render local Kokoro driver settings without changing signed release resources.

The container image stays pinned. Only a verified copy of this device's CIX
driver userspace is mounted into it; the original payload and repair provenance
remain available for conflict checks and later resumes.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

from common import atomic_json, atomic_text, no_symlink_parents, read_json, sha256

RELATIVE = 'payload/home/cix/kokoro-cix/docker-compose.yml'
IMAGE = 'kokoro-cix:0.2.0'
WHEEL = Path('/usr/share/cix/pypi/libnoe-3.1.2-py3-none-manylinux2014_aarch64.whl')
LIBRARY = Path('/usr/share/cix/lib/libnoe.so.3.1.0')
BACKEND = Path('/usr/share/cix/lib/onnxruntime/libaipu_driver.so')


def trusted(path):
    path = Path(path)
    no_symlink_parents(path, Path('/'))
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('NPU 适配文件不是普通文件：'+str(path))
    if path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o022:
        raise RuntimeError('NPU 适配文件所有者或权限不可信：'+str(path))
    for parent in path.parents:
        if parent.stat().st_uid != os.geteuid() or parent.stat().st_mode & 0o022:
            raise RuntimeError('NPU 适配目录所有者或权限不可信：'+str(parent))
    return path


def effective_compose(resources, root):
    record_path = Path(root)/'private/kokoro-runtime.json'
    if not record_path.exists():
        return Path(resources)/RELATIVE
    record = read_json(trusted(record_path))
    base = Path(resources)/RELATIVE
    generated = Path(root)/'private/local-configs/kokoro-compose.yml'
    if record.get('base_sha256') != sha256(base) or record.get('compose_sha256') != sha256(trusted(generated)):
        raise RuntimeError('Kokoro 本机适配与签名基础配置不一致')
    for name, digest in record['files'].items():
        path = Path(name)
        if not path.is_relative_to(Path(root)/'host-runtime/kokoro') or sha256(trusted(path)) != digest:
            raise RuntimeError('Kokoro 本机 NPU 运行库校验失败')
    return generated


def apply_local(original, generated, target, home):
    no_symlink_parents(target, home)
    if target.is_symlink() or not target.is_file():
        raise RuntimeError('Kokoro 本机配置路径不安全')
    if target.read_bytes() == generated.read_bytes():return
    if target.read_bytes() != original.read_bytes():
        raise RuntimeError('Kokoro 已有配置不同，不能自动应用 NPU 适配')
    atomic_text(target, generated.read_text(), 0o644)


def render(base, runtime):
    if base.count('    volumes:\n') != 1 or base.count('    environment:\n') != 1:
        raise RuntimeError('Kokoro 签名基础配置结构无法识别')
    base = base.replace('    volumes:\n', '    volumes:\n'
                        f'    - {runtime}/lib:/opt/cix-host-lib:ro\n'
                        f'    - {runtime}/libnoe:/usr/local/lib/python3.11/site-packages/libnoe:ro\n')
    return base.replace('    environment:\n', '    environment:\n'
                        '      LD_LIBRARY_PATH: /opt/cix-host-lib:/usr/local/lib\n'
                        '      AIPU_LIB_PATH: /opt/cix-host-lib/libaipu_driver.so\n')


def probe(extra=()):
    code = 'import os; from libnoe import NPU; n=NPU(); status=n.noe_init_context(); print(status,flush=True); os._exit(0 if status==0 else 1)'
    return subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--device', '/dev/aipu',
                           *extra, '--entrypoint', 'python', IMAGE, '-c', code],
                          capture_output=True, text=True, timeout=45)


def configure(installer):
    if 'kokoro-82m' not in installer.selected:
        return
    root = installer.root
    original = installer.resources/RELATIVE
    target = installer.home/'kokoro-cix/docker-compose.yml'
    current = effective_compose(installer.resources, root)
    if current != original:
        apply_local(original,current,target,installer.home)
        installer.event('复用已校验的 Kokoro 本机 NPU 运行库适配')
        return
    if probe().returncode == 0:
        return
    version = subprocess.check_output(['dpkg-query', '-W', '-f=${Version}', 'cix-noe-umd'], text=True).strip()
    if version != '3.1.2':
        raise RuntimeError('Kokoro NPU 初始化失败，当前主机运行库尚未适配：'+version)
    for package in ['cix-noe-umd', 'cix-npu-onnxruntime']:
        check = subprocess.run(['dpkg', '--verify', package], capture_output=True, text=True, timeout=45)
        if check.returncode or check.stdout.strip():
            raise RuntimeError('主机 NPU 软件包完整性检查失败：'+package)
    inputs = [trusted(p) for p in [WHEEL, LIBRARY, BACKEND]]
    runtime = root/'host-runtime/kokoro'
    no_symlink_parents(runtime/'placeholder', Path('/'))
    runtime.mkdir(parents=True, exist_ok=True, mode=0o755)
    for folder in [root/'host-runtime', runtime, runtime/'lib', runtime/'libnoe']:
        folder.mkdir(exist_ok=True);folder.chmod(0o755)
    contents = {'lib/libnoe.so.3': LIBRARY.read_bytes(), 'lib/libnoe.so': LIBRARY.read_bytes(),
                'lib/libaipu_driver.so': BACKEND.read_bytes()}
    with zipfile.ZipFile(WHEEL) as wheel:
        for name in ['libnoe/__init__.py', 'libnoe/libnoe.cpython-311-aarch64-linux-gnu.so']:
            contents[name] = wheel.read(name)
    files = {}
    for name, data in contents.items():
        path = runtime/name
        if path.exists() and trusted(path).read_bytes() != data:
            raise RuntimeError('已有 NPU 运行库快照不同，不覆盖：'+str(path))
        if not path.exists():
            with path.open('xb') as out: out.write(data)
            path.chmod(0o644)
        files[str(path)] = sha256(path)
    check = probe(['-v', str(runtime/'lib')+':/opt/cix-host-lib:ro',
                   '-v', str(runtime/'libnoe')+':/usr/local/lib/python3.11/site-packages/libnoe:ro',
                   '-e', 'LD_LIBRARY_PATH=/opt/cix-host-lib:/usr/local/lib',
                   '-e', 'AIPU_LIB_PATH=/opt/cix-host-lib/libaipu_driver.so'])
    if check.returncode:
        raise RuntimeError('主机配套 NPU 运行库仍无法初始化：'+check.stderr[-1000:])
    no_symlink_parents(target, installer.home)
    if target.is_symlink() or target.read_bytes() != original.read_bytes():
        raise RuntimeError('Kokoro 已有配置不同，不能自动应用 NPU 适配')
    local = root/'private/local-configs'
    local.mkdir(exist_ok=True, mode=0o700)
    backup = local/'kokoro-compose.original.yml'
    if not backup.exists(): shutil.copyfile(original, backup)
    generated = local/'kokoro-compose.yml'
    atomic_text(generated, render(original.read_text(), runtime), 0o644)
    record = {'schema':1, 'kind':'local-host-driver-configuration', 'host_noe_version':version,
              'base_sha256':sha256(original), 'compose_sha256':sha256(generated), 'files':files,
              'inputs':{str(p):sha256(p) for p in inputs}, 'image':IMAGE}
    # Record first: an interrupted target write can be recovered by payload().
    atomic_json(root/'private/kokoro-runtime.json', record)
    apply_local(original,generated,target,installer.home)
    installer.event('Kokoro 已适配主机 CIX NPU 3.1.2 运行库；原配置和摘要已保留')
