"""Prepare an authenticated app resource, excluding the three duplicate databases."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import zipfile

RELEASE_ID = 'yiqiban-0.6.1-arm64-20261019'
EXCLUDED = {'mysql', 'redis', 'paradedb'}


def decoded(name):
    try:return name.encode('cp437').decode('utf-8')
    except UnicodeError:return name


def prepare(root, archive, key):
    import release
    root = Path(root).resolve();archive = Path(archive).resolve();bundle = root / 'installer-bundle'
    base = bundle / 'yiqiban'
    if (base / 'manifest.json').exists():raise RuntimeError('YiQiBan resource already sealed; use a new explicit release ID')
    metadata = release.read(bundle / 'release.json');public = bundle / 'catalog/release-public.pem'
    if release.digest(public) != metadata['catalog_public_key_sha256']:raise RuntimeError('Original publisher key differs')
    sys.path.insert(0, str(bundle / 'installer'))
    from common import safe_relative
    from yiqiban_config import resumable_schema
    print('Checking the original archive and excluding bundled database images...', flush=True)
    source_digest = release.digest(archive)
    files = [];assets = []
    with zipfile.ZipFile(archive) as z:
        members = {decoded(r.filename): r for r in z.infolist()}
        if len(members) != len(z.infolist()):raise RuntimeError('Duplicate archive entries')
        images = json.loads(z.read(members['arm64/IMAGES.json']))
        selected_images = [dict(service=name, tag=row['image'], id=row['image_id'], archive=row['archive'])
                           for name, row in images.items() if name not in EXCLUDED]
        if len(selected_images) != 10:raise RuntimeError('Expected ten application images')
        required = {r['archive'] for r in selected_images} | {'configure.py', 'compose.yaml', 'RELEASE.json'}
        sums = {}
        for line in z.read(members['arm64/SHA256SUMS.txt']).decode('utf-8-sig').splitlines():
            match = re.fullmatch(r'([a-f0-9]{64}) [ *](.+)', line)
            if match:sums[match[2].removeprefix('./')] = match[1]
        resumable_schema(z.read(members['arm64/sql/01-agent-schema.sql']).decode('utf-8-sig'))
        for name, member in sorted(members.items()):
            if member.is_dir() or not name.startswith('arm64/'):continue
            relative = name[len('arm64/'):]
            if not (relative in required or relative.startswith(('config/', 'sql/'))):continue
            if '/._' in '/' + relative or Path(relative).name == '.DS_Store':continue
            safe_relative(relative)
            if ((member.external_attr >> 16) & 0o170000) == 0o120000:raise RuntimeError('Resource links are not accepted')
            if relative not in sums:raise RuntimeError('Original checksum missing: ' + relative)
            target = base / 'files' / relative
            if not target.resolve().is_relative_to((base / 'files').resolve()):raise RuntimeError('Unsafe resource path')
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.is_symlink() or target.stat().st_size != member.file_size or release.digest(target) != sums[relative]:
                    raise RuntimeError('Refusing changed resource: ' + relative)
            else:
                print('Verifying resource: ' + relative, flush=True) if relative.endswith('.tar') else None
                digest = hashlib.sha256()
                with z.open(member) as source, target.open('xb') as out:
                    while chunk := source.read(8 * 1024**2):out.write(chunk);digest.update(chunk)
                if digest.hexdigest() != sums[relative]:raise RuntimeError('Original checksum mismatch: ' + relative)
            files.append({'path': relative, 'size': member.file_size, 'sha256': sums[relative]})
            assets.append({'path': target.relative_to(root).as_posix(), 'bytes': member.file_size, 'sha256': sums[relative]})
    for image in selected_images:
        with tarfile.open(base / 'files' / image['archive']) as tar:
            entries = json.load(tar.extractfile('manifest.json'))
            matching = [r for r in entries if image['tag'] in (r.get('RepoTags') or [])]
            if len(matching) != 1:raise RuntimeError('Saved image tag is ambiguous')
            config_bytes = tar.extractfile(matching[0]['Config']).read()
            config = json.loads(config_bytes)
            if config['architecture'] != 'arm64' or config['os'] != 'linux':raise RuntimeError('Wrong image platform')
            image.update(config_id='sha256:' + hashlib.sha256(config_bytes).hexdigest(), rootfs_diff_ids=config['rootfs']['diff_ids'])
    manifest = {'schema': 1, 'release_id': RELEASE_ID, 'platform': 'linux/arm64',
                'source_archive_sha256': source_digest, 'application_version': '0.6.1-offline-20261015-195129',
                'shared_components': sorted(EXCLUDED), 'images': selected_images, 'files': files}
    release.write(base / 'manifest.json', manifest)
    subprocess.run(['openssl', 'pkeyutl', '-sign', '-inkey', str(key), '-rawin', '-in', str(base / 'manifest.json'),
                    '-out', str(base / 'manifest.sig')], check=True)
    subprocess.run(['openssl', 'pkeyutl', '-verify', '-pubin', '-inkey', str(public), '-rawin', '-in', str(base / 'manifest.json'),
                    '-sigfile', str(base / 'manifest.sig')], check=True)
    metadata['yiqiban_resource_id'] = RELEASE_ID;release.write(bundle / 'release.json', metadata)
    spec = release.read(root / 'release-config.json');spec['yiqiban_resource_id'] = RELEASE_ID
    release.write(root / 'release-config.json', spec)
    lock = release.read(root / 'external-assets.json')
    previous = {row['path'] for row in lock['files']}
    if previous.intersection(r['path'] for r in assets):raise RuntimeError('Resource asset lock already contains this release')
    lock['files'].extend(assets);lock['files'].sort(key=lambda r: r['path']);release.write(root / 'external-assets.json', lock)
    print(json.dumps({'resource_release_id': RELEASE_ID, 'verified_files': len(files), 'application_images': 10,
                      'bundled_database_images': 0, 'installer_package_built': False}), flush=True)
