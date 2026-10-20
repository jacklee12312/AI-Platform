"""Build a separately signed GB10 profile without changing prior signed resources."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from debian import pack
from release import digest, git, read, write


def files_in(directory):
    return [p for p in directory.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc']


def copy_code(source, destination):
    for path in files_in(source):
        if 'node_modules' in path.parts or ('yiqiban' in source.parts and 'files' in path.relative_to(source).parts):
            continue
        if path.suffix in ('.safetensors', '.gguf', '.whl', '.deb', '.tar', '.gz', '.run'):
            continue
        if path.stat().st_size > 25 * 1024**2:
            raise RuntimeError('Unexpected large platform code resource: ' + str(path))
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def assemble(root, stage):
    spec = read(root / 'release-config.json')['profiles']['gb10']
    source = root / spec['source_bundle']
    profile = read(source / 'profile.json')
    if profile['installer_version'] != spec['installer_version'] or profile['resource_release_id'] != spec['resource_release_id']:
        raise RuntimeError('GB10 profile version differs from release-config.json')
    copy_code(source, stage)
    parent = root / 'build/resources-1.2.0/payload/home/cix'
    for name in ('ai-model-gateway', 'gateway-admin'):
        copy_code(parent / name, stage / 'platform' / name)
        overlay = root / 'platform-overlays/1.2.0/files/payload/home/cix' / name
        copy_code(overlay, stage / 'platform' / name)
    # The current platform also carries a separately signed component fix. Apply
    # its verified bytes after the base overlay, just as the original installer does.
    patch_root = root / 'installer-bundle/component-runtime-r9'
    for row in read(patch_root / 'manifest.json')['files']:
        source = patch_root / 'files' / row['path']
        if source.stat().st_size != row['bytes'] or digest(source) != row['sha256']:
            raise RuntimeError('Signed component compatibility resource changed')
        shutil.copyfile(source, stage / 'platform' / row['path'])
    frontend = root / 'platform-overlays/1.2.0/modeldeck-frontend/dist'
    if not (frontend / 'index.html').is_file():
        raise RuntimeError('Build the existing ModelDeck frontend first')
    shutil.copytree(frontend, stage / 'platform/gateway-admin/frontend/dist', dirs_exist_ok=True)
    # Keep the full existing workflow, component and application source available
    # to this hardware profile; no NPU binary or Debian 12 host package is installed.
    copy_code(root / 'installer-bundle/installer', stage / 'legacy-installer')
    for name in ('catalog', 'component-runtime-r9', 'yiqiban'):
        copy_code(root / 'installer-bundle' / name, stage / 'parent-metadata' / name)
    lock = read(root / 'gb10-bundle/python-runtime.json')
    (stage / 'python').mkdir()
    import zipfile
    for row in lock['wheels']:
        wheel = root / row['path']
        if wheel.stat().st_size != row['size'] or digest(wheel) != row['sha256']:
            raise RuntimeError('GB10 dependency changed: ' + row['path'])
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.namelist():
                if '..' in Path(member).parts or Path(member).is_absolute():
                    raise RuntimeError('Invalid dependency archive')
            archive.extractall(stage / 'python')
    # A GB10 device must never offer NPU-only model recipes as installable.
    write(stage / 'platform/gateway-admin/agent/model_catalog.json', {'schema_version': 1, 'models': []})
    return spec


def build(root, key, revision):
    if git(root, 'status', '--porcelain'):
        raise RuntimeError('Commit source changes before building GB10')
    commit = git(root, 'rev-parse', 'HEAD')
    tests = read(root / 'build/validation/tests.json')
    if not tests.get('passed') or tests.get('source_dirty') or tests.get('skipped') or tests.get('source_commit') != commit:
        raise RuntimeError('Run release.py test against this exact clean commit')
    if not revision.isdigit() or int(revision) < 1:
        raise ValueError('Invalid Debian revision')
    spec = read(root / 'release-config.json')['profiles']['gb10']
    version = spec['installer_version'] + '-' + revision
    output = root / 'delivery' / ('ai-platform-gb10-installer_' + version + '_arm64.deb')
    if output.exists():
        raise RuntimeError('GB10 delivery already exists; use a new revision')
    public = subprocess.check_output(['openssl', 'pkey', '-in', str(key), '-pubout'])
    trusted = (root / 'installer-bundle/catalog/release-public.pem').read_bytes()
    if public.replace(b'\r\n', b'\n') != trusted.replace(b'\r\n', b'\n'):
        raise RuntimeError('Publisher key differs from the original trusted key')
    stage_root = Path(tempfile.mkdtemp(prefix='gb10-', dir=root / 'build'))
    stage = stage_root / 'bundle'
    assemble(root, stage)
    epoch = int(git(root, 'show', '-s', '--format=%ct', commit))
    write(stage / 'provenance.json', {'schema': 1, 'source_commit': commit, 'source_dirty': False,
          'installer_version': spec['installer_version'], 'package_version': version, 'target': spec['target'],
          'resource_release_id': spec['resource_release_id'], 'automated_tests': tests['tests_run'],
          'fresh_device_install': 'not_run', 'reboot': 'not_run'})
    rows = [{'path': p.relative_to(stage).as_posix(), 'size': p.stat().st_size, 'sha256': digest(p)}
            for p in sorted(files_in(stage)) if not p.relative_to(stage).as_posix().startswith('catalog/')]
    manifest = stage / 'catalog/manifest.json'
    write(manifest, {'schema': 1, 'release_id': spec['resource_release_id'], 'source_commit': commit,
                    'public_key_sha256': hashlib.sha256(trusted).hexdigest(), 'files': rows})
    shutil.copyfile(root / 'installer-bundle/catalog/release-public.pem', stage / 'catalog/release-public.pem')
    subprocess.run(['openssl', 'pkeyutl', '-sign', '-inkey', str(key), '-rawin', '-in', str(manifest),
                    '-out', str(stage / 'catalog/manifest.sig')], check=True)
    prefix = 'opt/ai-platform-gb10/' + spec['installer_version']
    files = {prefix + '/' + p.relative_to(stage).as_posix(): (p, 0o644) for p in files_in(stage)}
    launcher = '#!/bin/sh\nexport PYTHONDONTWRITEBYTECODE=1\nexport PYTHONPATH=/' + prefix + '\nexec /usr/bin/python3 -B -m gb10.install "$@"\n'
    files['usr/bin/ai-platform-gb10'] = (launcher.encode(), 0o755)
    desktop = '[Desktop Entry]\nType=Application\nName=AI Platform GB10\nName[zh_CN]=AI 平台 GB10 安装与管理\nExec=/usr/bin/python3 -B /' + prefix + '/gb10/desktop.py\nIcon=system-software-install\nTerminal=false\nCategories=System;\n'
    files['usr/share/applications/com.ai-platform.GB10.desktop'] = (desktop.encode(), 0o644)
    control = ('Package: ai-platform-gb10-installer\nVersion: ' + version + '\nArchitecture: arm64\n'
               'Maintainer: AI Platform Delivery\nDepends: python3 (>= 3.12), python3 (<< 3.13), openssl, systemd, zenity, policykit-1\n'
               'Description: AI Platform GB10 candidate for Ubuntu 24.04 ARM64\n'
               ' Reuses existing NVIDIA runtime and local GPT OSS / Nemotron weights.\n')
    work = stage_root / 'debian'
    work.mkdir()
    output.parent.mkdir(exist_ok=True)
    pack(output, files, {'control': (control.encode(), 0o644)}, epoch, work)
    record = {'package': output.relative_to(root).as_posix(), 'bytes': output.stat().st_size, 'sha256': digest(output),
              'source_commit': commit, 'resource_release_id': spec['resource_release_id'],
              'trusted_public_key_preserved': True, 'automated_tests': tests['tests_run'], 'candidate': True}
    write(root / ('releases/gb10-' + version + '.json'), record)
    output.with_suffix(output.suffix + '.sha256').write_text(record['sha256'] + '  ' + output.name + '\n')
    print(json.dumps(record, ensure_ascii=False, indent=2))
