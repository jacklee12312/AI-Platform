"""Single entry point for checked, traceable Linux ARM64 installer releases."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inside(root, name):
    relative = PurePosixPath(name)
    if not name or relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name:
        raise RuntimeError('Invalid relative path: ' + name)
    result = Path(root) / name
    if not result.resolve().is_relative_to(Path(root).resolve()):
        raise RuntimeError('Path escapes project: ' + name)
    return result


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def config(root):
    value = read(root / 'release-config.json')
    if value.get('schema') != 1 or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value.get('installer_version', '')):
        raise RuntimeError('release-config.json requires a stable major.minor.patch version')
    if value.get('target') != 'linux-arm64' or value.get('validation_status') != 'candidate':
        raise RuntimeError('This workflow produces linux-arm64 candidates; device acceptance is recorded separately')
    inside(root, value['source_bundle'])
    inside(root, value['resources_root'])
    return value


def installer_modules(bundle):
    sys.path.insert(0, str(bundle / 'installer'))
    return importlib.import_module('artifacts'), importlib.import_module('registry')


def check_assets(root, store=None):
    rows = read(root / 'external-assets.json')['files']
    for row in rows:
        target = inside(root, row['path'])
        source = inside(store, row['path']) if store else target
        if source.is_symlink() or not source.is_file() or source.stat().st_size != row['bytes'] or digest(source) != row['sha256']:
            raise RuntimeError('Missing or changed external asset: ' + row['path'])
        if store and source.resolve() != target.resolve():
            if target.exists():
                if target.is_symlink() or digest(target) != row['sha256']:
                    raise RuntimeError('Refusing to overwrite a different local asset: ' + row['path'])
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
    return len(rows)


def resource_rows(bundle,catalog):
    from workflow import load_workflow,workflow_assets
    workflow=load_workflow(bundle,catalog)
    return catalog['resources']+([dict(row,category='platform') for row in workflow_assets(workflow, include_images=False)] if workflow else [])


def check(root, online=False):
    spec = config(root)
    bundle = inside(root, spec['source_bundle'])
    assets = check_assets(root)
    artifacts, registry = installer_modules(bundle)
    catalog = artifacts.load_catalog(bundle)
    sources = artifacts.source_config(bundle)
    if catalog['release_id'] != spec['resource_release_id']:
        raise RuntimeError('Resource release ID differs from the signed catalog; a resource change needs an explicit signed release')
    if read(bundle / 'release.json')['installer_version'] != spec['installer_version']:
        raise RuntimeError('Source release.json installer_version differs from release-config.json')
    if sources != read(bundle / 'sources.example.json'):
        raise RuntimeError('sources.example.json differs from the published source routing')
    # All configured OCI blobs must agree with their signed resource rows.
    resources = {r['path']: r for r in catalog['resources']}
    for key, source in sources['oci_artifacts'].items():
        for path in source['files']:
            row = resources[path]
            if source['format'] == 'blob' and (source['blob_digest'] != 'sha256:' + row['sha256'] or source['blob_size'] != row['size']):
                raise RuntimeError('Source content differs from signed resource: ' + key)
    local_count = 0
    tracked = set(subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z']).decode('utf-8').split('\0'))
    external = {row['path'] for row in read(root / 'external-assets.json')['files']}
    for row in resource_rows(bundle,catalog):
        if row['category'] == 'models':
            continue
        path = inside(inside(root, spec['resources_root']), row['path'])
        if path.relative_to(root).as_posix() not in tracked | external:
            raise RuntimeError('Required resource is neither tracked nor locked as an external asset: ' + row['path'])
        if path.is_symlink() or not path.is_file() or path.stat().st_size != row['size'] or digest(path) != row['sha256']:
            raise RuntimeError('Local resource differs from signed catalog: ' + row['path'])
        local_count += 1
    baseline = read(root / 'releases/1.0.0.json')
    old_archive = inside(root, baseline['archive'])
    baseline_verified = False
    if old_archive.exists():
        if old_archive.stat().st_size != baseline['archive_bytes'] or digest(old_archive) != baseline['archive_sha256']:
            raise RuntimeError('Original 1.0.0 delivery archive has changed')
        baseline_verified = True
    report = {'schema': 1, 'source_commit': git(root, 'rev-parse', 'HEAD'),
              'installer_version': spec['installer_version'], 'resource_release_id': catalog['release_id'],
              'external_assets_verified': assets, 'local_resources_verified': local_count,
              'signed_catalog_verified': True, 'original_1_0_0_archive_verified': baseline_verified,
              'sources_sha256': digest(bundle / 'sources.json'), 'remote_checks': [],
              'full_model_download': 'not_run', 'fresh_device_install': 'not_run', 'reboot': 'not_run'}
    if online:
        def probe(item):
            key, source = item
            try:
                registry.RegistryClient(source, timeout=10).verify_manifest()
                return {'key': key, 'repository': source['repository'], 'passed': True}
            except Exception as exc:
                return {'key': key, 'repository': source['repository'], 'passed': False, 'detail': str(exc)}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            report['remote_checks'] = list(pool.map(probe, sources['oci_artifacts'].items()))
        report['remote_checked_at'] = datetime.now(timezone.utc).isoformat()
        report['all_configured_remote_models_ready'] = all(row['passed'] for row in report['remote_checks'])
    return report


def sync_version(root):
    spec = config(root)
    bundle = inside(root, spec['source_bundle'])
    metadata = read(bundle / 'release.json')
    if metadata['release_id'] != spec['resource_release_id']:
        raise RuntimeError('Source routing/version sync cannot change signed resource identity')
    metadata['installer_version'] = spec['installer_version']
    write(bundle / 'release.json', metadata)
    directory = 'AI-Platform-' + spec['installer_version'] + '-linux-arm64'
    for template, destination in [('README.md.in', 'README.md'), ('installer.desktop.in', '安装AI平台.desktop')]:
        text = (root / 'tools/templates' / template).read_text(encoding='utf-8')
        text = text.replace('@VERSION@', spec['installer_version']).replace('@DELIVERY_DIR@', directory)
        (bundle / destination).write_text(text, encoding='utf-8', newline='\n')
    print('Synced derived metadata from release-config.json: ' + spec['installer_version'])


def verify_checksums(root):
    rows = (root / 'SHA256SUMS').read_text(encoding='utf-8').splitlines()
    listed = set()
    for line in rows:
        expected, name = line.split('  ', 1)
        path = inside(root, name)
        if name in listed or path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise RuntimeError('Delivery checksum mismatch: ' + name)
        listed.add(name)
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and p != root / 'SHA256SUMS'}
    if actual != listed:
        raise RuntimeError('Delivery contains unlisted or missing files')
    return len(listed)


def verify_archive(archive, delivery):
    checked = 0
    with tarfile.open(archive, 'r:gz') as tar:
        seen = set()
        for member in tar:
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or member.name in seen or name.parts[0] != delivery.name:
                raise RuntimeError('Unexpected archive path: ' + member.name)
            seen.add(member.name)
            if member.isdir():
                continue
            if not member.isfile():
                raise RuntimeError('Unexpected archive member type: ' + member.name)
            source = inside(delivery, PurePosixPath(*name.parts[1:]).as_posix())
            with tar.extractfile(member) as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != digest(source):
                raise RuntimeError('Archive content mismatch: ' + member.name)
            expected_mode = 0o755 if source.suffix in ('.run', '.sh', '.desktop') else 0o644
            if member.mode != expected_mode:
                raise RuntimeError('Archive mode mismatch: ' + member.name)
            checked += 1
    if checked != sum(p.is_file() for p in delivery.rglob('*')):
        raise RuntimeError('Archive does not contain the complete delivery')
    return checked


def build(root, online=False):
    if git(root, 'status', '--porcelain'):
        raise RuntimeError('Commit source changes before building a traceable delivery')
    spec = config(root)
    version = spec['installer_version']
    commit = git(root, 'rev-parse', 'HEAD')
    test_path = root / 'build/validation/tests.json'
    if not test_path.is_file():
        raise RuntimeError('Run python tools/release.py test before building')
    tests = read(test_path)
    if not tests.get('passed') or tests.get('source_dirty') or tests.get('source_commit') != commit or tests.get('skipped'):
        raise RuntimeError('Required tests must pass without skips against this exact clean source commit')
    delivery = root / 'delivery' / f'AI-Platform-{version}-linux-arm64'
    archive = delivery.with_name(delivery.name + '.tar.gz')
    record_path = root / 'releases' / (version + '.json')
    if delivery.exists() or archive.exists() or archive.with_name(archive.name + '.sha256').exists() or record_path.exists():
        raise RuntimeError('Version already has output; use a new version, never overwrite a published release')
    workflow_reports=[]
    if read(inside(root,spec['source_bundle'])/'catalog/release-catalog.json').get('workflow_sha256'):
        workflow_lock=read(inside(root,spec['source_bundle'])/'catalog/workflow.json')
        image_report='component-sources.json' if workflow_lock.get('image_acquisition','').startswith('on-demand') else 'component-archives.json'
        required_reports=['workflow-artifacts.json',image_report]
        if version=='1.2.0':required_reports+=['model-supply.json','modeldeck-ui.json']
        if workflow_lock.get('image_acquisition','').startswith('on-demand'):required_reports.append('platform-images.json')
        for name in required_reports:
            path=root/'build/validation'/name
            if not path.is_file():raise RuntimeError('Run the prerequisite artifact validation: '+name)
            report=read(path)
            if not report.get('passed') or report.get('source_dirty') or report.get('source_commit')!=commit:
                raise RuntimeError('Prerequisite artifacts must be validated against this exact clean commit: '+name)
            workflow_reports.append(path)
    print('Checking signatures, assets, local resources and original delivery...', flush=True)
    checks = check(root, online)
    work = root / 'build'
    work.mkdir(exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f'release-{version}-', dir=work))
    bundle = stage / 'installer-bundle'
    shutil.copytree(inside(root, spec['source_bundle']), bundle, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'installer-manifest.json', 'SHA256SUMS'))
    release = read(bundle / 'release.json')
    epoch = int(git(root, 'show', '-s', '--format=%ct', commit))
    build_time = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    release.update(installer_version=version, resource_release_id=spec['resource_release_id'],
                   source_commit=commit, built_at=build_time, status='candidate')
    release['validation'] = {'automated_tests': f"{tests['tests_run']} passed", 'remote_models': checks.get('all_configured_remote_models_ready', 'not_run'),
                             'full_model_download': 'not_run', 'fresh_install': 'not_run', 'reboot': 'not_run'}
    write(bundle / 'release.json', release)
    desktop = (root / 'tools/templates/installer.desktop.in').read_text(encoding='utf-8').replace('@DELIVERY_DIR@', delivery.name)
    (bundle / '安装AI平台.desktop').write_text(desktop, encoding='utf-8', newline='\n')
    command = [sys.executable, '-B']
    subprocess.run(command + [str(bundle / 'tools/seal_installer.py'), '--bundle', str(bundle)], check=True)
    assembled = stage / delivery.name
    assembled.mkdir()
    launcher = assembled / f'安装AI平台-{version}.run'
    env = dict(os.environ, SOURCE_DATE_EPOCH=str(epoch))
    subprocess.run(command + [str(bundle / 'tools/pack_launcher_023.py'), '--bundle', str(bundle), '--output', str(launcher)], check=True, env=env)
    print('Assembling the delivery with verified local resources...', flush=True)
    artifacts, _ = installer_modules(bundle)
    for row in resource_rows(bundle,artifacts.load_catalog(bundle)):
        if row['category'] == 'models':
            continue
        source = inside(inside(root, spec['resources_root']), row['path'])
        target = inside(assembled / 'support-files/files', row['path'])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    if (bundle / 'tools/upgrade_modeldeck.py').is_file():
        upgrade = '#!/bin/sh\nset -eu\ncd -- "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\n' + 'work=$(mktemp -d /var/tmp/modeldeck-upgrade.XXXXXX)\n' + f'python3 "./{launcher.name}" --extract-only "$work/bundle"\n' + 'exec python3 "$work/bundle/tools/upgrade_modeldeck.py" --bundle "$work/bundle" --resources "$PWD/support-files/files" "$@"\n'
        (assembled / '升级ModelDeck.sh').write_text(upgrade, encoding='utf-8', newline='\n')
    shutil.copyfile(bundle / '安装AI平台.desktop', assembled / '安装AI平台.desktop')
    (assembled / '启动安装.sh').write_text('#!/bin/sh\nset -eu\ncd -- "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\nexec python3 "./' + launcher.name + '" "$@"\n', encoding='utf-8', newline='\n')
    readme = (root / 'tools/templates/README.md.in').read_text(encoding='utf-8').replace('@VERSION@', version).replace('@DELIVERY_DIR@', delivery.name)
    (assembled / 'README.md').write_text(readme, encoding='utf-8', newline='\n')
    for source, target in [('CHANGELOG.md', 'CHANGELOG.md'), ('模型上传清单.md', '模型上传清单.md')]:
        shutil.copyfile(root / source, assembled / target)
    write(assembled / 'validation/tests.json', tests)
    shutil.copyfile(root / 'build/validation/tests.log', assembled / 'validation/tests.log')
    for report in workflow_reports:shutil.copyfile(report,assembled/'validation'/report.name)
    if version=='1.2.0':
        for name in read(root/'build/validation/modeldeck-ui.json').get('screenshots',[]):
            shutil.copyfile(root/'build/validation'/name,assembled/'validation'/name)
    for relative in ['workflow-settings.example.json','docs/1.1安装流程.md']:
        source=bundle/relative
        if source.is_file():shutil.copyfile(source,assembled/source.name)
    write(assembled / 'validation/preflight.json', checks)
    provenance = {'schema': 1, 'installer_version': version, 'resource_release_id': spec['resource_release_id'],
                  'source_commit': commit, 'source_tag': 'v' + version, 'build_time': build_time,
                  'validation_status': 'candidate', 'sources_sha256': digest(bundle / 'sources.json'),
                  'catalog_sha256': release['catalog_sha256'], 'external_assets_lock_sha256': digest(root / 'external-assets.json'),
                  'test_report_sha256': digest(test_path), 'release_config_sha256': digest(root / 'release-config.json'),
                  'model_weights_in_distribution': False}
    write(assembled / 'provenance.json', provenance)
    warnings = [row for row in checks['remote_checks'] if not row['passed']]
    lines = [f'# AI 平台安装器 {version} 验证记录', '', f'源码提交：`{commit}`。自动化测试：{tests["tests_run"]} 项通过，无跳过。',
             '', '签名、外置资源摘要、来源映射和交付文件校验通过。', '', '完整模型下载、空白设备安装、真实推理及重启验收：本次未执行。',
             '', '不跨版本接管已有安装任务；1.0.x 任务请继续使用对应版本。',
             '', '本版安装顺序：探针自动识别及 MQTT 检查 → 所选组件库认证与健康检查 → 所选模型安装与真实推理检查。已有兼容探针时隐藏注册表单并复用，没有探针时强制安装；Milvus 自动包含 MinIO 与 etcd。',
             '', 'MinIO 使用 dappros/minio:RELEASE.2025-09-07T16-13-09Z，ARM64 清单及配置摘要固定在签名工作流中。']
    if warnings:
        lines += ['', '远端检查未通过的项目（候选包保留这些选项，请在使用前补齐或取消勾选）：']
        lines += ['- ' + row['key'] + '：' + row['detail'] for row in warnings]
    (assembled / '验证记录.md').write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\n')
    files = sorted(p for p in assembled.rglob('*') if p.is_file())
    (assembled / 'SHA256SUMS').write_text(''.join(digest(p) + '  ' + p.relative_to(assembled).as_posix() + '\n' for p in files), encoding='utf-8', newline='\n')
    verify_checksums(assembled)
    print('Checking the packed launcher through its real extraction entry point...', flush=True)
    subprocess.run(command + [str(launcher), '--extract-only', str(stage / 'extracted')], check=True)
    extracted = stage / 'extracted'
    if read(extracted / 'sources.json') != read(launcher.with_suffix('.sources.json')) or read(extracted / 'release.json')['source_commit'] != commit:
        raise RuntimeError('Packed launcher metadata differs from published sidecars')
    artifact = stage / archive.name
    print('Creating the full tar.gz archive...', flush=True)
    with tarfile.open(artifact, 'w:gz', compresslevel=1) as tar:
        def normalize(info):
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mtime = epoch
            info.mode = 0o755 if info.isdir() or info.name.endswith(('.run', '.sh', '.desktop')) else 0o644
            return info
        tar.add(assembled, arcname=assembled.name, filter=normalize)
    print('Verifying every archived file and Linux launch permissions...', flush=True)
    archive_files = verify_archive(artifact, assembled)
    archive_sha = digest(artifact)
    delivery.parent.mkdir(exist_ok=True)
    assembled.rename(delivery)
    artifact.rename(archive)
    archive.with_name(archive.name + '.sha256').write_text(archive_sha + '  ' + archive.name + '\n', encoding='utf-8', newline='\n')
    record = dict(provenance, archive=archive.relative_to(root).as_posix(), archive_bytes=archive.stat().st_size,
                  archive_sha256=archive_sha, distribution=delivery.relative_to(root).as_posix(),
                  verified_archive_files=archive_files, automated_tests=tests['tests_run'],
                  blocked_remote_models=[row['key'] for row in warnings], fresh_device_acceptance='not_run')
    write(record_path, record)
    # Existing 1.0.0 tasks can consume the corrected mutable source routing without changing their installer.
    if version=='1.0.1':
        recovery = delivery.parent / 'AI-Platform-1.0.0-mmproj-source-fix'
        recovery.mkdir(exist_ok=True)
        recovery_source = recovery / '安装AI平台-1.0.0.sources.json'
        if recovery_source.exists() and digest(recovery_source) != digest(bundle / 'sources.json'):
            raise RuntimeError('A different 1.0.0 recovery source patch already exists')
        shutil.copyfile(bundle / 'sources.json', recovery_source)
        (recovery / 'README.txt').write_text('仅供现有 1.0.0 安装任务恢复：先备份目标设备原来的同名 sources.json，再以此文件替换 .run 旁边的来源配置。保留原 1.0.0 .run 和 support-files；重新检查后继续。此补丁仅修正 Qwen3.8 单双卡 mmproj 来源，模型摘要不变。\n', encoding='utf-8')
    print(json.dumps(record, ensure_ascii=False, indent=2), flush=True)


def main():
    # POSIX/systemd tests run in WSL; large artifact hashing/packing stays native on Windows.
    if os.name == 'nt' and sys.argv[1:2] == ['test']:
        if not shutil.which('wsl.exe'):
            raise SystemExit('This project needs WSL Linux for release checks; see README.md')
        script = subprocess.check_output(['wsl.exe', '-e', 'wslpath', '-a', str(Path(__file__).resolve())], text=True).strip()
        raise SystemExit(subprocess.call(['wsl.exe', '-e', 'python3', '-B', script, *sys.argv[1:]]))
    if os.name == 'nt' and not shutil.which('openssl'):
        executable = shutil.which('git')
        candidate = Path(executable).resolve().parents[1] / 'usr/bin/openssl.exe' if executable else None
        if candidate and candidate.is_file():
            os.environ['PATH'] = str(candidate.parent) + os.pathsep + os.environ.get('PATH', '')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['check', 'test', 'build', 'deb', 'verify', 'assets', 'sync-version'])
    parser.add_argument('--deb-revision', default='1', help='Debian packaging revision; immutable once published')
    parser.add_argument('--online', action='store_true', help='Read-only remote manifest checks; no model weight download')
    parser.add_argument('--path', type=Path, help='Delivery directory for verify; external asset store for assets')
    args = parser.parse_args()
    if args.command == 'check':
        report = check(ROOT, args.online)
        write(ROOT / 'build/validation/preflight.json', report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if args.online and not report['all_configured_remote_models_ready']:
            raise SystemExit(2)
    elif args.command == 'test':
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONDONTWRITEBYTECODE='1')
        extra = str(ROOT / 'build/test-python')
        env['PYTHONPATH'] = extra + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
        subprocess.run([sys.executable, '-B', str(ROOT / 'tools/run_tests.py')], env=env, check=True)
    elif args.command == 'build':
        build(ROOT, args.online)
    elif args.command == 'deb':
        from debian import build as build_debian
        build_debian(ROOT, args.deb_revision)
    elif args.command == 'verify':
        if not args.path:
            parser.error('verify requires --path')
        print(json.dumps({'verified_files': verify_checksums(args.path.resolve())}))
    elif args.command == 'assets':
        if not args.path:
            parser.error('assets requires --path pointing to an asset store with the locked relative layout')
        print(json.dumps({'verified_external_assets': check_assets(ROOT, args.path.resolve())}))
    else:
        sync_version(ROOT)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        print('Release failed: ' + str(exc), file=sys.stderr)
        sys.exit(1)
