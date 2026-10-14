"""Refresh the complete Linux delivery after sealing and packing the installer."""

import argparse
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def replace_copy(source, target):
    temporary = target.with_name('.' + target.name + '.new')
    if temporary.exists():
        raise RuntimeError(f'未清理的临时文件：{temporary}')
    shutil.copyfile(source, temporary)
    os.replace(temporary, target)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--packed-run', type=Path, required=True)
    parser.add_argument('--delivery', type=Path, required=True)
    args = parser.parse_args()
    bundle, packed, delivery = (p.resolve() for p in (args.bundle, args.packed_run, args.delivery))
    root = delivery / 'AI-Platform-1.0.0-linux-arm64'
    archive = delivery / (root.name + '.tar.gz')
    if not root.is_dir() or not archive.is_file():
        raise RuntimeError('必须先有完整的交付目录和旧归档')
    manifest = json.loads((bundle / 'installer-manifest.json').read_text(encoding='utf-8'))
    desktop = bundle / '安装AI平台.desktop'
    desktop_row = next((r for r in manifest['files'] if r['path'] == desktop.name), None)
    if desktop_row is None or sha256(desktop) != desktop_row['sha256']:
        raise RuntimeError('桌面启动文件未封装或校验失败')
    run_sha = sha256(packed)
    listed_sha = packed.with_suffix('.run.sha256').read_text(encoding='utf-8').split()[0]
    if run_sha != listed_sha:
        raise RuntimeError('新安装器校验失败')
    if sha256(bundle / 'sources.json') != sha256(packed.with_suffix('.sources.json')):
        raise RuntimeError('新安装器来源配置不一致')

    replace_copy(packed, root / packed.name)
    replace_copy(packed.with_suffix('.run.sha256'), root / (packed.name + '.sha256'))
    replace_copy(packed.with_suffix('.sources.json'), root / '安装AI平台-1.0.0.sources.json')
    replace_copy(bundle / 'README.md', root / 'README.md')
    replace_copy(desktop, root / desktop.name)

    files = sorted(p for p in root.rglob('*') if p.is_file() and p != root / 'SHA256SUMS')
    if any(p.is_symlink() for p in root.rglob('*')):
        raise RuntimeError('交付目录不能包含符号链接')
    checksum_lines = [f'{sha256(p)}  {p.relative_to(root).as_posix()}\n' for p in files]
    sums_tmp = root / '.SHA256SUMS.new'
    sums_tmp.write_text(''.join(checksum_lines), encoding='utf-8')
    os.replace(sums_tmp, root / 'SHA256SUMS')

    temp_fd, temp_name = tempfile.mkstemp(prefix='.ai-platform-desktop-', suffix='.tar.gz', dir=delivery)
    os.close(temp_fd)
    temp = Path(temp_name)
    try:
        with tarfile.open(temp, 'w:gz', compresslevel=1) as tar:
            def normalize(info):
                info.uid = info.gid = 0
                info.uname = info.gname = ''
                info.mtime = 0
                info.mode = 0o755 if info.isdir() or info.name.endswith(('.run', '.sh', '.desktop')) else 0o644
                return info

            tar.add(root, arcname=root.name, recursive=True, filter=normalize)
        expected = sha256(desktop)
        with tarfile.open(temp, 'r:gz') as tar:
            entry = tar.getmember(f'{root.name}/安装AI平台.desktop')
            if entry.mode != 0o755 or hashlib.sha256(tar.extractfile(entry).read()).hexdigest() != expected:
                raise RuntimeError('归档中的桌面文件不完整或不可执行')
        archive_sha = sha256(temp)
        os.replace(temp, archive)
    finally:
        if temp.exists():
            temp.unlink()

    sidecar = archive.with_name(archive.name + '.sha256')
    sidecar.write_text(f'{archive_sha}  {archive.name}\n', encoding='utf-8')
    result = {
        'directory': str(root),
        'archive': str(archive),
        'archive_bytes': archive.stat().st_size,
        'archive_sha256': archive_sha,
        'distribution_file_count': len(files) + 1,
        'distribution_bytes': sum(p.stat().st_size for p in files) + (root / 'SHA256SUMS').stat().st_size,
        'model_weights_in_distribution': False,
    }
    (delivery.parent / 'delivery-result.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
    )
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
