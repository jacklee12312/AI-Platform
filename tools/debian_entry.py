"""Installed Debian application entry; mutable sources belong to the current user."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def verify_bundle(bundle):
    manifest = json.loads((bundle / 'installer-manifest.json').read_text('utf-8'))
    for row in manifest['files']:
        path = bundle / row['path']
        if not path.resolve().is_relative_to(bundle.resolve()) or path.is_symlink() or not path.is_file():
            raise RuntimeError('安装器文件校验失败：' + row['path'])
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != row['sha256']:
                raise RuntimeError('安装器文件校验失败：' + row['path'])


def prepare_sources(root, home, environ):
    version = json.loads((root / 'bundle/release.json').read_text('utf-8'))['installer_version']
    config_home = Path(environ.get('XDG_CONFIG_HOME') or home / '.config')
    if not config_home.is_absolute():
        config_home = home / '.config'
    target = config_home / 'ai-platform-installer' / version / 'sources.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise RuntimeError('来源配置不能是符号链接：' + str(target))
    if not target.exists():
        value = json.loads((root / 'bundle/sources.json').read_text('utf-8'))
        value['local_roots'] = ['@bundle/support-files', str(root / 'support-files'),
                                str(home / 'AI-Platform-Model-Library')]
        try:
            with target.open('x', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
        except FileExistsError:
            pass
    return target


def main():
    root = Path(__file__).resolve().parent
    bundle = root / 'bundle'
    verify_bundle(bundle)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    env['PATH'] = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    env['AI_PLATFORM_SOURCES_FILE'] = str(prepare_sources(root, Path.home(), env))
    env['AI_PLATFORM_SOURCE_BASE'] = str(root)
    if sys.argv[1:] == ['--check']:
        print(json.dumps({'verified': True, 'bundle': str(bundle),
                          'sources_file': env['AI_PLATFORM_SOURCES_FILE'],
                          'installation_started': False}, ensure_ascii=False))
        return 0
    return subprocess.call([sys.executable, '-B', str(bundle / 'installer/bootstrap.py'),
                            *sys.argv[1:]], env=env)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('启动失败：' + str(exc), file=sys.stderr)
        if shutil.which('zenity') and (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
            subprocess.run(['zenity', '--error', '--title=AI 平台安装器', '--text=' + str(exc)])
        sys.exit(1)
