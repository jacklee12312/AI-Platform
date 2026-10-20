"""Host-side model lifecycle. No Docker socket or shell is exposed to the browser."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import socket
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen

LABEL = 'ai-platform.gb10.managed'
ROOT = Path('/var/lib/ai-platform-gb10')
CONFIG = Path('/etc/ai-platform-gb10/config.json')


def read(path):
    return json.loads(Path(path).read_text('utf-8'))


def atomic(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('拒绝写入符号链接：' + str(path))
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        os.chmod(temporary, mode)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def run(args, timeout=60, check=True):
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError('命令执行失败：' + str(args[0]) + ': ' + (result.stderr or result.stdout)[-1500:])
    return result


def http(url, body=None, headers=None, timeout=10):
    headers = dict(headers or {})
    if body is not None:
        body = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    with urlopen(Request(url, data=body, headers=headers), timeout=timeout) as response:
        data = response.read()
        return json.loads(data) if data else {}


def safe_file(root, relative):
    name = PurePosixPath(relative)
    if not relative or name.is_absolute() or '..' in name.parts or '\\' in relative:
        raise ValueError('模型文件路径不合法')
    target = Path(root) / relative
    if not target.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError('模型路径越界')
    for parent in [target, *target.parents]:
        if parent == Path(root).parent:
            break
        if parent.is_symlink():
            raise ValueError('模型路径包含符号链接')
    return target


def verify_model(root, manifest, progress=lambda _: None):
    """Verify official LFS SHA256 and ordinary Git blob SHA1, including sizes."""
    result = []
    for index, row in enumerate(manifest['files']):
        target = safe_file(root, row['name'])
        before = target.stat()
        if before.st_size != row['size'] or not target.is_file():
            raise ValueError('模型文件大小不符：' + row['name'])
        algorithm = 'sha256' if row.get('sha256') else 'sha1'
        expected = row.get('sha256') or row['git_blob_sha1']
        digest = hashlib.new(algorithm)
        if algorithm == 'sha1':
            digest.update(f"blob {row['size']}\0".encode())
        with target.open('rb') as stream:
            for chunk in iter(lambda: stream.read(8 * 1024**2), b''):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        if digest.hexdigest() != expected or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError('模型文件摘要或稳定性校验失败：' + row['name'])
        result.append({'name': row['name'], 'size': before.st_size, 'mtime_ns': before.st_mtime_ns,
                       'inode': before.st_ino, 'digest': expected})
        progress({'stage': '校验 ' + row['name'], 'completed': index + 1, 'total': len(manifest['files'])})
    return {'revision': manifest['revision'], 'files': result, 'verified_at': time.time()}


def unchanged(root, evidence):
    for row in evidence['files']:
        stat = safe_file(root, row['name']).stat()
        if (stat.st_size, stat.st_mtime_ns, stat.st_ino) != (row['size'], row['mtime_ns'], row['inode']):
            raise ValueError('模型文件发生变化，请重新执行校验：' + row['name'])


@contextmanager
def exclusive(path):
    import fcntl
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('另一个模型管理操作正在执行') from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class Controller:
    def __init__(self, config, profile, root=ROOT, command=run, request=http):
        self.config, self.profile, self.root = config, profile, Path(root)
        self.run, self.http = command, request
        self.state = self.root / 'state'
        self.models = {row['mode']: row for row in profile['models']}

    def inspect(self, name):
        result = self.run(['docker', 'inspect', name], check=False)
        if result.returncode:
            self.run(['docker', 'info'], timeout=20)
            return None
        return json.loads(result.stdout)[0]

    def stop(self, name):
        value = self.inspect(name)
        if value is None:
            return
        if value.get('Config', {}).get('Labels', {}).get(LABEL) != 'true':
            raise ValueError('容器不属于本安装器，拒绝操作：' + name)
        log = self.run(['docker', 'logs', '--tail', '2000', name], check=False)
        atomic(self.root / 'logs' / (name + '-' + str(time.time_ns()) + '.log'),
               (log.stdout + log.stderr)[-1024**2])
        self.run(['docker', 'stop', '--time', '30', name], timeout=90)
        self.run(['docker', 'rm', name], timeout=60)
        if name == 'ai-platform-gb10-model' and hasattr(os, 'posix_fadvise'):
            for model in self.models:
                for path in self.model_path(model).glob('*.safetensors'):
                    if path.is_symlink():
                        continue
                    with path.open('rb') as stream:
                        os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)

    def model_path(self, mode):
        return Path(self.config['model_root']) / self.models[mode]['directory']

    def image(self):
        runtime = self.profile['runtime']
        value = json.loads(self.run(['docker', 'image', 'inspect', runtime['image']]).stdout)[0]
        if value['Id'] != runtime['image_id'] or value['Architecture'] != 'arm64' or value['Os'] != 'linux':
            raise ValueError('推理镜像与已验证的 GB10 镜像不一致')
        return value['Id']

    def verify_files(self, mode, progress=lambda _: None):
        recipe = self.models[mode]
        manifest = read(Path(self.config['bundle']) / 'models' / recipe['manifest'])
        evidence = verify_model(self.model_path(mode), manifest, progress)
        atomic(self.state / ('verified-' + mode + '.json'), evidence)
        return evidence

    def verified(self, mode):
        evidence = read(self.state / ('verified-' + mode + '.json'))
        manifest = read(Path(self.config['bundle']) / 'models' / self.models[mode]['manifest'])
        declared = {(r['name'], r['size'], r.get('sha256') or r.get('git_blob_sha1')) for r in manifest['files']}
        saved = {(r['name'], r['size'], r['digest']) for r in evidence['files']}
        if manifest['revision'] != evidence['revision'] or declared != saved:
            raise ValueError('权重校验记录与本次签名发行不一致，请重新校验')
        unchanged(self.model_path(mode), evidence)
        return evidence

    def inference(self, mode):
        model = self.models[mode]
        value = self.http('http://127.0.0.1:17701/v1/chat/completions', {
            'model': model['id'], 'messages': [{'role': 'user', 'content': 'What is 6 times 7? Answer only the number.'}],
            'max_tokens': 512, 'temperature': 0, 'stream': False,
            'chat_template_kwargs': {'enable_thinking': False}, 'reasoning_effort': 'low'}, timeout=120)
        message = value.get('choices', [{}])[0].get('message', {})
        content = message.get('content') or ''
        if '42' not in content:
            raise RuntimeError('实际对话检查未产生正确答案，网关保持关闭')
        return {'passed': True, 'model': model['id'], 'content': content[:1000], 'usage': value.get('usage')}

    def launch(self, mode, progress):
        model, runtime = self.models[mode], self.profile['runtime']
        self.verified(mode)
        image = self.image()
        name = 'ai-platform-gb10-model'
        previous = self.inspect(name)
        host_network = bool(previous and previous.get('HostConfig', {}).get('NetworkMode') == 'host')
        self.stop(name)
        if host_network:
            # One-time migration from the initial candidate. TRT's socket probe
            # does not set SO_REUSEADDR; let its host TIME_WAIT sockets expire.
            deadline = time.monotonic() + 90
            while True:
                with socket.socket() as probe:
                    try:
                        probe.bind(('127.0.0.1', 17701))
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError('原后端端口未释放；未停止其他进程') from None
                progress({'phase': 'port_release', 'stage': '等待旧版后端连接释放'})
                time.sleep(2)
        yaml = ('kv_cache_config:\n  dtype: fp8\n  enable_block_reuse: false\n'
                '  free_gpu_memory_fraction: 0.60\n  max_tokens: 65536\n')
        if mode == 'nemotron':
            yaml += '  mamba_ssm_cache_dtype: float32\n'
        yaml += ('moe_config:\n  backend: CUTLASS\ncuda_graph_config:\n  enable_padding: true\n'
                 '  max_batch_size: 4\nenable_attention_dp: false\nenable_chunked_prefill: true\nstream_interval: 1\n')
        if model.get('mtp_tokens'):
            yaml += ('speculative_config:\n  decoding_type: MTP\n  max_draft_len: 3\n'
                     '  use_relaxed_acceptance_for_thinking: false\n')
        yaml_path = self.root / 'runtime.yaml'
        atomic(yaml_path, yaml, 0o644)
        command = ['docker', 'run', '-d', '--name', name, '--label', LABEL + '=true',
                   '--gpus', 'all', '--ipc', 'host', '--network', 'bridge', '--restart', 'no',
                   '-p', '127.0.0.1:17701:8000',
                   '--ulimit', 'memlock=-1', '--ulimit', 'stack=67108864',
                   '-v', str(self.model_path(mode)) + ':/model:ro', '-v', str(yaml_path) + ':/runtime.yaml:ro',
                   '-v', self.config['cache_root'] + ':/root/.cache',
                   '-v', self.config['tiktoken_root'] + ':/tiktoken:ro',
                   '-e', 'TIKTOKEN_ENCODINGS_BASE=/tiktoken', '-e', 'HF_HUB_OFFLINE=1',
                   '-e', 'TRANSFORMERS_OFFLINE=1', '-e', 'OMP_NUM_THREADS=4',
                   '-e', 'PYTORCH_ALLOC_CONF=expandable_segments:True', image,
                   'trtllm-serve', 'serve', '/model', '--served_model_name', model['id'],
                   '--host', '0.0.0.0', '--port', '8000', '--max_batch_size', '4',
                   '--max_num_tokens', '4096', '--max_seq_len', '16384', '--tp_size', '1', '--ep_size', '1',
                   '--no-telemetry', '--extra_llm_api_options', '/runtime.yaml', '--trust_remote_code']
        if model.get('reasoning_parser'):
            command += ['--reasoning_parser', model['reasoning_parser']]
        identity = self.run(command, timeout=90).stdout.strip()
        started = time.monotonic()
        while time.monotonic() - started < runtime['ready_timeout_seconds']:
            info = self.inspect(name)
            if not info or not info['State']['Running']:
                raise RuntimeError('模型容器退出，请查看模型日志')
            progress({'phase': 'loading', 'stage': '正在加载 ' + model['display_name'],
                      'elapsed_seconds': round(time.monotonic() - started)})
            try:
                self.http('http://127.0.0.1:17701/health', timeout=3)
                break
            except Exception:
                time.sleep(5)
        else:
            raise TimeoutError('模型加载超过 20 分钟')
        progress({'phase': 'verification', 'stage': '正在进行实际对话检查'})
        evidence = self.inference(mode)
        installations = {'schema_version': 1, 'models': {m['id']: {'installed': True, 'enabled': True} for m in self.models.values()}}
        atomic(self.state / 'model-installations.json', installations, 0o644)
        atomic(self.state / 'runtime.json', {'mode': mode, 'container_id': identity, 'verified': True,
               'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'evidence': evidence})
        atomic(self.state / 'active-mode', mode + '\n', 0o644)
        atomic(self.state / 'desired-mode', mode + '\n')
        return evidence

    def switch(self, mode, progress=lambda _: None):
        if mode not in self.models:
            raise ValueError('未知的 GB10 模型模式')
        with exclusive(self.root / 'operation.lock'):
            # Do all cheap validation before closing admission or unloading a model.
            self.verified(mode)
            self.image()
            try:
                previous = (self.state / 'desired-mode').read_text().strip()
            except FileNotFoundError:
                previous = None
            atomic(self.state / 'active-mode', 'loading\n', 0o644)
            # Existing requests finish before their backend is stopped.
            deadline = time.monotonic() + 330
            try:
                while True:
                    active = self.http('http://127.0.0.1:17700/internal/requests/active',
                                       headers={'X-Gateway-Admin-Token': self.config['management_token']})
                    rows = active.get('requests')
                    if not isinstance(rows, list):
                        raise RuntimeError('无法确认网关在途请求，取消切换')
                    if not rows:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError('已有请求尚未结束，取消切换')
                    progress({'phase': 'draining', 'stage': '等待正在执行的请求结束'})
                    time.sleep(2)
            except Exception:
                atomic(self.state / 'active-mode', (previous or 'loading') + '\n', 0o644)
                raise
            try:
                atomic(self.state / 'runtime.json', {'verified': False, 'mode': mode, 'phase': 'loading'})
                return self.launch(mode, progress)
            except Exception as exc:
                self.stop('ai-platform-gb10-model')
                if previous in self.models and previous != mode:
                    try:
                        progress({'phase': 'rollback', 'stage': '目标加载失败，恢复原模型'})
                        self.launch(previous, progress)
                    except Exception:
                        atomic(self.state / 'active-mode', 'failed\n', 0o644)
                        atomic(self.state / 'runtime.json', {'verified': False, 'phase': 'failed'})
                raise RuntimeError('模型切换失败：' + str(exc)) from exc

    def snapshot(self):
        info = self.inspect('ai-platform-gb10-model')
        try:
            saved = read(self.state / 'runtime.json')
        except (OSError, ValueError):
            saved = {}
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        valid = bool(info and info['State']['Running'] and saved.get('verified')
                     and info['Id'] == saved.get('container_id') and saved.get('boot_id') == boot)
        value = {**saved, 'verified': valid, 'heartbeat_epoch': time.time(), 'boot_id': boot}
        atomic(self.state / 'safety.json', value, 0o644)
        return value
