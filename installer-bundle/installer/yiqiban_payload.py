"""Verify the separately signed YiQiBan application resource release."""
from pathlib import Path
import hashlib
import json
import re
import subprocess
import tarfile
from common import no_symlink_parents, read_json, safe_relative, sha256

DIRECTORY = 'yiqiban'


def saved_image_info(path, tag):
    """Derive config identity from the already authenticated Docker archive."""
    with tarfile.open(path) as archive:
        member = archive.getmember('manifest.json')
        if not member.isfile() or member.size > 1024**2:raise RuntimeError('Docker 镜像清单无效')
        rows = json.load(archive.extractfile(member))
        selected = [r for r in rows if tag in (r.get('RepoTags') or [])]
        if len(selected) != 1:raise RuntimeError('Docker 镜像标签不唯一')
        relative = safe_relative(selected[0]['Config']);config = archive.getmember(relative.as_posix())
        if not config.isfile() or config.size > 16 * 1024**2:raise RuntimeError('Docker 镜像配置无效')
        raw = archive.extractfile(config).read();data = json.loads(raw)
        if data['architecture'] != 'arm64' or data['os'] != 'linux':raise RuntimeError('翼起办镜像架构不符合要求')
        return {'config_id': 'sha256:' + hashlib.sha256(raw).hexdigest(), 'rootfs_diff_ids': data['rootfs']['diff_ids']}


def load(bundle, *, verify_files=False):
    bundle = Path(bundle);metadata = read_json(bundle / 'release.json');base = bundle / DIRECTORY
    for path in [base / 'manifest.json', base / 'manifest.sig', bundle / 'catalog/release-public.pem']:
        no_symlink_parents(path, bundle)
        if path.is_symlink() or not path.is_file():raise RuntimeError('翼起办资源签名缺失')
    key = bundle / 'catalog/release-public.pem'
    if sha256(key) != metadata['catalog_public_key_sha256']:raise RuntimeError('翼起办发布公钥不受信任')
    result = subprocess.run(['openssl', 'pkeyutl', '-verify', '-pubin', '-inkey', str(key), '-rawin',
                             '-in', str(base / 'manifest.json'), '-sigfile', str(base / 'manifest.sig')], capture_output=True, timeout=30)
    if result.returncode:raise RuntimeError('翼起办资源签名无效')
    manifest = read_json(base / 'manifest.json')
    if (manifest.get('schema') != 1 or manifest.get('release_id') != metadata.get('yiqiban_resource_id')
            or manifest.get('platform') != 'linux/arm64' or len(manifest.get('images', [])) != 10):
        raise RuntimeError('翼起办资源发行范围不一致')
    paths = set()
    for row in manifest['files']:
        relative = safe_relative(row['path']);path = base / 'files' / relative
        if row['path'] in paths or not re.fullmatch('[a-f0-9]{64}', row['sha256']) or type(row['size']) is not int:
            raise RuntimeError('翼起办文件清单无效')
        paths.add(row['path']);no_symlink_parents(path, base)
        if path.is_symlink() or not path.is_file() or path.stat().st_size != row['size']:
            raise RuntimeError('翼起办文件缺失或大小错误：' + row['path'])
        if verify_files and sha256(path) != row['sha256']:raise RuntimeError('翼起办文件摘要错误：' + row['path'])
    for image in manifest['images']:
        if (image['archive'] not in paths or not re.fullmatch(r'sha256:[a-f0-9]{64}', image['id'])
                or image['service'] in {'mysql', 'redis', 'paradedb'}):raise RuntimeError('翼起办镜像清单错误')
    return manifest
