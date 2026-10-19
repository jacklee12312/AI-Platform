"""Fetch the current probe from its publisher before classifying the local binary."""
import hashlib
from http.client import HTTPException
import os
from pathlib import Path
import re
import shutil
import struct
import tarfile
import tempfile
import time
from urllib import request
from urllib.error import HTTPError

from common import no_symlink_parents, safe_relative, sha256

PROBE_URL = 'http://36.111.34.121:15433/api/liuer/ops/probe-package/download'
MAX_DOWNLOAD = 64 * 1024**2
MAX_EXPANDED = 256 * 1024**2
REQUIRED = ('build/data-probe', 'deploy/data-probe.service',
            'deploy/data-probe-rollback.service', 'deploy/data-probe-rollback.sh',
            'config/certs/ca.pem', 'config/certs/sm2_pub.pem')


class PublisherRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from urllib.parse import urlsplit
        original = urlsplit(PROBE_URL);target = urlsplit(newurl)
        if target.username or target.password or (target.scheme, target.hostname, target.port) != (original.scheme, original.hostname, original.port):
            raise RuntimeError('探针下载接口重定向到其他来源，未修改本机探针')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def embedded_probe(path):
    """Only omit old probe payloads; leave historical signed catalogs intact."""
    return 'workflow/probe/' in ('/' + str(path).replace('\\', '/'))


def unpack(archive, directory):
    source = Path(directory) / 'data-probe-offline'
    total = 0;seen = set()
    with tarfile.open(archive, 'r:gz') as tar:
        for item in tar:
            relative = safe_relative(item.name)
            if relative.parts[0] != 'data-probe-offline' or relative.as_posix() in seen:
                raise RuntimeError('最新探针包目录或文件路径不兼容')
            seen.add(relative.as_posix())
            if len(seen) > 512 or not (item.isfile() or item.isdir()):
                raise RuntimeError('最新探针包含不支持的文件类型或过多文件')
            if item.isdir():continue
            total += item.size
            if item.size < 0 or total > MAX_EXPANDED:
                raise RuntimeError('最新探针解压大小超过限制')
            name = relative.relative_to('data-probe-offline').as_posix()
            # Never execute vendor install scripts or import upstream binding data.
            if name not in REQUIRED:continue
            target = source / name;no_symlink_parents(target, Path(directory))
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tar.extractfile(item) as src, target.open('xb') as dst:
                shutil.copyfileobj(src, dst, 1024**2)
            target.chmod(0o700 if name in ('build/data-probe', 'deploy/data-probe-rollback.sh') else 0o600)
    for name in REQUIRED:
        if not (source / name).is_file() or not (source / name).stat().st_size:
            raise RuntimeError('最新探针包缺少必要文件：' + name)
    with (source / 'build/data-probe').open('rb') as stream:header = stream.read(64)
    if (len(header) != 64 or header[:7] != b'\x7fELF\x02\x01\x01'
            or struct.unpack_from('<H', header, 18)[0] != 183):
        raise RuntimeError('最新探针程序不是 Linux ARM64 ELF 文件')
    return source


def fetch_probe(flow):
    """A new GET per probe stage/retry. Failure never falls back to the old bundle."""
    parent = Path(flow.directory);no_symlink_parents(parent, Path('/'))
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder = Path(tempfile.mkdtemp(prefix='probe-online-', dir=parent));folder.chmod(0o700)
    archive = folder / 'download.tar.gz'
    flow.owner.event('从发布接口下载当前最新探针，完成后比较本机程序摘要')
    try:
        opener = request.build_opener(PublisherRedirect())
        req = request.Request(PROBE_URL, headers={'Cache-Control': 'no-cache', 'Accept-Encoding': 'identity',
                                                'User-Agent': 'AI-Platform-Installer/online-probe'})
        with opener.open(req, timeout=30) as response, archive.open('xb') as out:
            archive.chmod(0o600)
            if response.status != 200 or response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                raise RuntimeError('探针接口没有返回完整资源包')
            value = response.headers.get('Content-Length')
            length = int(value) if value is not None else None
            if length is not None and not 0 < length <= MAX_DOWNLOAD:
                raise RuntimeError('最新探针下载大小超过限制或为空')
            count = 0;digest = hashlib.sha256();deadline = time.monotonic() + 180
            while True:
                flow.owner.pause_point()
                if time.monotonic() > deadline:raise RuntimeError('最新探针下载超时，未修改本机探针')
                block = getattr(response, 'read1', response.read)(64 * 1024)
                if not block:break
                count += len(block)
                if count > MAX_DOWNLOAD or (length is not None and count > length):
                    raise RuntimeError('最新探针下载大小与响应不符')
                out.write(block);digest.update(block)
                flow.owner.update('下载最新探针：' + f'{count / 1024**2:.1f} MB', force=True)
            out.flush();os.fsync(out.fileno())
            if not count or (length is not None and count != length):
                raise RuntimeError('最新探针下载未完成，未修改本机探针')
            disposition = response.headers.get('Content-Disposition', '')
        source = unpack(archive, folder)
        match = re.search(r'data-probe-([0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?)-(?:aarch64|arm64)-offline', disposition)
        version = match[1] if match else '接口当前版本（未提供版本号）'
        result = {'source': source, 'version': version, 'binary_sha256': sha256(source / 'build/data-probe'),
                  'archive_sha256': digest.hexdigest(), 'bytes': count}
        public = {k:v for k,v in result.items() if k != 'source'}
        public.update(url=PROBE_URL, downloaded_at=time.time())
        flow.owner.state['probe_source'] = public
        flow.owner.update(force=True)
        flow.owner.event('最新探针下载并检查通过：' + version + '；将按程序 SHA-256 比较，不只比较版本号')
        return result
    except (OSError, ValueError, tarfile.TarError, HTTPException) as exc:
        if isinstance(exc, HTTPError):exc.close()
        shutil.rmtree(folder)
        raise RuntimeError('无法下载或解析最新探针；未修改本机探针，请检查网络和下载接口后继续安装') from exc
    except BaseException:
        shutil.rmtree(folder)
        raise
