"""Native Debian packaging of a verified immutable delivery; called by release.py."""
import gzip
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


def add_bytes(archive, name, data, epoch, mode=0o644):
    info = tarfile.TarInfo('./' + name)
    info.size, info.mtime, info.mode = len(data), epoch, mode
    archive.addfile(info, io.BytesIO(data))


def write_ar(output, members, epoch):
    with output.open('xb') as stream:
        stream.write(b'!<arch>\n')
        for name, path in members:
            size = path.stat().st_size
            if size > 9999999999:
                raise RuntimeError('Debian ar member exceeds the ten-digit size field')
            header = f'{name + "/":<16}{epoch:<12}{0:<6}{0:<6}{"100644":<8}{size:<10}`\n'
            if len(header) != 60:
                raise RuntimeError('Invalid Debian ar header')
            stream.write(header.encode('ascii'))
            with path.open('rb') as source:
                shutil.copyfileobj(source, stream, 8 * 1024**2)
            if size % 2:
                stream.write(b'\n')


def pack(output, files, control, epoch, work):
    """Explicit tar ownership and modes work identically on Windows and Linux."""
    # Resources are already compressed; a raw data.tar adds about 1% and avoids
    # spending minutes recompressing every unchanged image/runtime on each fix.
    data_path, control_path = work / 'data.tar', work / 'control.tar.gz'
    directories = set()
    for name in files:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name:
            raise RuntimeError('Invalid package path: ' + name)
        directories.update(p.as_posix() for p in path.parents if p.as_posix() != '.')
    with data_path.open('wb') as raw:
        with tarfile.open(fileobj=raw, mode='w|', format=tarfile.GNU_FORMAT) as archive:
            for name in sorted(directories):
                info = tarfile.TarInfo('./' + name)
                info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, epoch
                archive.addfile(info)
            for name, (source, mode) in sorted(files.items()):
                if isinstance(source, bytes):
                    add_bytes(archive, name, source, epoch, mode)
                else:
                    info = tarfile.TarInfo('./' + name)
                    info.size, info.mtime, info.mode = source.stat().st_size, epoch, mode
                    with source.open('rb') as stream:
                        archive.addfile(info, stream)
    with control_path.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=epoch, compresslevel=1) as zipped:
        with tarfile.open(fileobj=zipped, mode='w') as archive:
            for name, (content, mode) in control.items():
                add_bytes(archive, name, content, epoch, mode)
    binary = work / 'debian-binary'
    binary.write_bytes(b'2.0\n')
    write_ar(output, [('debian-binary', binary), ('control.tar.gz', control_path), ('data.tar', data_path)], epoch)


def overlay_bundle(source, reference, destination):
    """Use tested current installer code while preserving the base resource bytes."""
    for prefix in ('catalog', 'gui-runtime', 'support-files'):
        def inventory(root):
            result = {}
            for path in (root / prefix).rglob('*'):
                if path.is_symlink():
                    raise RuntimeError('Immutable bundle resources cannot contain symlinks')
                if path.is_file():
                    with path.open('rb') as stream:
                        result[path.relative_to(root).as_posix()] = hashlib.file_digest(stream, 'sha256').hexdigest()
            return result
        if inventory(source) != inventory(reference):
            raise RuntimeError('Debian code revision cannot change immutable bundle resources: ' + prefix)
    if (source / 'sources.json').read_bytes() != (reference / 'sources.json').read_bytes():
        raise RuntimeError('Debian code revision must preserve the base source routing')
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns('__pycache__', '*.pyc',
                                                                   'installer-manifest.json', 'SHA256SUMS'))


def metadata(version, package_version, date):
    desktop = ('[Desktop Entry]\nVersion=1.0\nType=Application\nName=AI Platform Installer\n'
               f'Name[zh_CN]=AI 平台安装器 {version}\nComment=Install the probe, selected components and models\n'
               'Comment[zh_CN]=按探针、所选组件、所选模型的顺序安装\nExec=/usr/bin/ai-platform-installer\n'
               'Icon=system-software-install\nTerminal=false\nCategories=System;\nStartupNotify=false\n')
    appstream = f'''<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>com.ai-platform.Installer</id>
  <metadata_license>CC0-1.0</metadata_license>
  <project_license>LicenseRef-proprietary</project_license>
  <name>AI Platform Installer</name><name xml:lang="zh_CN">AI 平台安装器</name>
  <summary>Install the device probe, components and AI models</summary>
  <summary xml:lang="zh_CN">安装设备探针、所选组件库和 AI 模型</summary>
  <description><p>A graphical installation wizard for Debian 12 ARM64 devices. Installs the device probe, selected components and selected models in sequence.</p></description>
  <launchable type="desktop-id">com.ai-platform.Installer.desktop</launchable>
  <provides><binary>ai-platform-installer</binary></provides>
  <releases><release version="{package_version}" date="{date}"/></releases>
  <content_rating type="oars-1.1"/>
</component>
'''
    return desktop.encode('utf-8'), appstream.encode('utf-8')


def build(root, revision='1'):
    import release
    if not re.fullmatch(r'[1-9][0-9]*', revision):
        raise RuntimeError('Debian revision must be a positive integer')
    if release.git(root, 'status', '--porcelain'):
        raise RuntimeError('Commit source changes before building a traceable Debian package')
    config = release.config(root)
    version, commit = config['installer_version'], release.git(root, 'rev-parse', 'HEAD')
    tests = release.read(root / 'build/validation/tests.json')
    if not tests.get('passed') or tests.get('source_dirty') or tests.get('skipped') or tests.get('source_commit') != commit:
        raise RuntimeError('Required tests must pass without skips against this exact clean source commit')
    package_version = version + '-' + revision
    name = f'ai-platform-installer_{package_version}_arm64.deb'
    output = root / 'delivery' / name
    checksum = output.with_name(name + '.sha256')
    record_path = root / 'releases' / f'deb-{package_version}.json'
    if any(path.exists() for path in (output, checksum, record_path)):
        raise RuntimeError('Debian revision already published; never overwrite, use --deb-revision')
    delivery = root / 'delivery' / f'AI-Platform-{version}-linux-arm64'
    base_record = release.read(root / 'releases' / f'{version}.json')
    print('Verifying the immutable base delivery and its release identity...', flush=True)
    release.verify_checksums(delivery)
    base = release.read(delivery / 'provenance.json')
    for key in ('source_commit', 'installer_version', 'resource_release_id', 'catalog_sha256', 'sources_sha256'):
        if base.get(key) != base_record.get(key):
            raise RuntimeError('Base delivery identity differs from its release record: ' + key)
    archive = root / base_record['archive']
    if release.digest(archive) != base_record['archive_sha256']:
        raise RuntimeError('Original delivery archive checksum mismatch')
    release.verify_archive(archive, delivery)
    if base['installer_version'] != version or base['resource_release_id'] != config['resource_release_id']:
        raise RuntimeError('Debian package must wrap the current resource release')
    epoch = int(release.git(root, 'show', '-s', '--format=%ct', commit))
    work = Path(tempfile.mkdtemp(prefix=f'deb-{package_version}-', dir=root / 'build'))
    reference = work / 'base-bundle'
    launcher = delivery / f'安装AI平台-{version}.run'
    subprocess.run([sys.executable, '-B', str(launcher), '--extract-only', str(reference)], check=True)
    if release.read(reference / 'release.json')['source_commit'] != base['source_commit']:
        raise RuntimeError('Extracted bundle does not belong to the base release')
    preflight = release.read(root / 'build/validation/preflight.json')
    if preflight.get('source_commit') != commit or not preflight.get('signed_catalog_verified'):
        raise RuntimeError('Run release.py check --online against this exact source commit')
    if config.get('npu_runtime_release_id') and (not preflight.get('npu_runtime_signed') or preflight.get('npu_runtime_release_id')!=config['npu_runtime_release_id']):
        raise RuntimeError('Offline NPU runtime must pass trusted signature and content checks')
    bundle = work / 'bundle'
    overlay_bundle(root / config['source_bundle'], reference, bundle)
    metadata_release = release.read(reference / 'release.json')
    metadata_release.update(source_commit=commit, debian_revision=revision,
                            resource_source_commit=base['source_commit'],
                            built_at=datetime.fromtimestamp(epoch, timezone.utc).isoformat())
    if config.get('npu_runtime_release_id'):
        metadata_release.update(npu_runtime_release_id=config['npu_runtime_release_id'],
                                npu_runtime_manifest_sha256=release.digest(bundle/'npu-runtime/manifest.json'))
    if config.get('component_runtime_release_id'):
        if not preflight.get('component_runtime_signed'):raise RuntimeError('Component runtime signature was not verified')
        metadata_release.update(component_runtime_release_id=config['component_runtime_release_id'],
                                component_runtime_manifest_sha256=release.digest(bundle/'component-runtime/manifest.json'))
    metadata_release['validation']['automated_tests'] = str(tests['tests_run']) + ' passed'
    release.write(bundle / 'release.json', metadata_release)
    subprocess.run([sys.executable, '-B', str(bundle / 'tools/seal_installer.py'),
                    '--bundle', str(bundle)], check=True)
    prefix = f'opt/ai-platform-installer/{version}'
    files = {prefix + '/bundle/' + p.relative_to(bundle).as_posix(): (p, 0o644)
             for p in bundle.rglob('*') if p.is_file()}
    for source in delivery.rglob('*'):
        if not source.is_file():
            continue
        relative = source.relative_to(delivery).as_posix()
        if relative.startswith('support-files/'):
            target = prefix + '/' + relative
        elif source.parent == delivery and (source.suffix in ('.run', '.desktop', '.sh') or source.name.startswith(launcher.name)):
            continue
        elif source.name.endswith('.sources.json'):
            continue
        else:
            target = prefix + '/delivery-info/' + relative
        files[target] = (source, 0o644)
    files[prefix + '/debian_entry.py'] = (root / 'tools/debian_entry.py', 0o644)
    files['usr/bin/ai-platform-installer'] = (
        f'#!/bin/sh\nexec /usr/bin/python3 -B /{prefix}/debian_entry.py "$@"\n'.encode(), 0o755)
    desktop, appstream = metadata(version, package_version, datetime.fromtimestamp(epoch, timezone.utc).date().isoformat())
    files['usr/share/applications/com.ai-platform.Installer.desktop'] = (desktop, 0o644)
    files['usr/share/metainfo/com.ai-platform.Installer.metainfo.xml'] = (appstream, 0o644)
    files['usr/share/doc/ai-platform-installer/README.md'] = (root / 'tools/templates/DEBIAN-README.md', 0o644)
    provenance = {'schema': 1, 'package': 'ai-platform-installer', 'package_version': package_version,
                  'architecture': 'arm64', 'source_commit': commit, 'installer_version': version,
                  'resource_release_id': base['resource_release_id'], 'payload_source_commit': commit,
                  'resource_source_commit': base['source_commit'], 'code_overlay': True,
                  'base_archive_sha256': base_record['archive_sha256'],
                  'base_delivery_checksums_sha256': release.digest(delivery / 'SHA256SUMS'),
                  'catalog_sha256': base['catalog_sha256'], 'test_report_sha256': release.digest(root / 'build/validation/tests.json'),
                  'model_weights_in_distribution': False, 'fresh_device_acceptance': 'not_run'}
    if config.get('npu_runtime_release_id'):
        provenance.update(npu_runtime_release_id=config['npu_runtime_release_id'],
                          npu_runtime_manifest_sha256=release.digest(bundle/'npu-runtime/manifest.json'),
                          npu_runtime_archive_sha256=preflight['npu_runtime_archive_sha256'],
                          npu_runtime_offline_bytes=preflight['npu_runtime_archive_bytes'])
    if config.get('component_runtime_release_id'):
        provenance.update(component_runtime_release_id=config['component_runtime_release_id'],
                          component_runtime_manifest_sha256=release.digest(bundle/'component-runtime/manifest.json'))
    files[prefix + '/debian-provenance.json'] = ((json.dumps(provenance, ensure_ascii=False, indent=2) + '\n').encode(), 0o644)
    files['usr/share/doc/ai-platform-installer/packaging-tests.json'] = (root / 'build/validation/tests.json', 0o644)
    md5sums, manifest, installed_size = [], [], 0
    print('Hashing native package contents...', flush=True)
    for target, (source, mode) in sorted(files.items()):
        if isinstance(source, bytes):
            sha, md5, size = hashlib.sha256(source).hexdigest(), hashlib.md5(source).hexdigest(), len(source)
        else:
            with source.open('rb') as stream:
                sha = hashlib.file_digest(stream, 'sha256').hexdigest()
            with source.open('rb') as stream:
                md5 = hashlib.file_digest(stream, 'md5').hexdigest()
            size = source.stat().st_size
        installed_size += (size + 1023) // 1024
        md5sums.append(md5 + '  ' + target + '\n')
        manifest.append({'path': target, 'sha256': sha, 'bytes': size, 'mode': mode})
    manifest_bytes = (json.dumps({'files': manifest}, ensure_ascii=False, indent=2) + '\n').encode()
    files['usr/share/doc/ai-platform-installer/package-files.json'] = (manifest_bytes, 0o644)
    md5sums.append(hashlib.md5(manifest_bytes).hexdigest() + '  usr/share/doc/ai-platform-installer/package-files.json\n')
    installed_size += (len(manifest_bytes) + 1023) // 1024
    control = f'''Package: ai-platform-installer
Version: {package_version}
Architecture: arm64
Section: admin
Priority: optional
Maintainer: AI Platform Team <ai-platform@example.invalid>
Depends: python3 (>= 3.11), python3 (<< 3.12), libc6 (>= 2.36)
Installed-Size: {installed_size}
Description: AI Platform graphical installer for Debian 12 ARM64
 Installs the graphical wizard and its verified runtime resources.
 Launch AI Platform Installer from the system applications menu.
 Probe, component and model installation starts only inside the wizard.
'''
    controls = {'control': (control.encode(), 0o644), 'md5sums': (''.join(md5sums).encode(), 0o644)}
    for script, condition in (('postinst', '[ "$1" = configure ]'), ('postrm', '[ "$1" = remove ] || [ "$1" = purge ]')):
        content = f'#!/bin/sh\nset -e\nif {condition}; then\n  if command -v update-desktop-database >/dev/null 2>&1; then\n    update-desktop-database -q /usr/share/applications\n  fi\nfi\nexit 0\n'
        controls[script] = (content.encode(), 0o755)
    artifact = work / name
    print('Creating the Debian package with normalized Linux permissions...', flush=True)
    pack(artifact, files, controls, epoch, work)
    sha = release.digest(artifact)
    artifact.rename(output)
    checksum.write_text(sha + '  ' + name + '\n', encoding='utf-8', newline='\n')
    record = dict(provenance, archive=output.relative_to(root).as_posix(), archive_bytes=output.stat().st_size,
                  archive_sha256=sha, installed_size_kib=installed_size, packaged_files=len(files), automated_tests=tests['tests_run'])
    release.write(record_path, record)
    print(json.dumps(record, ensure_ascii=False, indent=2), flush=True)
