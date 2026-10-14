"""Authenticated batch upload, queue controls, and portable result exports."""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import re
import time
import uuid
import zipfile
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .batch_store import BatchError, identifier
from .batch_engine import run_io


class CreateBatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: uuid.UUID
    name: str = Field(min_length=1, max_length=80)
    kind: Literal['ocr', 'asr']
    language: Literal['auto', 'zh', 'en', 'ja', 'ko', 'yue'] = 'auto'


async def store_call(fn, *args, **kwargs):
    try: return await run_io(fn, *args, **kwargs)
    except BatchError as exc: raise HTTPException(exc.status, exc.message) from None


class ResultDownload(FileResponse):
    async def __call__(self, scope, receive, send):
        # Own the file's read lifetime so disconnects cannot skip cleanup.
        scope={**scope,'extensions':{key:value for key,value in scope.get('extensions',{}).items()
                                   if key!='http.response.pathsend'}}
        try:
            await super().__call__(scope,receive,send)
        finally:
            await run_io(Path(self.path).unlink,missing_ok=True)


def result_zip(job, path):
    manifest = {**job, 'format_version': 1, 'complete': job['status'] == 'succeeded',
                'has_warnings': any(i['warning'] for i in job['items'])}
    csv_text = io.StringIO(newline='')
    writer = csv.writer(csv_text)
    writer.writerow(['filename', 'status', 'completed_pages', 'pages', 'warning', 'error', 'request_id'])
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        for index, item in enumerate(job['items'], 1):
            cells = [item[key] for key in ('filename', 'status', 'completed_pages', 'pages', 'warning', 'error', 'request_id')]
            writer.writerow(["'" + v if isinstance(v, str) and v.lstrip().startswith(('=', '+', '-', '@')) else v for v in cells])
            if item['result']:
                stem = re.sub(r'[^\w.\-]', '_', Path(item['filename']).stem).strip('.')[:90] or 'file'
                name = f'{index:03d}_{stem}'
                archive.writestr(name + '.txt', item['result'].get('text', ''))
                archive.writestr(name + '.json', json.dumps(item['result'], ensure_ascii=False, indent=2))
        archive.writestr('summary.csv', '\ufeff' + csv_text.getvalue())


def router(get_engine, session_dependency, csrf_dependency, audit):
    routes = APIRouter(prefix='/api/batches')

    def require_session(request):
        return session_dependency(request.cookies.get('modeldeck_session'))

    def require_csrf(request):
        return csrf_dependency(require_session(request), request.headers.get('X-CSRF-Token', ''))

    @routes.get('')
    async def listing(request: Request):
        require_session(request)
        return await store_call(get_engine().store.listing)

    @routes.post('', status_code=201)
    async def create(body: CreateBatch, request: Request):
        require_csrf(request)
        if not body.name.strip(): raise HTTPException(422, '请输入任务名称')
        job = await store_call(get_engine().store.create, body.id.hex, body.name.strip(), body.kind, body.language)
        await audit(request, 'batch-create', job['id'], 'succeeded', {'kind': body.kind})
        return job

    @routes.get('/{job_id}')
    async def detail(job_id: str, request: Request):
        require_session(request)
        return await store_call(get_engine().store.get, job_id)

    @routes.put('/{job_id}/files/{file_id}')
    async def upload(job_id: str, file_id: str, request: Request,
                     filename: str = Query(min_length=1, max_length=200), size: int = Query(gt=0, le=50*1024*1024)):
        require_csrf(request)
        engine = get_engine()
        reservation = asyncio.create_task(store_call(engine.store.reserve_file, job_id, file_id, filename, size))
        try:
            item, fresh = await asyncio.shield(reservation)
        except asyncio.CancelledError:
            item, fresh = await asyncio.shield(reservation)
            if fresh: await asyncio.shield(asyncio.to_thread(engine.store.abandon_upload, job_id, file_id))
            raise
        if not fresh: return {'id': item['id'], 'status': 'ready', 'reused': True}
        complete = False
        try:
            async with engine.upload_slots:
                digest, received = hashlib.sha256(), 0
                with engine.store.file_path(job_id, file_id).open('xb') as output:
                    async for chunk in request.stream():
                        received += len(chunk)
                        if received > size: raise HTTPException(413, '上传内容超过声明的文件大小')
                        await run_io(output.write, chunk)
                        digest.update(chunk)
                if received != size: raise HTTPException(400, '上传不完整，请重新上传该文件')
                await store_call(engine.store.finish_upload, job_id, file_id, digest.hexdigest())
                complete = True
                return {'id': item['id'], 'status': 'ready'}
        finally:
            if not complete: await asyncio.shield(asyncio.to_thread(engine.store.abandon_upload, job_id, file_id))

    @routes.post('/{job_id}/submit')
    async def submit(job_id: str, request: Request):
        require_csrf(request)
        job = await store_call(get_engine().store.submit, job_id)
        await audit(request, 'batch-submit', job['id'], 'succeeded', {'files': job['total'], 'kind': job['kind']})
        return job

    @routes.post('/{job_id}/cancel')
    async def cancel(job_id: str, request: Request):
        require_csrf(request)
        job = await store_call(get_engine().store.cancel, job_id)
        await audit(request, 'batch-cancel', job['id'], 'succeeded')
        return job

    @routes.delete('/{job_id}')
    async def delete(job_id: str, request: Request):
        require_csrf(request)
        result = await store_call(get_engine().store.delete, job_id)
        await audit(request, 'batch-delete', result['id'], 'succeeded')
        return result

    @routes.get('/{job_id}/files/{file_id}/result')
    async def preview(job_id: str, file_id: str, request: Request):
        require_session(request)
        job = await store_call(get_engine().store.get, job_id, results=True)
        try: file_id = identifier(file_id)
        except BatchError as exc: raise HTTPException(exc.status, exc.message) from None
        item = next((i for i in job['items'] if i['id'] == file_id), None)
        if not item: raise HTTPException(404, '文件不存在')
        return {'filename': item['filename'], 'status': item['status'], 'warning': item['warning'], 'result': item['result']}

    @routes.get('/{job_id}/results.zip')
    async def download(job_id: str, request: Request):
        require_session(request)
        engine = get_engine()
        async with engine.export_slot:
            job = await store_call(engine.store.get, job_id, results=True)
            export_dir = engine.store.root / 'exports'
            export_dir.mkdir(exist_ok=True)
            # A client may disconnect before FileResponse runs its background
            # cleanup. Bound those temporary snapshots and collect stale files.
            export_bytes = 0
            for old in export_dir.glob('*.tmp'):
                try:
                    info = old.stat()
                    if info.st_mtime < time.time() - 3600: old.unlink(missing_ok=True)
                    else: export_bytes += info.st_size
                except FileNotFoundError: pass
            if export_bytes > 128*1024*1024:
                raise HTTPException(503, '结果下载繁忙，请稍后重试')
            path = export_dir / (uuid.uuid4().hex + '.tmp')
            writing = asyncio.create_task(asyncio.to_thread(result_zip, job, path))
            try:
                await asyncio.shield(writing)
                await audit(request, 'batch-download', job['id'], 'succeeded', {'status': job['status']})
                return ResultDownload(path, media_type='application/zip', filename=f"batch-{job['id'][:8]}.zip")
            except BaseException:
                try: await asyncio.shield(writing)
                finally: path.unlink(missing_ok=True)
                raise

    return routes
