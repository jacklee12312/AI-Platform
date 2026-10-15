"""Fixed deployment adapters and real per-model execution probes."""
from __future__ import annotations
import base64
import io
import json
import os
import re
import subprocess
import tarfile
import time
import wave
from pathlib import Path
from urllib.request import Request, urlopen
from .model_store import CHUNK, safe_relative, under
from .runtime_probes import digit_image, validate

class ModelRuntime:

    def __init__(self, home, catalog, *, docker='/usr/bin/docker', safety=None, command=None):
        self.home, self.catalog, self.docker, self.safety = (Path(home), catalog, docker, safety)
        self.command = command or subprocess.run

    def run(self, *args, timeout=30, check=True):
        result = self.command([self.docker, *args], text=True, capture_output=True, timeout=timeout, check=False)
        if check and result.returncode:
            raise RuntimeError('Docker 操作失败，请在设备端检查容器日志（' + str(args[0]) + '）')
        return result

    def inspect(self, model):
        response = self.run('inspect', model['container'], check=False)
        if response.returncode:
            if 'No such' in response.stderr:
                return None
            raise RuntimeError('无法确认 Docker 容器状态')
        return json.loads(response.stdout)[0]

    def image_ready(self, model):
        result = self.run('image', 'inspect', model['image'], check=False)
        if result.returncode:
            return False
        info = json.loads(result.stdout)[0]
        return info.get('Architecture') in {'arm64', 'aarch64'} and info.get('Os', 'linux') == 'linux'

    def preflight(self, models, activate):
        errors, notices = ([], [])
        if not activate:
            return (errors, notices)
        try:
            info = json.loads(self.run('info', '--format', '{{json .}}').stdout)
            if info.get('Architecture') not in {'aarch64', 'arm64'} or info.get('OSType') != 'linux':
                errors.append('启用模型需要 Linux ARM64 交付设备')
            ram = int(info.get('MemTotal', 0))
            if ram and ram < max((m['min_ram_gb'] for m in models)) * 1024 ** 3:
                errors.append('设备物理内存低于所选模型的最低要求')
        except (OSError, ValueError, RuntimeError):
            errors.append('Docker 服务不可用，无法执行部署预检')
            return (errors, notices)
        mode_path = self.home / 'houmo-docker-deployment/state/active-mode'
        mode = mode_path.read_text().strip() if mode_path.exists() else 'copaw'
        for model in models:
            if model['modes'] and mode not in model['modes']:
                errors.append(model['id'] + ' 需要模式 ' + '/'.join(model['modes']) + '；可先安装不启用，再到模型管理切换模式')
            if not under(self.home, model['directory'] + '/' + model['compose']).is_file():
                errors.append('缺少平台部署文件：' + model['directory'] + '/' + model['compose'])
            if not self.image_ready(model):
                errors.append('缺少 ARM64 运行镜像：' + model['image'] + '；请先导入对应运行环境镜像包')
            if model['id'] == 'cosyvoice3-0.5b':
                runtime = self.home / 'cix_telecom_projects/cosyvoice-vllm-staging'
                required = (runtime / 'venv/bin/vllm', runtime / 'models/tts/config.json')
                if not all((path.is_file() for path in required)):
                    errors.append('缺少 CosyVoice3 固定 HLIEvLLM / vLLM-Omni 运行包；请先导入对应离线运行环境')
            device_root = Path(os.getenv('MODELDECK_HOST_DEV', '/dev'))
            if 'CIX-NPU' in model['device'] and (not (device_root / 'aipu').exists()):
                errors.append('缺少 CIX NPU 设备 /dev/aipu')
            if 'LQ50' in model['device']:
                if model['id'] == 'qwen3.6-35b-a3b':
                    if not (device_root / 'xh2a_ipu1').is_char_device():
                        errors.append('Qwen3.6-35B-A3B 本版本需要双卡设备')
                    runtime = self.home / 'cix_telecom_projects/dual-core'
                    if not (runtime / '.venv/bin/vllm').is_file() or not (runtime / 'qwen36_35B/config.json').is_file():
                        errors.append('缺少配套 Qwen3.6 双卡运行时，请使用完整安装器')
                if not (device_root / 'xh2a_ctl0').exists():
                    errors.append('缺少后摩设备 /dev/xh2a_ctl0')
                sdk = Path(os.getenv('MODELDECK_HOST_USR_LOCAL', '/usr/local')) / 'houmo-drv-xh2_v1.4.0'
                if not sdk.is_dir():
                    errors.append('缺少后摩 XH2 v1.4.0 驱动运行目录')
                try:
                    self.assert_safe()
                    for other in self.catalog.models.values():
                        if 'LQ50' not in other['device'] or not other['modes'] or mode in other['modes']:
                            continue
                        current = self.inspect(other)
                        if current and current['State'].get('Running'):
                            errors.append('同卡互斥模型仍在运行：' + other['id'] + '；请通过模式切换处理')
                except RuntimeError as exc:
                    errors.append(str(exc))
            current = self.inspect(model)
            if current and current['State'].get('Running'):
                notices.append(model['container'] + ' 将短暂停止并重新创建；共享该服务的模型会暂时不可用')
        return (list(dict.fromkeys(errors)), list(dict.fromkeys(notices)))

    def assert_safe(self):
        if not self.safety:
            raise RuntimeError('后摩安全监控未启用，不能加载模型')
        self.safety.scan()
        state = self.safety.snapshot()
        if state.get('incident') or state.get('maintenance') or state.get('monitor_error'):
            raise RuntimeError('后摩设备处于隔离、恢复或监控异常状态，请先处理运行诊断')
        return state['revision']

    def stop(self, model):
        item = self.inspect(model)
        if item and item['State'].get('Running'):
            self.run('stop', '--time', '30', model['container'], timeout=60)
            current = self.inspect(model)
            if current and current['State'].get('Running'):
                raise RuntimeError('容器停止未确认，已停止后续部署')

    def start(self, model, progress):
        if 'LQ50' in model['device']:
            self.assert_safe()
        directory = under(self.home, model['directory'])
        self.run('compose', '--project-directory', str(directory), '-f', str(directory / model['compose']), '--profile', 'copaw', '--profile', 'spark-x1', '--profile', 'qwen38', '--profile', 'qwen36', '--profile', 'qwen-vl', '--profile', 'glm-ocr', '--profile', 'bge-m3', '--profile', 'reranker', '--profile', 'cosyvoice', 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate', model['service'], timeout=90)
        progress({'stage': '等待模型加载：' + model['id']})
        deadline = time.monotonic() + (600 if model['id'] == 'qwen3.6-35b-a3b' else 360)
        while time.monotonic() < deadline:
            if 'LQ50' in model['device']:
                self.assert_safe()
            item = self.inspect(model)
            if not item or not item['State'].get('Running') or item['State'].get('Restarting'):
                raise RuntimeError('模型容器在加载时退出，未自动重试')
            health = item['State'].get('Health', {}).get('Status')
            if health == 'healthy':
                return item
            time.sleep(2)
        raise RuntimeError('模型加载超时，尚未通过自测')

    def request(self, model, path, body, content_type='application/json'):
        item = self.inspect(model)
        if not item or not item['State'].get('Running'):
            raise RuntimeError('模型服务未运行')
        addresses = [v['IPAddress'] for v in item['NetworkSettings']['Networks'].values() if v.get('IPAddress')]
        if not addresses:
            raise RuntimeError('模型服务没有可访问的容器地址')
        key = next((v.partition('=')[2] for v in item['Config'].get('Env', []) if v.startswith('HOUMO_API_KEY=')), '')
        headers = {'Content-Type': content_type}
        if key:
            headers['Authorization'] = 'Bearer ' + key
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        request = Request(f"http://{addresses[0]}:{model['port']}{path}", data=body, headers=headers)
        with urlopen(request, timeout=180) as response:
            data = response.read(16 * CHUNK + 1)
            if len(data) > 16 * CHUNK:
                raise RuntimeError('模型自测响应超过限制')
            return data

    def probe(self, model):
        model_id, capability = (model['id'], model['capability'])
        before = self.inspect(model)
        identity = [before['Id'], before['State']['StartedAt'], before.get('RestartCount', 0)]
        revision = self.assert_safe() if 'LQ50' in model['device'] else None

        def json_request(path, body, content_type='application/json'):
            return json.loads(self.request(model, path, body, content_type))
        passed = False
        if capability.startswith('cv_'):
            image = base64.b64decode(digit_image().split(',', 1)[1])
            result = json_request('/v1/cv/infer/' + model_id, image, 'image/png')
            passed = result.get('ok') is True and result.get('model') == model_id and bool(result.get('runtime_outputs'))
        elif capability == 'embeddings':
            passed = validate(json_request('/v1/embeddings', {'model': model_id, 'input': ['模型部署验证']}), 'embedding')
        elif capability == 'rerank':
            passed = validate(json_request('/v1/rerank', {'model': model_id, 'query': '中国首都', 'documents': ['北京', '苹果'], 'top_n': 2}), 'rerank')
        elif capability == 'audio_tts':
            payload = {'model': model_id, 'input': '你好，模型部署验证完成。', 'response_format': 'wav'}
            if model_id == 'cosyvoice3-0.5b':
                reference = (self.home / 'cix_telecom_projects/sensevoice/model/test_wavs/zh.wav').read_bytes()
                payload.update(model='cosyvoice3', ref_audio='data:audio/wav;base64,' + base64.b64encode(reference).decode(), ref_text='开饭时间早上9点至下午5点。')
            audio = self.request(model, '/v1/audio/speech', payload)
            with wave.open(io.BytesIO(audio), 'rb') as reader:
                frames = reader.readframes(min(reader.getnframes(), 1000000))
                passed = reader.getnframes() > 0 and reader.getsampwidth() == 2 and any(frames)
        elif capability == 'audio_asr':
            sample = next((f for f in model['files'] if f['target'].endswith('test_wavs/zh.wav')))
            audio = under(self.home, sample['target']).read_bytes()
            boundary = 'ModelDeckFixedSpeechProbe'
            body = f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{model_id}\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="zh.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode() + audio + f'\r\n--{boundary}--\r\n'.encode()
            result = json_request('/v1/audio/transcriptions', body, 'multipart/form-data; boundary=' + boundary)
            passed = isinstance(result.get('text'), str) and bool(result['text'].strip())
        else:
            content = '计算 3+4，只回答数字。'
            if model_id in {'qwen3-vl-8b', 'glm-ocr-0.9b'}:
                content = [{'type': 'image_url', 'image_url': {'url': digit_image()}}, {'type': 'text', 'text': 'Text Recognition:' if model_id == 'glm-ocr-0.9b' else '只输出图片里的数字。'}]
            passed = validate(json_request('/v1/chat/completions', {'model': model_id, 'messages': [{'role': 'user', 'content': content}], 'temperature': 0, 'max_tokens': 128, 'stream': False}), 'digit7')
        if not passed:
            raise RuntimeError(model_id + ' 实际推理自测未通过')
        after = self.inspect(model)
        if [after['Id'], after['State']['StartedAt'], after.get('RestartCount', 0)] != identity:
            raise RuntimeError('自测期间模型实例发生变化')
        if revision is not None:
            self.safety.verify_installation(model['container'], identity, revision)
        return {'model': model_id, 'passed': True, 'at': time.time(), 'scope': 'execution_not_quality_acceptance'}

    def import_runtime(self, path):
        allowed = {m['image'] for m in self.catalog.models.values()}
        with tarfile.open(path, 'r:*') as archive:
            seen = set()
            for index, entry in enumerate(archive):
                safe_relative(entry.name.rstrip('/'))
                if index >= 4096 or entry.name in seen or (not (entry.isfile() or entry.isdir())):
                    raise ValueError('镜像归档条目过多或包含非普通文件')
                seen.add(entry.name)
            member = archive.getmember('manifest.json')
            if not member.isfile() or member.size > CHUNK:
                raise ValueError('不是受支持的 Docker save 镜像包')
            rows = json.loads(archive.extractfile(member).read(CHUNK))
            if not isinstance(rows, list) or not rows or len(rows) > 16:
                raise ValueError('镜像包内容无效')
            tags = []
            for row in rows:
                if not row.get('RepoTags') or any((tag not in allowed for tag in row['RepoTags'])):
                    raise ValueError('镜像包包含模型目录以外的镜像标签')
                config_member = archive.getmember(row['Config'])
                if not config_member.isfile() or config_member.size > CHUNK:
                    raise ValueError('镜像配置无效')
                config = json.loads(archive.extractfile(config_member).read(CHUNK))
                if config.get('architecture') != 'arm64' or config.get('os') != 'linux':
                    raise ValueError('需要 Linux ARM64 运行镜像')
                tags.extend(row['RepoTags'])
        self.run('load', '--input', str(path), timeout=1800)
        for tag in tags:
            if not self.image_ready({'image': tag}):
                raise RuntimeError('导入后运行镜像检查失败')
        return {'images': tags, 'message': '运行环境已导入，未启动模型'}
