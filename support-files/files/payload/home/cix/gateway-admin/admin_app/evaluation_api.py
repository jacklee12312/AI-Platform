"""Session/CSRF/audit boundary for bounded, persistent model evaluations."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Selection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[0-9a-f]{32}$')
    models: list[str] = Field(min_length=1, max_length=23)
    suite: Literal['smoke', 'quality', 'repeat'] = 'smoke'
    repeats: int = Field(default=1, ge=1, le=3, strict=True)

    @model_validator(mode='after')
    def consistent(self):
        if len(set(self.models)) != len(self.models) or any(not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', m) for m in self.models):
            raise ValueError('请选择不重复的有效模型 ID')
        if self.suite != 'repeat' and self.repeats != 1:
            raise ValueError('只有重复时延采样可以重复执行')
        return self


def job_snapshot(run):
    state = run['status']
    status = {'queued': 'queued', 'running': 'running', 'cancelling': 'running',
              'interrupted': 'unknown'}.get(state)
    if status is None:
        status = 'succeeded' if state == 'completed' and run.get('verdict') == 'passed' else 'failed'
    return {'id': run['id'], 'kind': 'evaluation-run', 'target': ','.join(run['models']),
            'status': status, 'created_at': run['created_at'], 'started_at': run.get('started_at'),
            'finished_at': run.get('finished_at'), 'error': run.get('error') or (
                '评测未全部通过，请到自检与评测查看失败、跳过或取消原因' if status in {'failed', 'unknown'} else None),
            'result': {'evaluation_id': run['id'], 'status': state, 'verdict': run['verdict'], 'summary': run['summary']}}


def markdown(run):
    # All model output is fenced JSON, never interpreted as document structure.
    def block(value):
        text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
        fence = '`' * max(3, max((len(s) for s in re.findall(r'`+', text)), default=0) + 1)
        return f'{fence}json\n{text}\n{fence}\n'
    parts = ['# ModelDeck 自检与评测报告\n',
             f"任务：{run['id']}\n\n创建：{run['created_at']}\n\n测试集：{run['suite']} · 版本：{run['version']}\n",
             f"执行状态：{run['status']} · 结论：{run['verdict']}\n",
             '固定样例只证明本次样例结果，不代表通用质量、长上下文或长期稳定性。时延为网关链路端到端耗时，不是模型纯计算耗时。\n',
             '## 统计\n', block(run['summary']), '## 运行快照\n', block(run.get('snapshot', {})), '## 逐项结果\n']
    for index, case in enumerate(run['cases'], 1):
        parts.extend([f'### 样例 {index}\n', block(case)])
    if run.get('error'):
        parts.extend(['## 中断原因\n', block(run['error'])])
    return '\n'.join(parts)


def router(owner):
    routes = APIRouter(prefix='/api/evaluations')
    read, write = [Depends(owner.require_session)], [Depends(owner.require_csrf)]

    def path(run_id):
        if not re.fullmatch('[0-9a-f]{32}', run_id):
            raise HTTPException(422, '评测任务 ID 无效')
        return '/v1/evaluations/' + run_id

    @routes.get('/catalog', dependencies=read)
    async def catalog():
        return await owner._agent('GET', '/v1/evaluations/catalog', timeout=60)

    @routes.get('', dependencies=read)
    async def history(limit: int = Query(30, ge=1, le=100)):
        return await owner._agent('GET', f'/v1/evaluations?limit={limit}')

    @routes.post('', dependencies=write)
    async def create(body: Selection, request: Request):
        try:
            run = await owner._agent('POST', '/v1/evaluations', json_body=body.model_dump(), timeout=30)
        except Exception as exc:
            known_denial = isinstance(exc, owner.AgentRequestError) and exc.upstream_status < 500
            await owner._audit_request(request, 'evaluation-run', body.id, 'failed' if known_denial else 'unknown',
                                       {'http_status': exc.status_code if isinstance(exc, HTTPException) else 502}, job_id=body.id)
            raise
        if run.get('id') != body.id:
            await owner._audit_request(request, 'evaluation-run', body.id, 'unknown', {'error': '任务 ID 不一致'}, job_id=body.id)
            raise HTTPException(502, '代理返回任务不一致；请刷新历史，勿重复提交')
        await owner._audit_request(request, 'evaluation-run', ','.join(body.models), 'accepted', {'suite': body.suite}, job_id=body.id)
        await asyncio.to_thread(owner._audit_store().record_job, job_snapshot(run))
        return run

    @routes.get('/{run_id}', dependencies=read)
    async def detail(run_id: str):
        return await owner._agent('GET', path(run_id))

    @routes.post('/{run_id}/cancel', dependencies=write)
    async def cancel(run_id: str, request: Request):
        run = await owner._agent('POST', path(run_id) + '/cancel', json_body={})
        await owner._audit_request(request, 'evaluation-cancel', run_id, 'succeeded', {'status': run['status']})
        await asyncio.to_thread(owner._audit_store().record_job, job_snapshot(run))
        return run

    @routes.get('/{run_id}/export', dependencies=read)
    async def export(run_id: str, format: Literal['json', 'md'] = 'json'):
        run = await owner._agent('GET', path(run_id))
        body = json.dumps(run, ensure_ascii=False, indent=2, allow_nan=False) if format == 'json' else markdown(run)
        return Response(body, media_type='application/json' if format == 'json' else 'text/markdown',
                        headers={'Content-Disposition': f'attachment; filename="evaluation-{run_id}.{format}"'})

    return routes
