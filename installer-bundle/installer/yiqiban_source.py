"""Acquire the pinned YiQiBan ZIP without running any publisher upload tools."""
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tarfile
import tempfile
import uuid
import zipfile

from artifacts import BlobStore, blob_path, file_lock, source_config, source_roots
from common import no_symlink_parents, read_json, safe_relative, sha256
from storage import trusted_directory
from yiqiban_payload import load, validate_files


def settings(bundle):
    bundle = Path(bundle);path = bundle / 'yiqiban/source.json'
    no_symlink_parents(path, bundle)
    if path.is_symlink():raise RuntimeError('翼起办来源配置不能是链接')
    spec = read_json(path);row = spec['archive'];source = spec['oci']
    manifest = load(bundle)
    if (spec.get('schema') != 1 or spec['release_id'] != manifest['release_id']
            or spec['manifest_sha256'] != sha256(bundle / 'yiqiban/manifest.json')
            or safe_relative(spec['zip_prefix']).as_posix() + '/' != spec['zip_prefix']
            or source.get('manifest_digest') is None or source['format'] != 'blob'
            or source['blob_digest'] != 'sha256:' + row['sha256'] or source['blob_size'] != row['size']
            or source['files'] != {row['path']: None}):
        raise RuntimeError('翼起办远端版本与签名资源不一致')
    source_config(bundle, acquisition_config(spec, []))
    return spec


def acquisition_config(spec, roots):
    # The model's local-import choice does not disable this separate application source.
    return {'schema': 1, 'mode': 'prefer-local', 'allow_http': True, 'local_roots': roots,
            'timeout_seconds': 45, 'retries': 3, 'oci_artifacts': {'yiqiban': spec['oci']}}


def resource_root(storage, spec):
    return storage.work / ('yiqiban-' + spec['manifest_sha256'])


def reservations(storage, spec, manifest):
    cached = blob_path(storage.cache, spec['archive'])
    no_symlink_parents(cached, storage.cache)
    existing = cached.stat().st_size if cached.is_file() and not cached.is_symlink() else 0
    # Size-only estimates never authorize using a cache entry; ensure() hashes it later.
    download = max(0, spec['archive']['size'] - existing)
    expanded = sum(r['size'] for r in manifest['files']) + spec['templates']['size']
    return [(storage.cache, download, '翼起办 ZIP 下载缓存'),
            (storage.work, expanded, '翼起办签名资源解包')]


def copy_verified(stream, target, row, pause):
    target = Path(target);target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    digest = hashlib.sha256();done = 0
    with target.open('xb') as output:
        while True:
            pause();chunk = stream.read(min(8 * 1024**2, row['size'] - done + 1))
            if not chunk:break
            done += len(chunk)
            if done > row['size']:raise RuntimeError('翼起办资源展开大小超出签名清单')
            digest.update(chunk);output.write(chunk)
        output.flush();os.fsync(output.fileno())
    if done != row['size'] or digest.hexdigest() != row['sha256']:
        raise RuntimeError('翼起办资源内容校验失败：' + row.get('path', 'templates.tar.gz'))
    target.chmod(0o644)


def unpack(bundle, archive, spec, manifest, destination, pause=lambda: None):
    """Only signed images/templates are materialized into a new private directory."""
    bundle = Path(bundle);destination = Path(destination)
    if (Path(archive).is_symlink() or Path(archive).stat().st_size != spec['archive']['size']
            or sha256(archive, lambda n, size: pause()) != spec['archive']['sha256']):
        raise RuntimeError('翼起办 ZIP 与已锁定摘要不符')
    images = {r['archive'] for r in manifest['images']}
    rows = {r['path']: r for r in manifest['files']}
    prefix = spec['zip_prefix'];members = {};expanded = 0
    with zipfile.ZipFile(archive) as zipped:
        for item in zipped.infolist():
            name = safe_relative(item.filename).as_posix()
            if name != item.filename.rstrip('/') or name in members or len(members) >= 2048:
                raise RuntimeError('翼起办 ZIP 包含重复、过多或不规范路径')
            mode = item.external_attr >> 16
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR) or item.flag_bits & 1:
                raise RuntimeError('翼起办 ZIP 包含链接、特殊文件或加密文件')
            members[name] = item;expanded += item.file_size
            if expanded > spec['archive']['size'] + 64 * 1024**2:
                raise RuntimeError('翼起办 ZIP 展开大小超过安全预算')
        for name, local in [('manifest.json', bundle / 'yiqiban/manifest.json'),
                            ('manifest.sig', bundle / 'yiqiban/manifest.sig'),
                            ('release-public.pem', bundle / 'catalog/release-public.pem')]:
            info = members.get(prefix + name)
            if info is None or info.file_size != local.stat().st_size or zipped.read(info) != local.read_bytes():
                raise RuntimeError('翼起办 ZIP 签名或公钥与安装器不一致')
        expected = {prefix + 'files/' + path: row for path, row in rows.items() if path in images}
        expected[prefix + 'templates.tar.gz'] = spec['templates']
        for name, row in expected.items():
            pause();info = members.get(name)
            if info is None or info.is_dir() or info.file_size != row['size']:
                raise RuntimeError('翼起办 ZIP 缺少签名资源或大小不符')
            target = destination / ('templates.tar.gz' if name.endswith('/templates.tar.gz') else 'files/' + row['path'])
            with zipped.open(info) as stream:copy_verified(stream, target, row, pause)
    expected = {path: row for path, row in rows.items() if path not in images};found = set();seen = set();expanded = 0
    with tarfile.open(destination / 'templates.tar.gz', 'r|gz') as tar:
        for item in tar:
            pause();name = safe_relative(item.name).as_posix()
            if name != item.name.rstrip('/') or name in seen or len(seen) >= 4096:
                raise RuntimeError('翼起办模板包含重复、过多或不规范路径')
            seen.add(name)
            if item.isdir():continue
            if not item.isfile() or not name.startswith('files/') or name[6:] not in expected:
                raise RuntimeError('翼起办模板包含未签名文件、链接或特殊文件')
            row = expected[name[6:]];expanded += item.size
            if item.size != row['size'] or expanded > sum(r['size'] for r in expected.values()):
                raise RuntimeError('翼起办模板展开大小不符')
            found.add(name[6:])
            with tar.extractfile(item) as stream:copy_verified(stream, destination / name, row, pause)
    if found != set(expected):raise RuntimeError('翼起办模板缺少签名文件')
    (destination / 'templates.tar.gz').unlink()
    validate_files(manifest, destination / 'files', pause)


def ensure(owner):
    spec = settings(owner.bundle);manifest = load(owner.bundle)
    target = resource_root(owner.storage, spec)
    trusted_directory(target.parent, os.geteuid())
    no_symlink_parents(target, owner.storage.work)
    if target.is_symlink():raise RuntimeError('翼起办资源暂存目录不能是链接')
    with file_lock(owner.storage.cache / 'locks' / ('yiqiban-' + spec['manifest_sha256'])):
        if target.exists():
            try:
                validate_files(manifest, target / 'files', owner.pause_point)
            except (OSError, RuntimeError):
                # Pause requests are a control signal, not a damaged payload.
                owner.pause_point()
                backup = target.with_name(target.name + '-invalid-' + uuid.uuid4().hex)
                target.rename(backup)
                owner.event('翼起办暂存内容损坏，已隔离并重新准备资源')
            else:
                owner.event('复用已完整校验的翼起办资源');return target / 'files'
        request = read_json(owner.root / 'private/request.json')
        original = source_config(owner.bundle, request.get('sources'))
        roots = [str(p) for p in source_roots(owner.bundle, original)]
        def progress(row, n, phase):
            owner.update('翼起办 ' + phase + '：' + spec['archive']['path'], min(n / spec['archive']['size'], 0.8),
                         transfer={'file': spec['archive']['path'], 'file_bytes': n, 'file_total': spec['archive']['size'],
                                   'ready_bytes': n, 'total_bytes': spec['archive']['size'], 'phase': phase})
        owner.event('准备翼起办应用资源：优先使用已校验缓存或本地 ZIP，否则从 Harbor 断点下载（约 4.41 GB）')
        store = BlobStore(owner.bundle, acquisition_config(spec, roots), owner.storage.cache,
                          progress, owner.pause_point, rows=[spec['archive']])
        archive = store.ensure(spec['archive'])
        owner.update('完整校验并展开翼起办签名资源', 0.85, force=True)
        stage = Path(tempfile.mkdtemp(prefix='.yiqiban-', dir=owner.storage.work))
        try:
            unpack(owner.bundle, archive, spec, manifest, stage, owner.pause_point)
            owner.pause_point();stage.rename(target)
        finally:
            # Only this freshly allocated private extraction directory is removed.
            if stage.exists() and stage.resolve().is_relative_to(owner.storage.work.resolve()):shutil.rmtree(stage)
        owner.state.pop('transfer', None)
        owner.state['yiqiban_source'] = {'release_id': spec['release_id'], 'manifest_digest': spec['oci']['manifest_digest'],
                                        'zip_sha256': spec['archive']['sha256'], 'resources': str(target / 'files')}
        owner.event('翼起办 ZIP、发行签名及全部资源校验通过，开始部署')
        return target / 'files'
