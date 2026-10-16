"""Signed offline NOE userspace, installed privately without apt or pip."""
import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile

from common import read_json,sha256,no_symlink_parents,safe_relative

RELEASE_ID='cix-noe-runtime-3.1.2-arm64-20261016'
MEMBERS={'lib/libnoe.so','lib/libnoe.so.3','lib/libaipu_driver.so',
         'libnoe/__init__.py','libnoe/libnoe.cpython-311-aarch64-linux-gnu.so'}
LIMIT=32*1024**2


def load(bundle):
    bundle=Path(bundle);base=bundle/'npu-runtime';path=base/'manifest.json'
    for p in [path,base/'manifest.sig',bundle/'catalog/release-public.pem']:
        no_symlink_parents(p,bundle)
        if p.is_symlink() or not p.is_file():raise RuntimeError('NPU 离线资源缺失或路径不安全')
    key=bundle/'catalog/release-public.pem'
    release=read_json(bundle/'release.json')
    if sha256(key)!=release['catalog_public_key_sha256']:raise RuntimeError('NPU 发布公钥不属于本安装包')
    result=subprocess.run(['openssl','pkeyutl','-verify','-pubin','-inkey',str(key),'-rawin',
                           '-in',str(path),'-sigfile',str(base/'manifest.sig')],capture_output=True,timeout=20)
    if result.returncode:raise RuntimeError('NPU 离线资源发行签名验证失败')
    manifest=read_json(path)
    if manifest.get('schema')!=1 or manifest.get('release_id')!=RELEASE_ID or manifest.get('architecture')!='arm64' or manifest.get('container_python')!='3.11':
        raise RuntimeError('NPU 离线资源版本或架构不匹配')
    rows=manifest.get('files',[])
    if len(rows)!=len(MEMBERS) or {r['path'] for r in rows}!=MEMBERS:raise RuntimeError('NPU 离线文件清单不完整')
    for row in rows+[manifest['archive']]:
        if type(row.get('size')) is not int or not 0<row['size']<=LIMIT or not re.fullmatch('[0-9a-f]{64}',row.get('sha256','')):
            raise RuntimeError('NPU 离线资源摘要或大小无效')
    archive=base/safe_relative(manifest['archive']['path'])
    no_symlink_parents(archive,base)
    if archive.is_symlink() or not archive.is_file() or archive.stat().st_size!=manifest['archive']['size'] or sha256(archive)!=manifest['archive']['sha256']:
        raise RuntimeError('NPU 离线资源归档校验失败')
    return manifest,archive


def contents(manifest,archive):
    expected={r['path']:r for r in manifest['files']};result={}
    with tarfile.open(archive,'r:gz') as tar:
        for item in tar:
            if item.name in result or item.name not in expected or not item.isfile() or item.size!=expected[item.name]['size']:
                raise RuntimeError('NPU 归档包含额外、重复或不安全的文件')
            data=tar.extractfile(item).read(item.size+1)
            if len(data)!=item.size or hashlib.sha256(data).hexdigest()!=expected[item.name]['sha256']:
                raise RuntimeError('NPU 归档文件校验失败：'+item.name)
            result[item.name]=data
    if set(result)!=MEMBERS:raise RuntimeError('NPU 归档文件不完整')
    return result


def directory(path):
    path=Path(path);no_symlink_parents(path/'placeholder',Path('/'))
    if not path.exists():path.mkdir(parents=True,mode=0o755)
    for item in [path,*path.parents]:
        if not item.is_dir() or item.stat().st_uid!=os.geteuid() or item.stat().st_mode&0o022:
            raise RuntimeError('NPU 专用运行库目录所有者或权限不可信：'+str(item))


def write(path,data):
    fd,tmp=tempfile.mkstemp(prefix='.npu-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as out:out.write(data);out.flush();os.fsync(out.fileno())
        os.chmod(tmp,0o644);os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def install(bundle,root,event=lambda message:None):
    manifest,archive=load(bundle);data=contents(manifest,archive)
    root=Path(root);runtime=root/'host-runtime/releases'/manifest['release_id']
    for folder in [root/'host-runtime',root/'host-runtime/releases',runtime,runtime/'lib',runtime/'libnoe']:
        directory(folder);folder.chmod(0o755)
    installed=repaired=0
    for row in manifest['files']:
        path=runtime/row['path'];no_symlink_parents(path,runtime)
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_uid!=os.geteuid())):
            raise RuntimeError('NPU 专用运行库文件路径不可信：'+str(path))
        if path.exists() and path.stat().st_mode&0o022:raise RuntimeError('NPU 专用运行库文件权限不可信')
        if path.exists() and path.stat().st_size==row['size'] and sha256(path)==row['sha256']:continue
        if path.exists():
            backup=root/'private/npu-runtime-backups';directory(backup)
            shutil.copyfile(path,backup/(path.name+'.'+secrets.token_hex(8)+'.before-repair'))
            repaired+=1
        else:installed+=1
        write(path,data[row['path']])
    event('离线 NPU 运行库已校验：补齐 '+str(installed)+' 个文件，修复 '+str(repaired)+' 个文件；无需主机预装 NOE 软件包')
    return runtime,manifest
