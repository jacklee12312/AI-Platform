"""Session-independent agent boundary for local host operations and persisted jobs."""
import asyncio
import http.client
import json
from pathlib import Path
import socket
from typing import Literal
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from .model_api import store_root, saved_job


class Components(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=8)
    settings: dict[str, str] = Field(default_factory=dict)


class Action(BaseModel):
    id: str = Field(max_length=32)
    action: Literal['start', 'stop', 'restart']


class Log(BaseModel):
    id: str = Field(max_length=32)


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/run/modeldeck-ops/ops.sock')


def host(owner, path, body=None, timeout=120):
    connection = UnixConnection('localhost', timeout=timeout)
    try:
        data = json.dumps(body).encode() if body is not None else None
        connection.request('POST' if body is not None else 'GET', path, data,
                           {'Authorization': 'Bearer '+owner.AGENT_TOKEN, 'Content-Type': 'application/json'})
        response = connection.getresponse(); result = json.loads(response.read(8*1024*1024))
        if response.status >= 400:
            raise RuntimeError(result.get('detail', '宿主机接口不可用'))
        return result
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise RuntimeError('本机运维服务连接失败，请检查 modeldeck-ops.service') from exc
    finally: connection.close()


def register(app, owner):
    auth = [Depends(owner.require_token)]

    @app.get('/v1/ops/overview', dependencies=auth)
    async def overview():
        try: return await asyncio.to_thread(host, owner, '/overview')
        except RuntimeError as exc: raise HTTPException(503, str(exc)) from exc

    @app.get('/v1/ops/tasks', dependencies=auth)
    async def tasks():
        rows = []
        for path in sorted((store_root()/'jobs').glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:200]:
            row = owner.JOBS.get(path.stem) or saved_job(path.stem)
            if row: rows.append(dict(row))
        return {'data': rows}

    @app.post('/v1/ops/components/plan', dependencies=auth)
    async def plan(body: Components):
        try: return await asyncio.to_thread(host, owner, '/components/plan', body.model_dump())
        except RuntimeError as exc: raise HTTPException(409, str(exc)) from exc

    @app.post('/v1/ops/components/install', dependencies=auth)
    async def install(body: Components):
        return owner._start_job('component-install', ','.join(body.ids),
            lambda: host(owner, '/components/install', body.model_dump(), timeout=3600))

    @app.post('/v1/ops/components/action', dependencies=auth)
    async def action(body: Action):
        return owner._start_job('component-'+body.action, body.id,
            lambda: host(owner, '/components/action', body.model_dump(), timeout=600))

    @app.post('/v1/ops/components/logs', dependencies=auth)
    async def logs(body: Log):
        try: return await asyncio.to_thread(host, owner, '/components/logs', body.model_dump())
        except RuntimeError as exc: raise HTTPException(409, str(exc)) from exc
