"""Use the existing evaluation engine and graders through the managed gateway."""
import copy
import hashlib
import json
import os
from pathlib import Path
import time


def stable_snapshot(status, registry, installations):
    runtime = status['runtime']
    return {'mode': status['modelctl']['configured_mode'], 'boot_id': runtime.get('boot_id'),
            'id': runtime.get('container_id'), 'verified': runtime.get('verified') is True,
            'registry_sha256': hashlib.sha256(registry).hexdigest(),
            'installstate_sha256': hashlib.sha256(installations).hexdigest(),
            'execution_path': 'gateway_management_no_customer_billing'}


def install(owner, profile, host):
    import httpx
    from agent import evaluation_api, evaluation_cases
    from agent.evaluation_engine import EvaluationSkip
    from agent.evaluation_runtime import grade
    evaluation_api.VERSION = '2026-10-20.gb10.1'
    evaluation_cases.VERSION = '2026-10-20.gb10.1'

    class Catalog:
        def __init__(self, *args, **kwargs):
            self.models = {row['id']: {**row, 'device': 'NVIDIA GB10', 'capability': 'chat', 'files': []}
                           for row in profile['models']}

        def select(self, ids):
            if not ids or len(ids) != len(set(ids)) or any(mid not in self.models for mid in ids):
                raise ValueError('请选择已适配的 GB10 模型')
            return [self.models[mid] for mid in ids]

    original_cases = evaluation_cases.make_cases
    evaluation_cases.Catalog = Catalog
    evaluation_api.Catalog = Catalog

    def cases(models, suite, repeats=1):
        rows = original_cases(models, suite, repeats)
        for row in rows:
            request = row.get('request', {})
            if request.get('path') == '/v1/chat/completions':
                request['json'].update(max_tokens=512, reasoning_effort='low', chat_template_kwargs={'enable_thinking': False})
                row['parameters'].update(max_tokens=512, reasoning_effort='low', enable_thinking=False)
                row['input_sha256'] = evaluation_cases.input_hash(request)
        return rows

    class Runtime:
        def __init__(self, *args, **kwargs):
            self.snapshot = None
            self.models = []
            self.catalog = Catalog()

        def capture(self):
            return stable_snapshot(host('/status'), Path('/platform/config/models.json').read_bytes(),
                                   Path('/platform/state/model-installations.json').read_bytes())

        def prepare(self, models):
            self.catalog.select(models)
            self.models = list(models)
            self.snapshot = self.capture()
            return copy.deepcopy(self.snapshot)

        def check(self, model, snapshot):
            if snapshot != self.snapshot or self.capture() != snapshot:
                raise RuntimeError('评测期间模型实例、模式或注册配置发生变化，评测已中止')
            row = self.catalog.models[model]
            if not snapshot['verified'] or snapshot['mode'] != row['mode']:
                raise EvaluationSkip('此模型当前未激活或未通过实际推理验证；不会自动切换模式')

        def execute(self, case):
            suite = case['id'].split(':', 1)[0]
            repeat = int(case['id'].rsplit(':', 1)[1])
            canonical = next((r for r in cases([case['model']], suite, repeat) if r['id'] == case['id']), None)
            if canonical is None or any(case.get(k) != canonical[k] for k in ('model', 'checker', 'expected', 'request', 'input_sha256')):
                raise RuntimeError('评测请求不是当前固定样例')
            self.check(case['model'], self.snapshot)
            started = time.monotonic()
            response = httpx.post('http://127.0.0.1:17700/v1/chat/completions', json=canonical['request']['json'],
                                  headers={'X-Gateway-Admin-Token': os.environ['GATEWAY_MANAGEMENT_TOKEN']}, timeout=180, trust_env=False)
            self.check(case['model'], self.snapshot)
            if response.status_code != 200:
                raise EvaluationSkip('管理网关未执行该固定样例（HTTP ' + str(response.status_code) + '），未重试')
            result = grade(canonical, response.json())
            result.setdefault('metrics', {}).update(wall_ms=round((time.monotonic()-started)*1000, 3), response_bytes=len(response.content))
            return result

        def catalog_models(self):
            snapshot = self.capture()
            return [{'id': row['id'], 'device': 'NVIDIA GB10', 'capability': 'chat',
                     'status': 'online' if snapshot['verified'] and snapshot['mode'] == row['mode'] else 'inactive',
                     'available': snapshot['verified'] and snapshot['mode'] == row['mode'],
                     'reason': '' if snapshot['verified'] and snapshot['mode'] == row['mode'] else '当前未激活；不会自动切换模式',
                     'suites': list(evaluation_cases.SUITES)} for row in profile['models']]

    evaluation_api.EvaluationRuntime = Runtime
    evaluation_api.make_cases = cases
