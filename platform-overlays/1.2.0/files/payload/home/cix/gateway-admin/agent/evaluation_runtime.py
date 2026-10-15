"""Read-only deployment checks and bounded management-gateway evaluation.

No model lifecycle, download, admission write, direct backend request or retry.
Docker access is exclusively allowlisted read-only inspect. Reported artifact hashes are
catalog declarations, NOT evidence that the installed weights were verified.
"""
from __future__ import annotations
import base64
import asyncio
import copy
import functools
import hashlib
import io
import json
import math
import queue
import re
import subprocess
import threading
import time
import wave
from pathlib import Path
import httpx
from .evaluation_cases import NOTICE, SUITES, VERSION, make_cases
from .evaluation_engine import EvaluationSkip
from .model_runtime import ModelRuntime
from .model_store import Catalog, HERE, under
from .runtime_probes import validate
GATEWAY = 'http://ai-model-gateway:17700'
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
REQUEST_TIMEOUT = 180.0
STATUS_TIMEOUT = 15.0
SUMMARY_LIMIT = 2000
_FATAL_SAFETY = {'npu_quarantined', 'npu_recovering', 'npu_monitor_unavailable'}

class _RequestLimitError(RuntimeError):
    """Only our static, public transport errors use this type."""

def _bounded(method):

    @functools.wraps(method)
    def call(self, *args, **kwargs):
        previous = self._deadline
        self._deadline = previous if previous is not None else time.monotonic() + REQUEST_TIMEOUT
        try:
            return method(self, *args, **kwargs)
        finally:
            self._deadline = previous
    return call

def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False

def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result

def _json(raw):

    def invalid(_):
        raise ValueError('nonfinite JSON number')
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=invalid)

def _cosine(a, b):

    def normalized(values):
        scale = max((abs(x) for x in values))
        scaled = [x / scale for x in values]
        norm = math.sqrt(math.fsum((x * x for x in scaled)))
        return [x / norm for x in scaled]
    return math.fsum((x * y for x, y in zip(normalized(a), normalized(b))))

def grade(case: dict, body, *, summarize=None) -> dict:
    """Strict deterministic checker, with small summaries and no raw vectors/audio."""
    checker, passed, actual, metrics = (case['checker'], False, '响应结构不符合固定样例要求', {})
    try:
        if checker in {'exact_text', 'strict_json'}:
            choices = body['choices']
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError()
            content = choices[0]['message']['content']
            if not isinstance(content, str):
                raise ValueError()
            actual = content
            if checker == 'exact_text':
                passed = content.strip() == case['expected']
                if case['expected'] == '7':
                    passed = passed and validate(body, 'digit7')
            else:
                value = _json(content)
                expected = case['expected']
                passed = isinstance(value, dict) and value.keys() == expected.keys() and all((type(value[k]) is type(v) and value[k] == v for k, v in expected.items()))
        elif checker in {'embedding', 'embedding_semantic'}:
            rows = body['data']
            count = 3 if checker == 'embedding_semantic' else 1
            if not isinstance(rows, list) or len(rows) != count:
                raise ValueError()
            if any((type(row.get('index')) is not int for row in rows)) or sorted((row['index'] for row in rows)) != list(range(count)):
                raise ValueError()
            rows = sorted(rows, key=lambda row: row['index'])
            vectors = [row['embedding'] for row in rows]
            passed = all((validate({'data': [{'embedding': v}]}, 'embedding') for v in vectors))
            actual = f'向量数={count}；维度=' + ','.join((str(len(v)) if isinstance(v, list) else 'invalid' for v in vectors))
            if passed and count == 3:
                close, unrelated = (_cosine(vectors[0], vectors[1]), _cosine(vectors[0], vectors[2]))
                passed = _finite(close) and _finite(unrelated) and (close > unrelated)
                metrics = {'related_cosine': close, 'unrelated_cosine': unrelated}
                actual += f'；相关余弦={close:.6f}；无关余弦={unrelated:.6f}'
        elif checker in {'rerank', 'rerank_related'}:
            rows = body['results']
            passed = isinstance(rows, list) and len(rows) == 2 and all((isinstance(row, dict) and type(row.get('index')) is int for row in rows)) and validate(body, 'rerank')
            if passed:
                actual = '; '.join((f"index={row['index']}, score={row['relevance_score']:.6g}" for row in rows))
                if checker == 'rerank_related':
                    passed = rows[0]['index'] == 0 and rows[0]['relevance_score'] > rows[1]['relevance_score']
        elif checker == 'cv_outputs':
            outputs = body.get('runtime_outputs')
            passed = body.get('ok') is True and body.get('model') == case['model'] and isinstance(outputs, (dict, list)) and bool(outputs)
            actual = 'runtime_outputs 项数=' + str(len(outputs) if isinstance(outputs, (dict, list)) else 0)
        elif checker == 'pcm16_wav':
            if not isinstance(body, bytes) or body[:4] != b'RIFF' or body[8:12] != b'WAVE':
                raise ValueError()
            with wave.open(io.BytesIO(body), 'rb') as reader:
                frames, channels, rate = (reader.getnframes(), reader.getnchannels(), reader.getframerate())
                if reader.getsampwidth() != 2 or reader.getcomptype() != 'NONE' or (not 1 <= channels <= 8) or (rate <= 0):
                    raise ValueError()
                raw = reader.readframes(MAX_RESPONSE_BYTES // 2 + 1)
                passed = frames > 0 and len(raw) == frames * channels * 2 and any(raw)
                actual = f'PCM16 WAV；bytes={len(body)}；frames={frames}；channels={channels}；rate={rate}'
                metrics = {'audio_seconds': frames / rate}
        elif checker == 'asr_text':
            text = body.get('text')
            passed = isinstance(text, str) and bool(text.strip())
            actual = text if isinstance(text, str) else actual
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, OverflowError, RecursionError, wave.Error, EOFError):
        passed = False
    result = {'passed': bool(passed), 'actual': (summarize(actual) if summarize else actual)[:SUMMARY_LIMIT]}
    if not passed:
        result['reason'] = '固定样例检查未通过；不代表通用质量结论'
    if metrics:
        result['metrics'] = metrics
    return result

class EvaluationRuntime:

    def __init__(self, home, deploy, registry, docker='/usr/bin/docker', safety=None, *, transport=None, command=None):
        self.home, self.deploy, self.registry = (Path(home), Path(deploy), Path(registry))
        self.catalog = Catalog()
        self.safety, self.transport = (safety, transport)
        self._command = command
        self._deadline = None
        names = {'ai-model-gateway', 'gateway-admin-agent', *(m['container'] for m in self.catalog.models.values())}

        def inspect_command(args, **kwargs):
            if len(args) < 3 or len(args) > len(names) + 2 or args[1] != 'inspect' or (len(set(args[2:])) != len(args[2:])) or (not set(args[2:]) <= names):
                raise RuntimeError('评测只允许 Docker inspect')
            kwargs['timeout'] = min(kwargs.get('timeout', 30), self._remaining())
            return (command or subprocess.run)(args, **kwargs)
        self.runtime = ModelRuntime(home, self.catalog, docker=docker, safety=safety, command=inspect_command)
        self._secrets, self._token, self._snapshot, self._models = (set(), '', None, [])
        self._aborted = False

    def _remaining(self):
        remaining = REQUEST_TIMEOUT if self._deadline is None else self._deadline - time.monotonic()
        if remaining <= 0:
            self._aborted = True
            raise RuntimeError('评测操作超过墙钟时限；未重试')
        return remaining

    def _clean(self, value):
        text = str(value)
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, '[REDACTED]')
        text = re.sub('(?i)Bearer\\s+[^\\s"\\\']+', 'Bearer [REDACTED]', text)
        text = re.sub('data:[^\\s,]+;base64,[A-Za-z0-9+/=]+', '[image/audio omitted]', text)
        text = re.sub('[A-Za-z0-9+/=]{160,}', '[opaque data omitted]', text)
        return text[:SUMMARY_LIMIT]

    @staticmethod
    def _read(path, optional=False):
        try:
            with Path(path).open('rb') as stream:
                value = stream.read(MAX_RESPONSE_BYTES + 1)
            if len(value) > MAX_RESPONSE_BYTES:
                raise RuntimeError('评测状态文件超过限制')
            return value
        except FileNotFoundError:
            if optional:
                return None
            raise RuntimeError('评测基础设施状态文件缺失') from None
        except OSError:
            raise RuntimeError('无法读取评测基础设施状态') from None

    def _inspect(self, name):
        try:
            item = self.runtime.inspect({'container': name})
            if item is not None and (not isinstance(item, dict)):
                raise ValueError()
            for env in (item or {}).get('Config', {}).get('Env', []) or []:
                key, _, value = env.partition('=')
                if value and any((word in key.upper() for word in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD'))):
                    self._secrets.add(value)
            return item
        except Exception:
            raise RuntimeError('无法确认 Docker 容器状态；评测已中止') from None

    def _inspect_many(self, names):
        """One read-only Docker process per snapshot, without weakening drift checks."""
        names = list(dict.fromkeys(names))
        try:
            response = self.runtime.run('inspect', *names, check=False)
            if response.returncode:
                return {name: self._inspect(name) for name in names}
            if len(response.stdout.encode()) > MAX_RESPONSE_BYTES:
                raise ValueError()
            rows = _json(response.stdout)
            if not isinstance(rows, list) or len(rows) != len(names):
                raise ValueError()
            result = {}
            for row in rows:
                name = row['Name'].removeprefix('/')
                if name not in names or name in result:
                    raise ValueError()
                result[name] = row
                for env in row.get('Config', {}).get('Env', []) or []:
                    key, _, value = env.partition('=')
                    if value and any((word in key.upper() for word in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD'))):
                        self._secrets.add(value)
            return result
        except Exception:
            raise RuntimeError('无法确认 Docker 容器状态；评测已中止') from None

    def _identity(self, item):
        if item is None:
            return None
        try:
            state = item['State']
            return {'id': self._clean(item['Id']), 'started_at': self._clean(state['StartedAt']), 'restart_count': int(item.get('RestartCount', 0)), 'image_id': self._clean(item['Image']), 'image': self._clean(item.get('Config', {}).get('Image', '')), 'running': state.get('Running') is True, 'restarting': state.get('Restarting') is True}
        except (KeyError, TypeError, ValueError):
            raise RuntimeError('Docker 容器身份信息不完整') from None

    def _safe(self, models):
        try:
            if self.safety is not None:
                state = self.safety.snapshot()
            else:
                raw = self._read(self.deploy / 'state/runtime-safety.json', optional=True)
                state = _json(raw) if raw is not None else None
            if state is None:
                if any(('LQ50' in m['device'] for m in models)):
                    raise ValueError()
                return None
            if not isinstance(state, dict) or any((state.get(k) for k in ('incident', 'maintenance', 'monitor_error'))):
                raise ValueError()
            if type(state.get('revision')) is not int or state['revision'] < 0:
                raise ValueError()
            return {'revision': state.get('revision'), 'boot_id': self._clean(state.get('boot_id', ''))}
        except Exception:
            raise RuntimeError('NPU 处于隔离、维护或监控异常状态；评测已中止') from None

    def _capture(self, models):
        safety = self._safe(models)
        mode_raw = self._read(self.deploy / 'state/active-mode', optional=True)
        registry_raw = self._read(self.registry)
        install_raw = self._read(self.deploy / 'state/model-installations.json', optional=True)
        try:
            mode = mode_raw.decode('utf-8').strip() if mode_raw is not None else 'copaw'
            if mode not in {'copaw', 'spark-x1', 'qwen38', 'qwen36', 'qwen-vl', 'glm-ocr', 'bge-m3', 'reranker'}:
                raise ValueError()
            registry = _json(registry_raw)
            policy = {row['id']: row for row in registry['models']}
            if install_raw is not None:
                installation = _json(install_raw)
                if installation.get('schema_version') != 1 or not isinstance(installation['models'], dict):
                    raise ValueError()
                installed = installation['models']
                if any((not isinstance(row, dict) or type(row.get('installed', False)) is not bool or type(row.get('enabled', False)) is not bool for row in installed.values())):
                    raise ValueError()
            else:
                installed = None
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
            raise RuntimeError('评测模式、注册表或安装状态无效') from None
        inventory = self._inspect_many(['ai-model-gateway', 'gateway-admin-agent', *(m['container'] for m in models)])
        gateway = inventory['ai-model-gateway']
        gateway_identity = self._identity(gateway)
        if not gateway_identity or not gateway_identity['running'] or gateway_identity['restarting']:
            raise RuntimeError('管理网关未运行；评测基础设施不可用')
        token = next((v.partition('=')[2] for v in gateway.get('Config', {}).get('Env', []) if v.startswith('GATEWAY_MANAGEMENT_TOKEN=')), '')
        if not token:
            raise RuntimeError('管理网关未配置管理令牌')
        self._token = token
        evaluator = self._identity(inventory['gateway-admin-agent'])
        grader = hashlib.sha256()
        for filename in ('evaluation_cases.py', 'evaluation_runtime.py'):
            raw = self._read(HERE / filename)
            grader.update(filename.encode() + b'\x00' + str(len(raw)).encode() + b'\x00' + raw)
        containers, records = ({}, {})
        for model in models:
            name, mid = (model['container'], model['id'])
            if name not in containers:
                containers[name] = self._identity(inventory[name])
            identity = containers[name]
            if installed is None:
                try:
                    present = all((under(self.home, f['target']).is_file() and under(self.home, f['target']).stat().st_size == f['size'] for f in model['files']))
                except (OSError, ValueError):
                    present = False
                if not model['capability'].startswith('cv_'):
                    present = present or bool(identity and identity['running'])
                record = {'installed': present, 'enabled': policy.get(mid, {}).get('enabled', True)}
            else:
                record = installed.get(mid, {})
            records[mid] = {'installed': record.get('installed') is True, 'enabled': record.get('enabled') is True and policy.get(mid, {}).get('enabled', True) is True, 'registered': mid in policy, 'container': name, 'active': not model['modes'] or mode in model['modes'], 'declared_version': model['version'], 'declared_image': model['image'], 'declared_artifacts': [{'target': f['target'], 'size': f['size'], 'sha256': f['sha256']} for f in model['files']]}
        return {'mode': mode, 'mode_sha256': hashlib.sha256(mode_raw or b'').hexdigest(), 'registry_sha256': hashlib.sha256(registry_raw).hexdigest(), 'installstate_sha256': hashlib.sha256(install_raw).hexdigest() if install_raw is not None else 'absent', 'catalog_sha256': hashlib.sha256(self._read(HERE / 'model_catalog.json')).hexdigest(), 'baseline_sha256': hashlib.sha256(self._read(HERE / 'runtime_baseline.json')).hexdigest(), 'grader_sha256': grader.hexdigest(), 'evaluator': evaluator, 'gateway': gateway_identity, 'containers': containers, 'models': records, 'safety': safety, 'case_version': VERSION, 'artifact_hash_scope': 'declared_catalog_not_verified_weights', 'execution_path': 'gateway_management_no_customer_billing', 'notice': NOTICE}

    def _request(self, path, *, body=None, content_type='application/json', timeout=None):
        if self._aborted:
            raise RuntimeError('评测已中止，不能继续发送请求')
        if not path.startswith('/v1/') or '\\' in path or '#' in path or ('://' in path):
            raise RuntimeError('评测请求不在固定网关端点范围')
        timeout = min(timeout if timeout is not None else REQUEST_TIMEOUT, self._remaining())
        deadline, result, cancelled = (time.monotonic() + timeout, queue.Queue(maxsize=1), threading.Event())
        token = self._token

        def check_headers(response):
            if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                raise _RequestLimitError('评测响应编码不受支持')
            size = response.headers.get('content-length')
            if size is not None and (not size.isdecimal() or int(size) > MAX_RESPONSE_BYTES):
                raise _RequestLimitError('评测响应超过 16 MiB 限制')

        def append(data, chunk):
            if cancelled.is_set() or time.monotonic() >= deadline:
                raise _RequestLimitError('评测请求超过墙钟时限；未重试')
            if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                raise _RequestLimitError('评测响应超过 16 MiB 限制')
            data.extend(chunk)
        headers = {'X-Gateway-Admin-Token': token, 'Content-Type': content_type, 'Accept-Encoding': 'identity'}

        async def production_request():
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(timeout)) as client:

                async def receive():
                    async with client.stream('POST' if body is not None else 'GET', GATEWAY + path, headers=headers, content=body) as response:
                        check_headers(response)
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            append(data, chunk)
                        append(data, b'')
                        return (response.status_code, bytes(data))
                try:
                    return await asyncio.wait_for(receive(), max(0, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    raise _RequestLimitError('评测请求超过墙钟时限；未重试') from None

        def request_once():
            try:
                if cancelled.is_set() or time.monotonic() >= deadline:
                    return
                if self.transport is None:
                    result.put(asyncio.run(production_request()))
                    return
                with httpx.Client(transport=self.transport, trust_env=False, follow_redirects=False, timeout=httpx.Timeout(timeout)) as client:
                    if cancelled.is_set() or time.monotonic() >= deadline:
                        return
                    with client.stream('POST' if body is not None else 'GET', GATEWAY + path, headers=headers, content=body) as response:
                        check_headers(response)
                        data = bytearray()
                        for chunk in response.iter_bytes():
                            append(data, chunk)
                        append(data, b'')
                        result.put((response.status_code, bytes(data)))
            except _RequestLimitError as exc:
                result.put(RuntimeError(str(exc)))
            except Exception:
                result.put(RuntimeError('管理网关请求失败；未重试'))
        threading.Thread(target=request_once, name='bounded-evaluation-http', daemon=True).start()
        try:
            value = result.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            cancelled.set()
            self._aborted = True
            raise RuntimeError('评测请求超过墙钟时限；未重试') from None
        if isinstance(value, Exception):
            self._aborted = True
            raise value
        return value

    def _statuses(self, snapshot):
        code, raw = self._request('/v1/models', timeout=STATUS_TIMEOUT)
        if code != 200:
            raise RuntimeError('无法读取管理网关模型状态')
        try:
            data = _json(raw)['data']
            if not isinstance(data, list) or len(data) > 256:
                raise ValueError()
            rows = {}
            for row in data:
                if not isinstance(row, dict) or not isinstance(row.get('id'), str) or row['id'] in rows:
                    raise ValueError()
                if row.get('active_mode', snapshot['mode']) != snapshot['mode']:
                    raise RuntimeError('网关模式与评测快照不一致；评测已中止')
                safety = row.get('safety') or {}
                if safety.get('reason') in _FATAL_SAFETY:
                    raise RuntimeError('网关报告 NPU 隔离、维护或监控异常；评测已中止')
                rows[row['id']] = row
            return rows
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise RuntimeError('管理网关返回了无效模型状态') from None

    @_bounded
    def prepare(self, models: list[str]) -> dict:
        self._aborted = False
        self._models = self.catalog.select(models)
        snapshot = self._capture(self._models)
        self._statuses(snapshot)
        if self._capture(self._models) != snapshot:
            raise RuntimeError('评测准备期间运行状态发生变化')
        self._snapshot = copy.deepcopy(snapshot)
        return copy.deepcopy(snapshot)

    @staticmethod
    def _availability(model, snapshot, statuses):
        row = snapshot['models'][model]
        identity = snapshot['containers'][row['container']]
        if not row['installed']:
            return ('uninstalled', '模型未安装')
        if not row['active'] or not row['enabled']:
            return ('inactive', '模型未启用或不属于当前模式')
        if not row['registered'] or not identity or (not identity['running']) or identity['restarting']:
            return ('unavailable', '模型服务不可用')
        status = statuses.get(model, {}).get('status')
        if status != 'online':
            return ('inactive' if status == 'inactive' else 'unavailable', '管理网关尚未允许此模型调用')
        return ('online', '')

    def _verify_snapshot(self, snapshot):
        if self._aborted:
            raise RuntimeError('评测已中止')
        if self._snapshot is None or snapshot != self._snapshot or self._capture(self._models) != self._snapshot:
            self._aborted = True
            raise RuntimeError('评测期间模式、注册表、安装状态或容器身份发生变化；评测已中止')

    @_bounded
    def check(self, model: str, snapshot: dict) -> None:
        self._verify_snapshot(snapshot)
        if model not in snapshot['models']:
            raise RuntimeError('模型不在本次评测范围')
        statuses = self._statuses(snapshot)
        self._verify_snapshot(snapshot)
        _, reason = self._availability(model, snapshot, statuses)
        if reason:
            raise EvaluationSkip(reason)

    def _payload(self, case):
        request = case['request']
        if 'json' in request:
            return (json.dumps(request['json'], ensure_ascii=False, allow_nan=False).encode(), 'application/json')
        if 'image' in request:
            return (base64.b64decode(request['image'].split(',', 1)[1], validate=True), 'image/png')
        pinned = request['audio']
        try:
            path = under(self.home, pinned['target'])
            if path.stat().st_size != pinned['size']:
                raise ValueError()
            raw = self._read(path)
            if len(raw) != pinned['size'] or hashlib.sha256(raw).hexdigest() != pinned['sha256']:
                raise ValueError()
        except (OSError, ValueError, RuntimeError):
            raise EvaluationSkip('固定 zh.wav 样本缺失或大小/SHA-256 校验失败；未下载') from None
        boundary = 'ModelDeckFixedEvaluationSpeech'
        body = f"""--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{case['model']}\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="zh.wav"\r\nContent-Type: audio/wav\r\n\r\n""".encode() + raw + f'\r\n--{boundary}--\r\n'.encode()
        return (body, 'multipart/form-data; boundary=' + boundary)

    @_bounded
    def execute(self, case: dict) -> dict:
        try:
            suite = case['id'].split(':', 1)[0]
            repeat = int(case['id'].rsplit(':', 1)[1])
            canonical = next((row for row in make_cases([case['model']], suite, repeat) if row['id'] == case['id']))
            if any((case.get(key) != canonical[key] for key in ('model', 'checker', 'request', 'expected', 'input_sha256'))):
                raise ValueError()
        except (ValueError, KeyError, TypeError, StopIteration, AttributeError):
            raise RuntimeError('评测样例不在固定目录中或已被修改') from None
        if canonical['status'] == 'skipped':
            raise EvaluationSkip(canonical['reason'])
        self.check(case['model'], self._snapshot)
        body, content_type = self._payload(canonical)
        self._verify_snapshot(self._snapshot)
        started = time.monotonic()
        code, raw = self._request(canonical['request']['path'], body=body, content_type=content_type)
        elapsed = time.monotonic() - started
        self._verify_snapshot(self._snapshot)
        if code != 200:
            try:
                error_code = _json(raw).get('error', {}).get('code')
            except (ValueError, TypeError, AttributeError, RecursionError):
                error_code = None
            if error_code in _FATAL_SAFETY:
                raise RuntimeError('网关报告 NPU 隔离、维护或监控异常；评测已中止')
            if code in {401, 403} or 300 <= code < 400:
                raise RuntimeError('管理网关拒绝评测请求或返回重定向；未重试')
            if code in {404, 409, 429, 502, 503, 504}:
                raise EvaluationSkip('管理网关当前无法执行此模型样例（HTTP ' + str(code) + '）；未重试')
            result = {'passed': False, 'actual': '网关返回 HTTP ' + str(code), 'reason': '固定请求未成功；未重试'}
        else:
            try:
                value = raw if canonical['checker'] == 'pcm16_wav' else _json(raw)
                result = grade(canonical, value, summarize=self._clean)
            except (ValueError, UnicodeError, RecursionError):
                result = {'passed': False, 'actual': '响应不是有效的有限值 JSON', 'reason': '固定样例响应解析失败'}
        result['actual'] = self._clean(result['actual'])
        result.setdefault('metrics', {}).update(wall_ms=round(elapsed * 1000, 3), response_bytes=len(raw))
        return result

    def catalog_models(self) -> list[dict]:
        request_runtime = EvaluationRuntime(self.home, self.deploy, self.registry, self.runtime.docker, self.safety, transport=self.transport, command=self._command)
        return request_runtime._catalog_rows()

    @_bounded
    def _catalog_rows(self) -> list[dict]:
        models = list(self.catalog.models)
        try:
            snapshot = self.prepare(models)
            statuses = self._statuses(snapshot)
            self._verify_snapshot(snapshot)
            error = ''
        except RuntimeError:
            error = '评测基础设施或安全监控暂不可用'
        rows = []
        for model in self.catalog.models.values():
            status, reason = ('unavailable', error) if error else self._availability(model['id'], snapshot, statuses)
            rows.append({k: model[k] for k in ('id', 'device', 'capability')} | {'status': status, 'available': status == 'online', 'reason': reason, 'suites': list(SUITES)})
        return rows

def catalog_models(home, deploy, registry, docker='/usr/bin/docker', safety=None, *, transport=None, command=None) -> list[dict]:
    return EvaluationRuntime(home, deploy, registry, docker, safety, transport=transport, command=command)._catalog_rows()
