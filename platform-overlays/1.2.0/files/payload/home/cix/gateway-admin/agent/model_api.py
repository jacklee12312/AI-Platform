"""Authenticated, bounded model operations. No arbitrary URLs or host paths."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Literal

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .model_install import Installer
from .model_runtime import ModelRuntime
from .model_store import CHUNK, ModelStore, atomic_json, file_lock, now

MAX_CHUNK = 8 * CHUNK
MAX_UPLOAD = 100 * 1024 ** 3


class Selection(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)
    activate: bool = False


class Upload(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    size: int = Field(gt=0, le=MAX_UPLOAD)
    kind: Literal['models', 'runtime']


class LocalImport(BaseModel):
    root: Literal['staging', 'media', 'mnt']
    path: str = Field(default='', max_length=1024)


def local_path(root, relative=''):
    from .model_store import under
    roots = {'staging': Path(os.getenv('MODELDECK_HOME', '/home/cix')) / 'model-imports',
             'media': Path('/media'), 'mnt': Path('/mnt')}
    base = roots[root]
    path = under(base, relative) if relative else base
    if path.is_symlink() or not path.resolve().is_relative_to(base.resolve()):
        raise ValueError('导入路径越界或包含链接')
    return path


def store_root():
    return Path(os.getenv('MODELDECK_STORE_ROOT', '/home/cix/modeldeck-models'))


def saved_job(job_id):
    if not re.fullmatch('[0-9a-f]{32}', job_id):
        return None
    path = store_root() / 'jobs' / (job_id + '.json')
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding='utf-8'))
    if value['status'] in {'queued', 'running'}:
        value.update(status='unknown', error='代理曾中断：请到对应模块核查实际状态；下载可重试续传，未完成的模型安装请先恢复旧文件，组件操作请重新预检。')
    return value


class Uploads:
    def __init__(self, root):
        self.root = Path(root) / 'inbox'
        self.root.mkdir(parents=True, exist_ok=True)

    def paths(self, upload_id):
        if not re.fullmatch('[0-9a-f]{32}', upload_id):
            raise ValueError('离线包 ID 无效')
        return self.root / (upload_id + '.json'), self.root / (upload_id + '.tar')

    def get(self, upload_id):
        meta, path = self.paths(upload_id)
        row = json.loads(meta.read_text(encoding='utf-8'))
        return {**row, 'offset': path.stat().st_size if path.exists() else 0}

    def create(self, request):
        with file_lock(self.root / 'uploads.lock'):
            files = list(self.root.glob('*.json'))
            if len(files) >= 8:
                raise ValueError('最多保留 8 个离线包，请先删除不再需要的上传包')
            reserved = sum(max(0, self.get(f.stem)['size'] - self.get(f.stem)['offset']) for f in files)
            if shutil.disk_usage(self.root).free < request.size + reserved + 256 * CHUNK:
                raise ValueError('上传磁盘空间不足（还需为解包后模型预留空间）')
            upload_id = uuid.uuid4().hex
            meta, _ = self.paths(upload_id)
            row = {'id': upload_id, **request.model_dump(), 'created_at': now()}
            atomic_json(meta, row)
            return {**row, 'offset': 0}

    def append(self, upload_id, offset, data):
        self.paths(upload_id)
        with file_lock(self.root / (upload_id + '.lock')):
            row = self.get(upload_id)
            if offset != row['offset']:
                raise ValueError('上传偏移不一致，请刷新续传位置')
            if not data or len(data) > MAX_CHUNK or offset + len(data) > row['size']:
                raise ValueError('上传分块大小无效')
            _, path = self.paths(upload_id)
            with path.open('ab') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            return self.get(upload_id)

    def remove(self, upload_id):
        meta, path = self.paths(upload_id)
        with file_lock(self.root / 'uploads.lock'), file_lock(self.root / (upload_id + '.lock')):
            path.unlink(missing_ok=True); meta.unlink(missing_ok=True)
        return {'deleted': upload_id}


async def bounded_body(request):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_CHUNK:
            raise HTTPException(413, '每块不能超过 8 MiB')
        data.extend(chunk)
    return bytes(data)


def register(app, owner):
    dependencies = [Depends(owner.require_token)]

    def installer():
        home = Path(os.getenv('MODELDECK_HOME', '/home/cix'))
        store = ModelStore(store_root())
        return Installer(store, home, ModelRuntime(home, store.catalog, docker=owner.DOCKER, safety=owner.RUNTIME_SAFETY))

    async def checked(fn, *args):
        try:
            return await asyncio.to_thread(fn, *args)
        except FileNotFoundError as exc:
            raise HTTPException(404, '所需文件不存在') from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/v1/model-install', dependencies=dependencies)
    async def catalog():
        def read():
            inst = installer()
            return {**inst.snapshot(), 'uploads': [Uploads(inst.store.root).get(p.stem) for p in (inst.store.root / 'inbox').glob('*.json')]}
        return await checked(read)

    @app.get('/v1/model-install/local', dependencies=dependencies)
    async def local_files(root: Literal['staging', 'media', 'mnt'] = 'staging', path: str = ''):
        def read():
            directory = local_path(root, path)
            if not directory.is_dir():
                raise ValueError('导入目录不存在，请先将资源复制到设备导入目录或挂载 U 盘')
            entries = []
            for p in sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
                if p.is_symlink() or len(entries) >= 500:
                    continue
                entries.append({'name': p.name, 'path': (Path(path)/p.name).as_posix(),
                                'directory': p.is_dir(), 'bytes': p.stat().st_size if p.is_file() else 0})
            return {'root': root, 'path': path, 'entries': entries}
        return await checked(read)

    @app.post('/v1/model-install/local/import', dependencies=dependencies)
    async def import_local(body: LocalImport):
        path = await checked(local_path, body.root, body.path)
        def work(progress):
            inst = installer()
            with file_lock(inst.store.root / 'operation.lock'):
                return inst.store.import_local(path, progress)
        return owner._start_job('model-import', body.root+':'+body.path, work, with_progress=True)

    @app.post('/v1/model-install/plan', dependencies=dependencies)
    async def plan(body: Selection):
        return await checked(lambda: installer().plan(body.ids, body.activate))

    @app.post('/v1/model-install/actions/{action}', dependencies=dependencies)
    async def action(action: Literal['download', 'install', 'activate', 'disable', 'rollback'], body: Selection):
        inst = installer()
        try:
            inst.catalog.select(body.ids)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        def work(progress):
            with file_lock(inst.store.root / 'operation.lock'):
                if action == 'download':
                    return inst.store.fetch(body.ids, progress)
                if action == 'install':
                    return inst.install(body.ids, body.activate, progress)
                return inst.manage(body.ids, action, progress)
        return owner._start_job('model-' + action, ','.join(body.ids), work, with_progress=True)

    @app.post('/v1/model-install/uploads', dependencies=dependencies)
    async def create_upload(body: Upload):
        return await checked(lambda: Uploads(store_root()).create(body))

    @app.get('/v1/model-install/uploads/{upload_id}', dependencies=dependencies)
    async def upload_status(upload_id: str):
        return await checked(lambda: Uploads(store_root()).get(upload_id))

    @app.put('/v1/model-install/uploads/{upload_id}', dependencies=dependencies)
    async def upload_chunk(upload_id: str, request: Request, offset: int = 0):
        # Validate before constructing any lock path.
        try:
            Uploads(store_root()).paths(upload_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        data = await bounded_body(request)
        return await checked(lambda: Uploads(store_root()).append(upload_id, offset, data))

    @app.delete('/v1/model-install/uploads/{upload_id}', dependencies=dependencies)
    async def delete_upload(upload_id: str):
        return await checked(lambda: Uploads(store_root()).remove(upload_id))

    @app.post('/v1/model-install/uploads/{upload_id}/import', dependencies=dependencies)
    async def import_upload(upload_id: str):
        uploads = Uploads(store_root())
        row = await checked(uploads.get, upload_id)
        if row['size'] != row['offset']:
            raise HTTPException(409, '离线包尚未上传完整')
        def work(progress):
            inst = installer()
            with file_lock(inst.store.root / 'operation.lock'), file_lock(uploads.root / (upload_id + '.lock')):
                _, path = uploads.paths(upload_id)
                progress({'stage': '校验并导入离线包'})
                if row['kind'] == 'runtime':
                    with file_lock(inst.deploy / 'state/modelctl.lock'):
                        return inst.runtime.import_runtime(path)
                return inst.store.import_local(path, progress)
        return owner._start_job('model-import', row['kind'] + ':' + upload_id, work, with_progress=True)
