"""Publisher-only: lock and prepare the ARM64 prerequisites for installer 1.1.

Downloads and archive construction only. Never installs a package or starts a service.
Existing lock files are reused; refreshes require --refresh-lock explicitly.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / 'support-files/files'
WORK = ROOT / 'build/workflow-resources'
LOCK = ROOT / 'installer-bundle/catalog/workflow.json'
IMAGES = {'mysql': 'mysql:8.0.36', 'redis': 'redis:7.4',
          'paradedb': 'paradedb/paradedb:0.24.0-pg18', 'neo4j': 'neo4j:5.26.29',
          'minio': 'dappros/minio:RELEASE.2025-09-07T16-13-09Z', 'milvus': 'milvusdb/milvus:v2.6.21'}
MINIO_DIGEST = 'sha256:2f25127f784f002f1b98df5ae40cb1a67e0b8a877c90b6113c14b223a2ac65a5'
PACKAGES = ['nginx', 'etcd-server', 'etcd-client', 'libpaho-mqtt1.3', 'libcurl4',
            'libssl3', 'libyaml-cpp0.7', 'libspdlog1.10', 'libfmt9', 'libstdc++6', 'libgcc-s1', 'libc6']


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def get(url, headers=None):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=90) as response:
        return response.read()


def asset(path, **extra):
    return dict(path=path.relative_to(FILES).as_posix(), size=path.stat().st_size, sha256=sha(path), **extra)


def download(url, target, digest=None, headers=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and (digest is None or sha(target) == digest):
        return
    part = target.with_name(target.name + '.part')
    if part.is_file() and digest is not None and sha(part) == digest:
        part.replace(target);return
    for attempt in range(3):
        try:
            offset = part.stat().st_size if part.exists() else 0
            request_headers = dict(headers or {})
            if offset:request_headers['Range'] = 'bytes=' + str(offset) + '-'
            with urllib.request.urlopen(urllib.request.Request(url, headers=request_headers), timeout=120) as source:
                resume = source.status == 206 and source.headers.get('Content-Range', '').startswith('bytes ' + str(offset) + '-')
                with part.open('ab' if resume else 'wb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
            if digest is not None and sha(part) != digest:
                part.unlink()
                raise RuntimeError('Downloaded digest differs: ' + target.name)
            part.replace(target)
            return
        except Exception:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def lock_image(name, reference):
    repository, tag = reference.rsplit(':', 1)
    if '/' not in repository:
        repository = 'library/' + repository
    token = json.loads(get('https://auth.docker.io/token?' + urllib.parse.urlencode({
        'service': 'registry.docker.io', 'scope': 'repository:' + repository + ':pull'})))['token']
    headers = {'Authorization': 'Bearer ' + token, 'Accept': ', '.join([
        'application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json',
        'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'])}
    base = 'https://registry-1.docker.io/v2/' + repository
    raw = get(base + '/manifests/' + tag, headers)
    index = json.loads(raw)
    if 'manifests' in index:
        choices = [x for x in index['manifests'] if x.get('platform', {}).get('os') == 'linux'
                   and x.get('platform', {}).get('architecture') == 'arm64']
        if len(choices) != 1:
            raise RuntimeError('Expected exactly one ARM64 image: ' + reference)
        raw = get(base + '/manifests/' + choices[0]['digest'], headers)
        if 'sha256:' + hashlib.sha256(raw).hexdigest() != choices[0]['digest']:
            raise RuntimeError('Registry manifest digest differs')
    manifest = json.loads(raw)
    digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
    if name == 'minio' and digest != MINIO_DIGEST:
        raise RuntimeError('MinIO differs from the source explicitly approved by the user')
    config_bytes = get(base + '/blobs/' + manifest['config']['digest'], headers)
    if 'sha256:' + hashlib.sha256(config_bytes).hexdigest() != manifest['config']['digest']:
        raise RuntimeError('Image config digest differs')
    config = json.loads(config_bytes)
    if config['architecture'] != 'arm64' or config['os'] != 'linux':
        raise RuntimeError('Incorrect image platform')
    directory = WORK / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'config.json').write_bytes(config_bytes)
    write(directory / 'manifest.json', manifest)
    print('Locked ' + name + ' ' + digest, flush=True)
    return dict(id=name, image=reference, pinned=reference.split(':')[0] + '@' + digest,
                manifest_digest=digest, config_id=manifest['config']['digest'],
                rootfs_diff_ids=config['rootfs']['diff_ids'], layers=manifest['layers'])


def image_archive(row):
    name = row['id']
    path = FILES / 'workflow/images' / (name + '-linux-arm64.tar.gz')
    if row.get('archive') and path.is_file() and sha(path) == row['archive']['sha256']:
        return row
    # The previously verified MinIO Docker export is preserved verbatim.
    verified = ROOT / 'build/minio-mirror-check/minio-RELEASE.2025-09-07T16-13-09Z-linux-arm64.tar.gz'
    if name == 'minio' and verified.is_file() and sha(verified) == '04c2edcbd02684af5975677d78efb7b7098349ac4e3a22dbcc0e52f1213041ac':
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(verified, path)
        row['archive'] = asset(path)
        return row
    repository = row['image'].rsplit(':', 1)[0]
    if '/' not in repository:
        repository = 'library/' + repository
    token = json.loads(get('https://auth.docker.io/token?' + urllib.parse.urlencode({
        'service': 'registry.docker.io', 'scope': 'repository:' + repository + ':pull'})))['token']
    headers = {'Authorization': 'Bearer ' + token}
    base = 'https://registry-1.docker.io/v2/' + repository + '/blobs/'
    blobs = WORK / 'blobs'
    blobs.mkdir(exist_ok=True)
    def fetch(layer):
        p = blobs / layer['digest'].split(':')[1]
        download(base + layer['digest'], p, layer['digest'].split(':')[1], headers)
        if p.stat().st_size != layer['size']:
            raise RuntimeError('Layer size differs')
        return p
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(fetch, row['layers']))
    path.parent.mkdir(parents=True, exist_ok=True)
    layer_names = []
    with path.open('wb') as output, gzip.GzipFile(fileobj=output, mode='wb', filename='', mtime=0, compresslevel=1) as zipped, tarfile.open(fileobj=zipped, mode='w|') as tar:
        def add_bytes(name, data):
            item = tarfile.TarInfo(name);item.size = len(data);item.mode = 0o644
            tar.addfile(item, io.BytesIO(data))
        cfg = row['config_id'].split(':')[1] + '.json'
        add_bytes(cfg, (WORK / name / 'config.json').read_bytes())
        for source, diff in zip(paths, row['rootfs_diff_ids'], strict=True):
            expanded = WORK / 'expanded-layer.tar'
            with source.open('rb') as raw:
                magic = raw.read(2)
            reader = gzip.open(source, 'rb') if magic == b'\x1f\x8b' else source.open('rb')
            with reader, expanded.open('wb') as target:
                shutil.copyfileobj(reader, target, 8 * 1024 * 1024)
            if 'sha256:' + sha(expanded) != diff:
                raise RuntimeError('Uncompressed layer digest differs')
            layer_name = diff.split(':')[1] + '/layer.tar';layer_names.append(layer_name)
            item = tarfile.TarInfo(layer_name);item.size = expanded.stat().st_size;item.mode = 0o644
            with expanded.open('rb') as content:
                tar.addfile(item, content)
            expanded.unlink()
        add_bytes('manifest.json', json.dumps([{'Config': cfg, 'RepoTags': [row['image']], 'Layers': layer_names}]).encode())
    row['archive'] = asset(path)
    print('Prepared ' + name + ': ' + str(path.stat().st_size) + ' bytes', flush=True)
    return row


def probe_archive():
    raw = WORK / 'probe-upstream.tar.gz'
    download('http://36.111.34.121:15433/api/liuer/ops/probe-package/download', raw,
             '4c7b3f3fa608232543bf39d418c491a6a92f05404374a05150a63cf39934d483')
    target = FILES / 'workflow/probe/data-probe-1.1.2-sanitized.tar.gz'
    target.parent.mkdir(parents=True, exist_ok=True)
    binary_sha = None
    with tarfile.open(raw) as source, target.open('wb') as output, gzip.GzipFile(fileobj=output, mode='wb', filename='', mtime=0) as zipped, tarfile.open(fileobj=zipped, mode='w|') as packed:
        for member in source:
            if not member.isfile() and not member.isdir():
                raise RuntimeError('Unexpected probe archive member')
            data = source.extractfile(member).read() if member.isfile() else None
            if member.name.endswith('/config/probe.yaml'):
                text = data.decode('utf-8')
                text = re.sub(r'(?m)^(  (?:owner|project|city|location|deploymentPurpose|accessPolicy|bindActivateToken|os|arch|hardwareModel):).*$', r'\1 ""', text)
                text = text.replace('  enabled: true', '  enabled: false')
                data = text.encode('utf-8')
            if member.name.endswith('/build/data-probe'):
                binary_sha = hashlib.sha256(data).hexdigest()
            item = tarfile.TarInfo(member.name);item.type = member.type;item.mode = member.mode & 0o777
            item.size = len(data) if data is not None else 0
            packed.addfile(item, io.BytesIO(data) if data is not None else None)
    return dict(version='1.1.2', archive=asset(target), binary_sha256=binary_sha,
                upstream_sha256=sha(raw), registration_required=True, mqtt_required=True)


def main():
    parser = argparse.ArgumentParser();parser.add_argument('--refresh-lock', action='store_true')
    args = parser.parse_args();WORK.mkdir(parents=True, exist_ok=True)
    if LOCK.is_file() and not args.refresh_lock:
        lock = json.loads(LOCK.read_text('utf-8'))
    else:
        lock = {'schema': 1, 'target': 'linux-arm64', 'order': ['probe', 'components', 'models'],
                'probe': probe_archive(), 'images': [lock_image(k, v) for k, v in IMAGES.items()],
                'host_packages': [], 'probe_packages': PACKAGES[3:],
                'component_packages': ['nginx', 'etcd-server', 'etcd-client']}
        write(LOCK, lock)
    for index, row in enumerate(lock['images']):
        lock['images'][index] = image_archive(row);write(LOCK, lock)
    print('Image and sanitized probe assets ready. Prepare the Debian dependency closure next.', flush=True)


if __name__ == '__main__':
    main()
