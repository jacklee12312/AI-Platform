"""Gateway configuration transactions; no NPU settings or arbitrary upstreams."""
import json
from pathlib import Path
import time

from .core import ROOT, atomic, http, read, run, exclusive

FIELDS = {
    'rate_limit_per_minute': ('RATE_LIMIT_PER_MINUTE', 10, 1000, 1),
    'max_body_mb': ('MAX_BODY_BYTES', 1, 100, 1024**2),
    'chat_timeout_seconds': ('CHAT_TIMEOUT_SECONDS', 30, 900, 1),
    'cv_timeout_seconds': ('CV_TIMEOUT_SECONDS', 10, 600, 1),
    'audio_timeout_seconds': ('AUDIO_TIMEOUT_SECONDS', 60, 3600, 1),
    'audio_max_body_mb': ('AUDIO_MAX_BODY_BYTES', 1, 512, 1024**2),
    'default_max_tokens': ('DEFAULT_MAX_TOKENS', 1, 8192, 1),
    'default_temperature': ('DEFAULT_TEMPERATURE', 0, 2, 1),
    'default_top_p': ('DEFAULT_TOP_P', 0.00001, 1, 1),
    'cv_default_confidence': ('CV_DEFAULT_CONFIDENCE', 0, 1, 1),
    'cv_default_iou': ('CV_DEFAULT_IOU', 0, 1, 1),
}
ASR = {'asr_num_threads', 'asr_max_concurrency', 'asr_max_file_mb', 'asr_max_audio_seconds', 'asr_stream_max_seconds'}


def validate(controller, body):
    settings = body.get('settings')
    if not isinstance(settings, dict) or set(settings) - set(FIELDS) - ASR - {'generation_defaults_enabled'}:
        raise ValueError('未知的配置项')
    for key, value in settings.items():
        if key == 'generation_defaults_enabled':
            if type(value) is not bool:
                raise ValueError('默认生成开关需要布尔值')
        elif key in FIELDS:
            _, minimum, maximum, _ = FIELDS[key]
            if type(value) not in (int, float) or not minimum <= value <= maximum:
                raise ValueError('配置值超出范围：' + key)
            if key not in {'default_temperature', 'default_top_p', 'cv_default_confidence', 'cv_default_iou'} and type(value) is not int:
                raise ValueError('该配置项需要整数：' + key)
        elif type(value) is not int or not 1 <= value <= 7200:
            raise ValueError('音频配置值超出范围')
    known = {row['id'] for row in controller.profile['models']}
    models = body.get('models')
    if not isinstance(models, list) or len(models) != len(known) or {row.get('id') for row in models} != known:
        raise ValueError('配置必须包含当前发行的两套模型')
    if any(type(row.get('enabled')) is not bool or row.get('base_url') != 'http://127.0.0.1:17701' for row in models):
        raise ValueError('GB10 后端地址由签名配方固定，不能改为任意主机')
    old = read(ROOT / 'settings.json') if (ROOT / 'settings.json').exists() else {}
    changes = [{'field': key, 'old': old.get(key), 'new': value} for key, value in settings.items() if old.get(key) != value]
    return {'valid': True, 'changes': changes, 'affected_services': ['gateway'], 'estimated_downtime_seconds': 10}


def apply(controller, body, progress):
    with exclusive(controller.root / 'operation.lock'):
        return _apply(controller, body, progress)


def _apply(controller, body, progress):
    validate(controller, body)
    state = controller.state
    previous = (state / 'active-mode').read_text().strip()
    files = [ROOT / 'platform.env', ROOT / 'config/models.json', ROOT / 'settings.json']
    originals = {p: p.read_bytes() if p.exists() else None for p in files}
    atomic(state / 'active-mode', 'loading\n', 0o644)

    def wait_ready():
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                if http('http://127.0.0.1:17700/health/live').get('ok') and http('http://127.0.0.1:17780/health/live').get('ok'):
                    return
            except Exception:
                pass
            time.sleep(2)
        raise RuntimeError('配置应用后的平台健康检查超时')

    try:
        deadline = time.monotonic() + 930
        while True:
            value = http('http://127.0.0.1:17700/internal/requests/active', headers={
                'X-Gateway-Admin-Token': controller.config['management_token']})
            if not isinstance(value.get('requests'), list):
                raise ValueError('无法确认在途请求')
            if not value['requests']:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('在途请求未结束，取消配置应用')
            progress({'phase': 'draining', 'stage': '等待当前网关请求结束'})
            time.sleep(2)
        values = dict(line.split('=', 1) for line in originals[files[0]].decode().splitlines() if '=' in line)
        for key, value in body['settings'].items():
            if key in FIELDS:
                environment, _, _, multiplier = FIELDS[key]
                values[environment] = str(value * multiplier)
            elif key == 'generation_defaults_enabled':
                values['GENERATION_DEFAULTS_ENABLED'] = 'true' if value else 'false'
        # Persist unsupported-server settings for later CPU model recipes, without
        # pretending to restart an ASR service that is not deployed.
        registry = json.loads(originals[files[1]])
        enabled = {row['id']: row['enabled'] for row in body['models']}
        for row in registry['models']:
            row['enabled'] = enabled[row['id']]
        atomic(files[0], ''.join(key + '=' + value + '\n' for key, value in values.items()))
        atomic(files[1], registry, 0o644)
        atomic(files[2], body['settings'])
        progress({'phase': 'restarting', 'stage': '重载管理与网关配置，保留 GPU 模型实例'})
        run(['systemctl', 'restart', 'ai-platform-gb10-platform.service'], timeout=180)
        wait_ready()
        atomic(state / 'active-mode', previous + '\n', 0o644)
        return {'status': 'succeeded', 'gateway_reloaded': True, 'gpu_model_reloaded': False}
    except Exception:
        for path, data in originals.items():
            if data is not None:
                atomic(path, data.decode(), 0o644 if path.name == 'models.json' else 0o600)
            else:
                path.unlink(missing_ok=True)
        try:
            run(['systemctl', 'restart', 'ai-platform-gb10-platform.service'], timeout=180)
            wait_ready()
            atomic(state / 'active-mode', previous + '\n', 0o644)
        except Exception:
            atomic(state / 'active-mode', 'failed\n', 0o644)
        raise
