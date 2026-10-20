"""Explicit, repeatable installation; dpkg itself never starts a model load."""
from __future__ import annotations
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import socket
import sys

from .core import CONFIG, ROOT, LABEL, Controller, atomic, read, run, http
import time


def verify_bundle(bundle):
    release = read(bundle / 'catalog/manifest.json')
    key = bundle / 'catalog/release-public.pem'
    trusted = '2372e0f4636aad17b6a7bae4ccea27056ca43c3ffaa24563e588f4e889b88fe5'
    if hashlib.sha256(key.read_bytes()).hexdigest() != trusted or release['public_key_sha256'] != trusted:
        raise ValueError('安装包的发布公钥不匹配')
    run(['openssl', 'pkeyutl', '-verify', '-pubin', '-inkey', key, '-rawin', '-in', bundle / 'catalog/manifest.json',
         '-sigfile', bundle / 'catalog/manifest.sig'])
    expected = {row['path'] for row in release['files']}
    for row in release['files']:
        from .core import safe_file
        path = safe_file(bundle, row['path'])
        if path.stat().st_size != row['size']:
            raise ValueError('安装资源大小不符：' + row['path'])
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != row['sha256']:
                raise ValueError('安装资源摘要不符：' + row['path'])
    actual = {p.relative_to(bundle).as_posix() for p in bundle.rglob('*') if p.is_file()
              and not p.relative_to(bundle).as_posix().startswith('catalog/') and '__pycache__' not in p.parts}
    if expected != actual:
        raise ValueError('安装包包含未签名或缺失的文件')
    return release


def preflight(bundle):
    if platform.machine() != 'aarch64' or sys.version_info[:2] != (3, 12):
        raise RuntimeError('GB10 安装包需要 ARM64 Ubuntu 24.04 / Python 3.12')
    os_release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if os_release.get('ID', '').strip('"') != 'ubuntu' or os_release.get('VERSION_ID', '').strip('"') != '24.04':
        raise RuntimeError('当前已验证系统为 Ubuntu 24.04')
    gpu = run(['nvidia-smi', '--query-gpu=name,compute_cap', '--format=csv,noheader']).stdout.strip()
    if 'GB10' not in gpu or '12.1' not in gpu:
        raise RuntimeError('未检测到 NVIDIA GB10 SM 12.1')
    run(['docker', 'info'])
    release = verify_bundle(bundle)
    return {'gpu': gpu, 'resource_release_id': release['release_id'], 'docker': 'reuse', 'driver': 'reuse'}


def free_port(port):
    with socket.socket() as sock:
        try:
            sock.bind(('0.0.0.0', port))
        except OSError as exc:
            raise RuntimeError(f'端口 {port} 已被其他服务占用，未停止原服务') from exc


def platform_start(config, profile):
    controller = Controller(config, profile)
    controller.stop('ai-platform-gb10-platform')
    image = controller.image()
    command = ['docker', 'run', '-d', '--name', 'ai-platform-gb10-platform', '--label', LABEL + '=true',
               '--network', 'host', '--restart', 'unless-stopped', '--read-only', '--init',
               '--memory', '1536m', '--cpus', '3', '--tmpfs', '/tmp:size=128m,mode=1777',
               '--env-file', str(ROOT / 'platform.env'), '-v', config['bundle'] + ':/bundle:ro',
               '-v', str(ROOT) + ':/platform', '-v', '/run/ai-platform-gb10:/run/ai-platform-gb10:ro',
               image, 'python', '-B', '/bundle/gb10/platform.py']
    # Use module execution so relative imports remain valid.
    command[-3:] = ['python', '-B', '-m', 'gb10.platform']
    command[command.index('--env-file'):command.index('--env-file')] = ['-e', 'PYTHONPATH=/bundle:/bundle/python']
    run(command, timeout=90)


def configure(args, bundle):
    profile = read(bundle / 'profile.json')
    print(json.dumps(preflight(bundle), ensure_ascii=False), flush=True)
    model_root = Path(args.model_root).resolve(strict=True)
    cache = Path(args.cache_root).resolve(strict=True)
    tiktoken = Path(args.tiktoken_root).resolve(strict=True)
    for path in [model_root, cache, tiktoken]:
        if path == Path('/') or path == Path('/home'):
            raise ValueError('请提供明确的模型与缓存目录')
    existing = read(CONFIG) if CONFIG.exists() else None
    if existing and existing['model_root'] != str(model_root):
        raise ValueError('已有部署的模型目录不同，未自动更改')
    if not existing:
        for port in profile['ports'].values():
            free_port(port)
    config = existing or {'model_root': str(model_root), 'cache_root': str(cache), 'tiktoken_root': str(tiktoken),
                          'agent_token': secrets.token_urlsafe(32), 'api_key': 'sk-gb10-' + secrets.token_urlsafe(32),
                          'management_token': secrets.token_urlsafe(32), 'admin_password': secrets.token_urlsafe(18)}
    config['bundle'] = str(bundle)
    config.setdefault('install_user', os.environ.get('SUDO_USER') or 'root')
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic(ROOT / 'install-in-progress', 'installation\n')
    atomic(CONFIG, config)
    controller = Controller(config, profile)
    for mode in controller.models:
        try:
            if not existing:
                raise ValueError('first install')
            controller.verified(mode)
            print('复用已有完整校验记录：' + mode, flush=True)
        except (OSError, ValueError, KeyError):
            controller.verify_files(mode, lambda p: print(p['stage'], flush=True))
    atomic(ROOT / 'state/model-installations.json', {'schema_version': 1, 'models': {
        model['id']: {'installed': True, 'enabled': True} for model in profile['models']}}, 0o644)
    atomic(ROOT / 'state/active-mode', 'loading\n', 0o644)
    models = []
    for row in profile['models']:
        models.append({'id': row['id'], 'capability': 'chat', 'backend': 'openai',
                       'base_url': 'http://127.0.0.1:17701', 'upstream_model': row['id'], 'enabled': True,
                       'guard': False, 'modes': [row['mode']], 'metadata': {'device': 'NVIDIA GB10',
                       'display_name': row['display_name'], 'dashboard': True, 'exclusive': True,
                       'service': row['mode'], 'quantization': row['quantization'], 'context_length': 16384}})
    atomic(ROOT / 'config/models.json', {'services': {'hardware_profile': 'nvidia-gb10'}, 'models': models}, 0o644)
    from base64 import urlsafe_b64encode
    salt = secrets.token_bytes(18)
    password_hash = '$'.join(['pbkdf2_sha256', '260000', urlsafe_b64encode(salt).decode(),
        urlsafe_b64encode(hashlib.pbkdf2_hmac('sha256', config['admin_password'].encode(), salt, 260000)).decode()])
    values = {'GATEWAY_API_KEY': config['api_key'], 'GATEWAY_MANAGEMENT_TOKEN': config['management_token'],
              'ADMIN_AGENT_TOKEN': config['agent_token'], 'ADMIN_PASSWORD_HASH': password_hash,
              'MODEL_REGISTRY': '/platform/config/models.json', 'MODEL_MODE_FILE': '/platform/state/active-mode',
              'MODEL_MODE_DEFAULT': 'loading', 'MODEL_INSTALLATION_FILE': '/platform/state/model-installations.json',
              'GATEWAY_USAGE_DB': '/platform/data/gateway-usage.db', 'ADMIN_DATA_DIR': '/platform/admin-data',
              'ADMIN_STATIC_DIR': '/bundle/platform/gateway-admin/frontend/dist',
              'GATEWAY_URL': 'http://127.0.0.1:17700', 'ADMIN_AGENT_URL': 'http://127.0.0.1:17781',
              'GB10_BUNDLE': '/bundle', 'MODELDECK_HOME': '/platform', 'MODELDECK_STORE_ROOT': '/platform/model-store',
              'RUNTIME_SAFETY_ENABLED': 'false', 'RUNTIME_SAFETY_FILE': '/platform/state/safety.json',
              'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
              'CHAT_TIMEOUT_SECONDS': '300'}
    atomic(ROOT / 'platform.env', ''.join(key + '=' + value + '\n' for key, value in values.items()))
    for directory in ['data', 'admin-data', 'model-store/jobs']:
        (ROOT / directory).mkdir(parents=True, exist_ok=True)
    for name in ('controller', 'platform'):
        unit = (bundle / ('systemd/ai-platform-gb10-' + name + '.service')).read_text().replace('@BUNDLE@', str(bundle))
        atomic(Path('/etc/systemd/system') / ('ai-platform-gb10-' + name + '.service'), unit, 0o644)
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', 'ai-platform-gb10-controller.service', 'ai-platform-gb10-platform.service'])
    run(['systemctl', 'restart', 'ai-platform-gb10-controller.service'])
    run(['systemctl', 'restart', 'ai-platform-gb10-platform.service'])
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            if http('http://127.0.0.1:17700/health/live').get('ok') and http('http://127.0.0.1:17780/health/live').get('ok'):
                break
        except Exception:
            pass
        time.sleep(2)
    else:
        raise RuntimeError('平台未就绪，请查看 ai-platform-gb10-platform 容器日志；权重保留。')
    desired_path = ROOT / 'state/desired-mode'
    desired = desired_path.read_text().strip() if desired_path.exists() else profile['default_mode']
    if controller.snapshot().get('verified'):
        controller.inference(desired)
        atomic(ROOT / 'state/active-mode', desired + '\n', 0o644)
    else:
        controller.switch(desired, lambda p: print(p['stage'], flush=True))
    (ROOT / 'install-in-progress').unlink(missing_ok=True)
    print('GB10 平台已安装。凭据仅保存在 /etc/ai-platform-gb10/config.json（root 可读）。', flush=True)
    print('查看凭据：sudo ai-platform-gb10 credentials；加载模型：sudo ai-platform-gb10 switch gpt', flush=True)


def main():
    parser = argparse.ArgumentParser(description='AI Platform GB10 installer and lifecycle CLI')
    parser.add_argument('command', choices=['check', 'install', 'platform-start', 'switch', 'status', 'credentials'])
    parser.add_argument('mode', nargs='?', choices=['gpt', 'nemotron'])
    parser.add_argument('--model-root')
    parser.add_argument('--cache-root')
    parser.add_argument('--tiktoken-root')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('请使用 sudo 或桌面的系统授权入口')
    bundle = Path(__file__).resolve().parents[1]
    if args.command == 'check':
        print(json.dumps(preflight(bundle), ensure_ascii=False, indent=2))
    elif args.command == 'install':
        if not all([args.model_root, args.cache_root, args.tiktoken_root]):
            parser.error('install requires --model-root, --cache-root and --tiktoken-root')
        configure(args, bundle)
    elif args.command == 'credentials':
        config = read(CONFIG)
        print(json.dumps({'username': 'admin', 'password': config['admin_password'], 'api_key': config['api_key']}, indent=2))
    elif args.command == 'platform-start':
        config = read(CONFIG)
        platform_start(config, read(bundle / 'profile.json'))
    else:
        config = read(CONFIG)
        controller = Controller(config, read(bundle / 'profile.json'))
        if args.command == 'switch':
            if not args.mode:
                parser.error('switch requires a mode')
            print(json.dumps(controller.switch(args.mode, lambda p: print(json.dumps(p, ensure_ascii=False), flush=True)), ensure_ascii=False))
        else:
            print(json.dumps(controller.snapshot(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
