"""Session/CSRF and audit boundary for the restricted model installer."""
import re
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field


class Selection(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=23)
    activate: bool = False


class Upload(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    size: int = Field(gt=0, le=100 * 1024 ** 3)
    kind: Literal['models', 'runtime']


def router(owner):
    routes = APIRouter(prefix='/api/model-install')
    read, write = [Depends(owner.require_session)], [Depends(owner.require_csrf)]

    def upload_path(upload_id):
        if not re.fullmatch('[0-9a-f]{32}', upload_id):
            raise HTTPException(422, '离线包 ID 无效')
        return '/v1/model-install/uploads/' + upload_id

    @routes.get('', dependencies=read)
    async def catalog():
        return await owner._agent('GET', '/v1/model-install', timeout=120)

    @routes.post('/plan', dependencies=write)
    async def plan(body: Selection):
        return await owner._agent('POST', '/v1/model-install/plan', json_body=body.model_dump(), timeout=120)

    @routes.post('/actions/{action}', dependencies=write)
    async def action(action: Literal['download', 'install', 'activate', 'disable', 'rollback'], body: Selection, request: Request):
        return await owner._submit_audited_job(request, 'model-' + action, ','.join(body.ids),
            '/v1/model-install/actions/' + action, json_body=body.model_dump())

    @routes.post('/uploads', dependencies=write)
    async def create(body: Upload, request: Request):
        result = await owner._agent('POST', '/v1/model-install/uploads', json_body=body.model_dump())
        await owner._audit_request(request, 'model-upload', result['id'], 'succeeded', {'kind': body.kind, 'bytes': body.size})
        return result

    @routes.get('/uploads/{upload_id}', dependencies=read)
    async def status(upload_id: str):
        return await owner._agent('GET', upload_path(upload_id))

    @routes.put('/uploads/{upload_id}', dependencies=write)
    async def chunk(upload_id: str, request: Request, offset: int = 0):
        path = upload_path(upload_id)
        data = bytearray()
        async for block in request.stream():
            if len(data) + len(block) > 8 * 1024 * 1024:
                raise HTTPException(413, '每块不能超过 8 MiB')
            data.extend(block)
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.put(owner.AGENT_URL + path, params={'offset': offset},
                    headers={**owner._agent_headers(), 'Content-Type': 'application/octet-stream'}, content=bytes(data))
            payload = response.json()
            if response.status_code >= 400:
                raise owner.AgentRequestError(response.status_code, payload.get('detail', '上传失败'))
            return payload
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, '上传连接中断；重新选择同一文件可续传') from exc

    @routes.delete('/uploads/{upload_id}', dependencies=write)
    async def delete(upload_id: str, request: Request):
        result = await owner._agent('DELETE', upload_path(upload_id))
        await owner._audit_request(request, 'model-upload-delete', upload_id, 'succeeded', {})
        return result

    @routes.post('/uploads/{upload_id}/import', dependencies=write)
    async def import_file(upload_id: str, request: Request):
        return await owner._submit_audited_job(request, 'model-import', upload_id, upload_path(upload_id) + '/import')

    return routes
