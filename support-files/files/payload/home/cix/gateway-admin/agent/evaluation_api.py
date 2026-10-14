"""Token-protected evaluation API; never accepts URLs, scripts or lifecycle commands."""
from __future__ import annotations

import asyncio
import os
import re
import threading
from pathlib import Path
from typing import Literal

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .evaluation_cases import VERSION, SUITES, make_cases
from .evaluation_engine import EvaluationManager
from .evaluation_store import EvaluationConflict
from .evaluation_runtime import EvaluationRuntime
from .model_api import store_root
from .model_store import Catalog


class Selection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[0-9a-f]{32}$')
    models: list[str] = Field(min_length=1, max_length=23)
    suite: Literal['smoke', 'quality', 'repeat'] = 'smoke'
    repeats: int = Field(default=1, ge=1, le=3, strict=True)

    @model_validator(mode='after')
    def validate_models(self):
        Catalog().select(self.models)
        if self.suite != 'repeat' and self.repeats != 1:
            raise ValueError('只有重复时延采样可以重复执行')
        return self


MANAGER = None
MANAGER_LOCK = threading.Lock()


def manager(owner):
    global MANAGER
    with MANAGER_LOCK:
        if MANAGER is None:
            home = Path(os.getenv('MODELDECK_HOME', '/home/cix'))
            runtime = EvaluationRuntime(home, owner.HOUMO_DEPLOY_DIR, owner.MODEL_REGISTRY,
                                        docker=owner.DOCKER, safety=owner.RUNTIME_SAFETY)
            MANAGER = EvaluationManager(store_root() / 'evaluations', owner.HOUMO_DEPLOY_DIR,
                                        owner.OPERATION_LOCK, runtime, VERSION, make_cases)
        return MANAGER


def close():
    global MANAGER
    with MANAGER_LOCK:
        if MANAGER:
            MANAGER.close()
            MANAGER = None


def active_id(value):
    return value.active_run_id


def job_snapshot(run):
    state = run['status']
    status = {'queued': 'queued', 'running': 'running', 'cancelling': 'running', 'interrupted': 'unknown'}.get(state)
    if status is None:
        status = 'succeeded' if state == 'completed' and run.get('verdict') == 'passed' else 'failed'
    return {'id': run['id'], 'kind': 'evaluation-run', 'target': ','.join(run['models']),
            'status': status, 'created_at': run['created_at'], 'started_at': run.get('started_at'),
            'finished_at': run.get('finished_at'), 'error': run.get('error') or (
                '评测未全部通过，请到自检与评测查看失败、跳过或取消原因' if status in {'failed', 'unknown'} else None),
            'result': {'evaluation_id': run['id'], 'status': state, 'verdict': run['verdict'], 'summary': run['summary']}}


def saved_job(owner, job_id):
    if not re.fullmatch('[0-9a-f]{32}', job_id):
        return None
    # Do not initialize a new store just to look up unrelated legacy jobs.
    if MANAGER is None and not (store_root() / 'evaluations' / 'evaluation-runs.db').exists():
        return None
    try:
        return job_snapshot(manager(owner).get(job_id))
    except KeyError:
        return None


def register(app, owner):
    auth = [Depends(owner.require_token)]

    async def checked(call):
        try:
            return await asyncio.to_thread(call)
        except KeyError as exc:
            raise HTTPException(404, '评测任务不存在') from exc
        except EvaluationConflict as exc:
            raise HTTPException(409, '任务参数与已保存 ID 冲突、历史容量已满或另一个管理操作正在执行；请刷新核查') from exc
        except ValueError as exc:
            raise HTTPException(422, '评测参数或记录格式无效') from exc
        except (OSError, RuntimeError) as exc:
            raise HTTPException(503, '无法确认评测服务或运行状态，请查看运行诊断') from exc

    def valid_id(run_id):
        if not re.fullmatch('[0-9a-f]{32}', run_id):
            raise HTTPException(422, '评测任务 ID 无效')

    @app.get('/v1/evaluations/catalog', dependencies=auth)
    async def catalog():
        def read():
            value = manager(owner)
            descriptions = {
                'smoke': '逐模型发起小型真实推理，检查协议与输出结构；不代替业务质量验收。',
                'quality': '严格检查内置算术、JSON、图片数字、相关度与审核样例；缺少标注集的能力不判质量通过。',
                'repeat': '顺序重复快速自检 1–3 次，记录网关端到端时延；不是并发或长期压力测试。',
            }
            return {'version': VERSION, 'models': value.runtime.catalog_models(),
                    'suites': [{'id': key, 'name': name, 'description': descriptions[key],
                                'scope': 'quality' if key == 'quality' else 'execution'} for key, name in SUITES.items()],
                    'limits': {'max_models': 23, 'max_repeats': 3, 'max_cases': 120},
                    'active_run_id': active_id(value)}
        return await checked(read)

    @app.get('/v1/evaluations', dependencies=auth)
    async def history(limit: int = Query(30, ge=1, le=100)):
        return await checked(lambda: {'runs': manager(owner).list(limit), 'active_run_id': active_id(manager(owner))})

    @app.post('/v1/evaluations', dependencies=auth)
    async def create(body: Selection):
        return await checked(lambda: manager(owner).create(body.model_dump()))

    @app.get('/v1/evaluations/{run_id}', dependencies=auth)
    async def detail(run_id: str):
        valid_id(run_id)
        return await checked(lambda: manager(owner).get(run_id))

    @app.post('/v1/evaluations/{run_id}/cancel', dependencies=auth)
    async def cancel(run_id: str):
        valid_id(run_id)
        return await checked(lambda: manager(owner).cancel(run_id))
