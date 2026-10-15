"""Authenticated and audited administration routes for device operations."""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from typing import Literal


class Components(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=8)
    settings: dict[str, str] = Field(default_factory=dict)


class Action(BaseModel):
    id: str = Field(max_length=32)
    action: Literal['start', 'stop', 'restart']


class Log(BaseModel):
    id: str = Field(max_length=32)


def router(owner):
    routes = APIRouter(prefix='/api/ops')
    read, write = [Depends(owner.require_session)], [Depends(owner.require_csrf)]

    @routes.get('/overview', dependencies=read)
    async def overview(): return await owner._agent('GET', '/v1/ops/overview', timeout=120)

    @routes.get('/tasks', dependencies=read)
    async def tasks(): return await owner._agent('GET', '/v1/ops/tasks')

    @routes.post('/components/plan', dependencies=write)
    async def plan(body: Components):
        return await owner._agent('POST', '/v1/ops/components/plan', json_body=body.model_dump(), timeout=120)

    @routes.post('/components/install', dependencies=write)
    async def install(body: Components, request: Request):
        return await owner._submit_audited_job(request, 'component-install', ','.join(body.ids),
            '/v1/ops/components/install', json_body=body.model_dump())

    @routes.post('/components/action', dependencies=write)
    async def action(body: Action, request: Request):
        return await owner._submit_audited_job(request, 'component-'+body.action, body.id,
            '/v1/ops/components/action', json_body=body.model_dump())

    @routes.post('/components/logs', dependencies=read)
    async def logs(body: Log):
        return await owner._agent('POST', '/v1/ops/components/logs', json_body=body.model_dump())

    return routes
