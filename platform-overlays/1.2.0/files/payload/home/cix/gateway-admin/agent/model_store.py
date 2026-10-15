"""Pinned model catalog, resumable downloads and portable offline bundles.

This module is stdlib-only and can also run on the delivery engineer's laptop.
Bundle metadata cannot add models, URLs, commands or destination paths.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import time
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
HOSTS = {'www.modelscope.cn', 'modelscope.cn', 'huggingface.co', 'github.com', 'llmoss.houmoai.com'}
CHUNK = 1024 * 1024
SCHEMA = 1


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8', newline='\n') as stream:
            os.chmod(temporary, 0o644)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(CHUNK), b''):
            value.update(block)
    return value.hexdigest()


def safe_relative(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise ValueError('无效的模型文件路径')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {'', '.', '..'} for part in value.split('/')):
        raise ValueError('模型文件路径越界')
    return path


def under(root: Path, value: str):
    safe_relative(value)
    root = root.resolve()
    path = root / value
    # Symlinks must not redirect writes outside the owned destination.
    if not path.resolve().is_relative_to(root):
        raise ValueError('模型文件路径越界')
    return path


@contextlib.contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        try:
            if os.name == 'nt':
                import msvcrt
                stream.seek(0); stream.write(b'0'); stream.flush(); stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('另一个模型操作正在执行，请等待完成') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


class Catalog:
    def __init__(self, path=None, baseline=None):
        document = json.loads(Path(path or HERE / 'model_catalog.json').read_text(encoding='utf-8'))
        baseline = baseline or json.loads((HERE / 'runtime_baseline.json').read_text(encoding='utf-8'))
        self.models = {}
        self.files = {}
        self.release_id = document.get('resource_release_id', '')
        if document.get('schema_version') != SCHEMA:
            raise ValueError('不支持的模型目录版本')
        for row in document['models']:
            row = dict(row)
            model_id = row['id']
            if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}', model_id) or model_id in self.models:
                raise ValueError('模型 ID 无效或重复')
            spec = baseline['models'][model_id]
            if not spec.get('gateway_enabled'):
                raise ValueError('实验项不能进入可安装目录')
            row.update(version=spec['model_version'], modes=spec.get('modes', []), port=spec['port'],
                       image=baseline['components'][row['component']]['image'])
            targets = set()
            for item in row['files']:
                safe_relative(item['target'])
                if 'oci_source' in item:
                    from .registry import validate_sources
                    def allowed(url,allow_http):
                        if url!='http://36.111.34.121:15436':raise ValueError('模型仓库不在发布来源中')
                    validate_sources({'allow_http':True,'oci_artifacts':{'model':item['oci_source']}},safe_relative,allowed,ValueError)
                    source=item['oci_source']
                    if source['format']=='blob' and (source['blob_digest']!='sha256:'+item['sha256'] or source['blob_size']!=item['size']):
                        raise ValueError('模型 OCI 来源与固定摘要不符')
                    if source['format']=='tar+gzip' and source['files'].get('payload/home/cix/'+item['target']) is None:
                        raise ValueError('模型 OCI 归档缺少目标文件映射')
                elif 'bundled_file' in item:
                    safe_relative(item['bundled_file'])
                else:
                    parsed = urlsplit(item['url'])
                    if parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.username or parsed.password:
                        raise ValueError('下载地址不在发布目录允许的来源中')
                if not re.fullmatch('[0-9a-f]{64}', item['sha256']) or not isinstance(item['size'], int) or item['size'] <= 0:
                    raise ValueError('模型文件缺少固定校验值或大小')
                if item['target'] in targets:
                    raise ValueError('模型目标路径重复')
                targets.add(item['target'])
                previous = self.files.get(item['sha256'])
                if previous and previous['size'] != item['size']:
                    raise ValueError('同一 SHA-256 的文件大小冲突')
                self.files[item['sha256']] = item
            if not targets:
                raise ValueError('模型没有文件')
            row['download_bytes'] = sum(item['size'] for item in row['files'])
            self.models[model_id] = row
        self.fingerprint = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()

    def select(self, ids):
        if not ids or len(ids) > len(self.models) or len(ids) != len(set(ids)):
            raise ValueError('请选择不重复的已适配模型')
        if any(model_id not in self.models for model_id in ids):
            raise ValueError('模型不在已适配目录中')
        return [self.models[model_id] for model_id in ids]


class ModelStore:
    def __init__(self, root, catalog=None, *, opener=urlopen):
        self.root = Path(root)
        self.catalog = catalog or Catalog()
        self.opener = opener
        for folder in ('cache', 'inbox', 'jobs', 'backups', 'transactions'):
            (self.root / folder).mkdir(parents=True, exist_ok=True)

    def cached(self, item):
        return self.root / 'cache' / item['sha256']

    def ready(self, item):
        path = self.cached(item)
        try:
            stat = path.stat()
            receipt = json.loads(path.with_suffix('.json').read_text())
            return stat.st_size == item['size'] and receipt == {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
        except (OSError, ValueError):
            return False

    def verify(self, path, item):
        if path.stat().st_size != item['size'] or digest(path) != item['sha256']:
            raise ValueError('模型文件校验失败：' + item['target'])

    def receipt(self, item):
        path = self.cached(item)
        os.chmod(path, 0o644)  # Model containers run as UID 65534.
        stat = path.stat()
        atomic_json(path.with_suffix('.json'), {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns})

    def remaining(self, ids):
        items = {item['sha256']: item for model in self.catalog.select(ids) for item in model['files']}
        return sum(item['size'] for item in items.values() if not self.ready(item))

    def transfer_remaining(self, ids):
        items = {f['sha256']: f for m in self.catalog.select(ids) for f in m['files'] if not self.ready(f)}
        sources = {}
        for item in items.values():
            if 'bundled_file' in item: continue
            source = item.get('oci_source')
            if source:
                blob = {'sha256': source['blob_digest'][7:], 'size': source['blob_size']}
                if not self.ready(blob): sources[blob['sha256']] = blob['size']
            else: sources[item['sha256']] = item['size']
        return sum(sources.values())

    def download(self, item, progress=lambda _: None):
        if self.ready(item):
            return self.cached(item)
        if 'bundled_file' in item:
            return self.import_file(under(HERE, item['bundled_file']), item, progress)
        if item.get('oci_source', {}).get('format') == 'tar+gzip':
            source = item['oci_source']
            blob = {'target': source['repository'], 'sha256': source['blob_digest'][7:],
                    'size': source['blob_size'], 'oci_source': {**source, 'format': 'blob'}}
            archive = self.download(blob, progress)
            self.import_archive(archive, source, progress)
            if not self.ready(item):
                raise ValueError('仓库资源包缺少模型文件：'+item['target'])
            return self.cached(item)
        final = self.cached(item)
        if self.ready(item):
            return final
        if final.exists():
            try:
                self.verify(final, item); self.receipt(item)
                return final
            except ValueError:
                # This is an owned, content-addressed cache entry, not a live target.
                final.unlink()
                final.with_suffix('.json').unlink(missing_ok=True)
        partial = final.with_suffix('.part')
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > item['size']:
            partial.unlink(); offset = 0
        if offset < item['size']:
            headers = {'User-Agent': 'ModelDeck-ModelInstaller/1', 'Accept-Encoding': 'identity'}
            if offset:
                headers['Range'] = f'bytes={offset}-'
            opener=self.opener;url=item.get('url')
            if 'oci_source' in item:
                from .registry import RegistryClient
                client=RegistryClient(item['oci_source'],40);client.verify_manifest()
                opener=client.open;url=client.blob_url()
            with opener(Request(url, headers=headers), timeout=40) as response:
                status = response.status
                if status == 206:
                    expected = f'bytes {offset}-'
                    content_range = response.headers.get('Content-Range', '')
                    if not content_range.startswith(expected) or not content_range.endswith('/' + str(item['size'])):
                        raise ValueError('下载服务器返回了错误的续传区间')
                elif status == 200:
                    offset = 0
                else:
                    raise ValueError('下载服务器返回异常状态')
                last = 0.0
                with partial.open('ab' if offset else 'wb') as stream:
                    while True:
                        block = response.read(CHUNK)
                        if not block:
                            break
                        if offset + len(block) > item['size']:
                            raise ValueError('下载文件超过目录声明大小')
                        stream.write(block); offset += len(block)
                        if time.monotonic() - last >= 1:
                            progress({'stage': '下载模型文件', 'file': item['target'], 'bytes': offset, 'total_bytes': item['size']})
                            last = time.monotonic()
                    stream.flush(); os.fsync(stream.fileno())
        progress({'stage': '校验 SHA-256', 'file': item['target']})
        try:
            self.verify(partial, item)
        except ValueError:
            # A short transfer is retained for the next explicit retry.
            if partial.stat().st_size >= item['size']:
                partial.unlink()
            raise
        partial.replace(final); self.receipt(item)
        return final

    def import_file(self, path, item=None, progress=lambda _: None):
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise ValueError('导入资源必须是普通文件')
        if item is None:
            candidates = [f for f in self.catalog.files.values() if f['size'] == path.stat().st_size]
            sha = digest(path)
            item = next((f for f in candidates if f['sha256'] == sha), None)
            if item is None:
                raise ValueError('文件与已适配目录的大小或 SHA-256 不匹配')
        if self.ready(item):
            return self.cached(item)
        if shutil.disk_usage(self.root).free < item['size'] + 256 * CHUNK:
            raise ValueError('导入模型缓存空间不足')
        self.verify(path, item)
        progress({'stage': '导入并校验模型文件', 'file': item['target']})
        temporary = self.cached(item).with_suffix('.import')
        with path.open('rb') as src, temporary.open('wb') as dst:
            shutil.copyfileobj(src, dst, CHUNK)
            dst.flush(); os.fsync(dst.fileno())
        self.verify(temporary, item)
        temporary.replace(self.cached(item)); self.receipt(item)
        return self.cached(item)

    def import_archive(self, path, source, progress=lambda _: None):
        expected = {source['files']['payload/home/cix/'+f['target']]: f for f in self.catalog.files.values()
                    if 'payload/home/cix/'+f['target'] in source['files']}
        seen, found, expanded = set(), set(), 0
        limit = max(source['blob_size'] * 64, sum(f['size'] for f in expected.values()) * 2) + 64 * CHUNK
        with tarfile.open(path, 'r|*') as archive:
            for member in archive:
                name = member.name.rstrip('/')
                safe_relative(name)
                if name in seen or len(seen) >= 50000 or not (member.isfile() or member.isdir()):
                    raise ValueError('模型资源包含重复路径、链接或特殊文件')
                seen.add(name); expanded += member.size
                if expanded > limit:
                    raise ValueError('模型资源包展开大小超过限制')
                if name not in expected or member.isdir():
                    continue
                item = expected[name]; found.add(name)
                if member.size != item['size']:
                    raise ValueError('模型资源包文件大小不匹配：'+name)
                if self.ready(item):
                    continue
                if shutil.disk_usage(self.root).free < item['size'] + 256 * CHUNK:
                    raise ValueError('模型资源包展开空间不足')
                progress({'stage': '展开并校验模型资源', 'file': item['target']})
                partial = self.cached(item).with_suffix('.import')
                with archive.extractfile(member) as src, partial.open('wb') as dst:
                    shutil.copyfileobj(src, dst, CHUNK)
                self.verify(partial, item)
                partial.replace(self.cached(item)); self.receipt(item)
        if found != set(expected):
            raise ValueError('模型资源包缺少依赖文件')
        return {'files_imported': len(found), 'status': 'downloaded'}

    def import_local(self, path, progress=lambda _: None):
        path = Path(path)
        if path.is_symlink():
            raise ValueError('导入路径不能是链接')
        if path.is_dir():
            imported = 0
            paths = list(path.rglob('*'))
            if len(paths) > 50000:
                raise ValueError('导入目录文件过多')
            for file in paths:
                if file.is_symlink():
                    raise ValueError('导入目录包含链接')
                if not file.is_file():
                    continue
                # Match exact release paths, ORAS member paths or known filenames.
                relative = file.relative_to(path).as_posix()
                matches = [f for f in self.catalog.files.values() if file.stat().st_size == f['size'] and
                           (relative == f['target'] or relative == 'payload/home/cix/'+f['target'] or
                            file.name == PurePosixPath(f['target']).name)]
                if matches:
                    self.import_file(file, None, progress); imported += 1
                elif file.name.endswith(('.tar.gz', '.tgz', '.tar')):
                    result = self.import_local(file, progress); imported += result.get('files_imported', 0)
            if not imported:
                raise ValueError('目录中没有匹配当前设备的已适配模型资源')
            return {'files_imported': imported, 'status': 'downloaded'}
        if not path.is_file():
            raise ValueError('导入文件不存在')
        size = path.stat().st_size
        if any(f['size'] == size for f in self.catalog.files.values()):
            sha = digest(path)
            if sha in self.catalog.files:
                self.import_file(path, self.catalog.files[sha], progress)
                return {'files_imported': 1, 'status': 'downloaded'}
        archives = {f['oci_source']['blob_digest']: f['oci_source'] for f in self.catalog.files.values()
                    if f.get('oci_source', {}).get('format') == 'tar+gzip' and f['oci_source']['blob_size'] == size}
        if archives:
            source = archives.get('sha256:'+digest(path))
            if source:
                return self.import_archive(path, source, progress)
        if tarfile.is_tarfile(path):
            return self.import_bundle(path, progress)
        raise ValueError('不支持的文件：请选择本项目模型、Harbor 资源包或标准离线包')

    def fetch(self, ids, progress=lambda _: None):
        models = self.catalog.select(ids)
        needed = self.remaining(ids) + self.transfer_remaining(ids)
        if shutil.disk_usage(self.root).free < needed + 256 * CHUNK:
            raise ValueError('模型缓存磁盘空间不足')
        for model in models:
            progress({'stage': '准备 ' + model['id'], 'model': model['id']})
            for item in model['files']:
                self.download(item, progress)
        return {'models': list(ids), 'status': 'downloaded', 'message': '已下载并校验，尚未启用'}

    def import_bundle(self, path, progress=lambda _: None):
        # Do not extractall: only known content-addressed regular files may be read.
        imported, seen = [], set()
        with tarfile.open(path, 'r|*') as archive:
            first = archive.next()
            if first is None or first.name != 'modeldeck-bundle.json' or not first.isfile() or first.size > CHUNK:
                raise ValueError('离线包缺少有效的 modeldeck-bundle.json')
            manifest = json.loads(archive.extractfile(first).read(CHUNK))
            if not isinstance(manifest, dict):
                raise ValueError('离线包清单无效')
            if manifest.get('schema_version') != SCHEMA:
                raise ValueError('离线包版本不支持')
            selected = self.catalog.select(manifest.get('models', []))
            expected = {f['sha256']: f for m in selected for f in m['files']}
            if shutil.disk_usage(self.root).free < self.remaining(manifest['models']) + 256 * CHUNK:
                raise ValueError('导入模型所需磁盘空间不足')
            for member in archive:
                if member is first:
                    continue
                key = member.name.removeprefix('files/')
                if member.name != 'files/' + key or key not in expected or key in seen or not member.isfile():
                    raise ValueError('离线包包含未知、重复或非普通文件')
                seen.add(key)
                item = expected[key]
                if member.size != item['size']:
                    raise ValueError('离线包模型大小不符')
                progress({'stage': '导入并校验离线模型', 'file': item['target']})
                if self.ready(item):
                    continue
                partial = self.cached(item).with_suffix('.part')
                with archive.extractfile(member) as source, partial.open('wb') as dest:
                    shutil.copyfileobj(source, dest, CHUNK)
                self.verify(partial, item)
                partial.replace(self.cached(item)); self.receipt(item)
                imported.append(key)
            if seen != set(expected):
                raise ValueError('离线包缺少模型依赖文件')
        return {'models': manifest['models'], 'files_imported': len(imported), 'status': 'downloaded'}

    def export_bundle(self, ids, output):
        items = {f['sha256']: f for m in self.catalog.select(ids) for f in m['files']}
        output = Path(output)
        if output.exists():
            raise ValueError('输出文件已存在，请使用新的包名')
        if any(not self.ready(f) for f in items.values()):
            raise ValueError('请先下载或导入所选模型的完整文件')
        output.parent.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(output.parent).free < sum(f['size'] for f in items.values()) + 16 * CHUNK:
            raise ValueError('离线包输出空间不足')
        partial = output.with_name(output.name + '.partial')
        if partial.exists():
            raise ValueError('存在未完成的同名离线包，请选择新文件名')
        with tarfile.open(partial, 'w') as archive:
            body = json.dumps({'schema_version': SCHEMA, 'models': ids, 'created_at': now(),
                               'catalog_sha256': self.catalog.fingerprint}).encode()
            member = tarfile.TarInfo('modeldeck-bundle.json'); member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
            for sha, item in items.items():
                self.verify(self.cached(item), item)
                archive.add(self.cached(item), arcname='files/' + sha, recursive=False)
        partial.replace(output)
        return {'path': str(output), 'bytes': output.stat().st_size, 'models': ids}
