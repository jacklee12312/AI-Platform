"""Local UI fixture: real evaluation engine, deterministic fake inference only."""
import threading
import time

from agent.evaluation_cases import VERSION, make_cases
from agent.evaluation_engine import EvaluationManager, EvaluationSkip
from agent.model_store import Catalog
from admin_app.evaluation_api import job_snapshot


class FixtureRuntime:
    def catalog_models(self):
        return [{'id': m['id'], 'device': m['device'], 'capability': m['capability'],
                 'status': 'inactive' if m['id'] == 'qwen3.8-27b' else 'online',
                 'available': m['id'] != 'qwen3.8-27b',
                 'reason': '模式不匹配' if m['id'] == 'qwen3.8-27b' else None,
                 'suites': ['smoke', 'repeat'] if m['capability'].startswith('cv_') or m['capability'] in {'audio_asr', 'audio_tts'} else ['smoke', 'quality', 'repeat']}
                for m in Catalog().models.values()]

    def prepare(self, models):
        return {'mode': 'multi-model', 'versions': {'evaluation': {'version': VERSION}},
                'models': {m: {'model_version': 'FIXTURE-NOT-REAL', 'image_id': 'sha256:fixture',
                               'container_id': 'fixture-container'} for m in models}}

    def check(self, model, snapshot):
        if model == 'qwen3.8-27b':
            raise EvaluationSkip('模式不匹配，未启动服务')

    def execute(self, case):
        time.sleep(.35)
        if case.get('status') == 'skipped':
            raise EvaluationSkip(case.get('reason', '该能力没有标注数据集'))
        passed = case['model'] != 'bge-reranker-v2-m3'
        return {'passed': passed, 'actual': 'FIXTURE：' + ('样例返回正常' if passed else '排序不符合预期'),
                'reason': None if passed else '相关文档没有排在首位（模拟失败）'}


class Fixture:
    def __init__(self, owner, root):
        self.owner = owner
        self.manager = EvaluationManager(root/'evaluations', root/'fixture-deploy', threading.Lock(),
                                         FixtureRuntime(), VERSION, make_cases)

    async def handle(self, method, path, body=None):
        from urllib.parse import urlsplit, parse_qs
        from agent.evaluation_cases import SUITES
        from agent.evaluation_store import EvaluationConflict
        route = urlsplit(path); p = route.path
        try:
            if p == '/v1/evaluations/catalog':
                return {'version': VERSION, 'models': self.manager.runtime.catalog_models(),
                        'suites': [{'id': k, 'name': v, 'description': '本地模拟自检，不连接设备',
                                    'scope': 'quality' if k == 'quality' else 'execution'} for k, v in SUITES.items()],
                        'limits': {'max_models': 23, 'max_repeats': 3, 'max_cases': 120},
                        'active_run_id': self.manager.active_run_id}
            if p == '/v1/evaluations':
                if method == 'POST': return self.manager.create(body)
                return {'runs': self.manager.list(int(parse_qs(route.query).get('limit', ['30'])[0])),
                        'active_run_id': self.manager.active_run_id}
            if p.startswith('/v1/jobs/'):
                return job_snapshot(self.manager.get(p.rsplit('/', 1)[-1]))
            if p.endswith('/cancel'):
                return self.manager.cancel(p.split('/')[-2])
            return self.manager.get(p.rsplit('/', 1)[-1])
        except KeyError as exc:
            raise self.owner.AgentRequestError(404, '评测不存在') from exc
        except EvaluationConflict as exc:
            raise self.owner.AgentRequestError(409, '另一个评测正在执行') from exc
        except ValueError as exc:
            raise self.owner.AgentRequestError(422, '评测参数无效') from exc
