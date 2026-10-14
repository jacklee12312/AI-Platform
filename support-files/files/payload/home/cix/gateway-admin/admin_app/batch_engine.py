"""Bounded, durable OCR/ASR workers. Never repeat an uncertain inference."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import sys
from pathlib import Path

import httpx

from .batch_store import BatchError, BatchStore, MODELS

LOGGER = logging.getLogger(__name__)


class CancelledBatch(Exception):
    pass


class UncertainInference(Exception):
    pass


class WaitForResource(Exception):
    def __init__(self, message, delay=5):
        self.message, self.delay = message, delay


async def run_io(fn, *args, **kwargs):
    # Task cancellation does not stop a thread. Drain an in-progress SQLite
    # transaction before shutdown so no old claim can race startup recovery.
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try: return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.shield(task)
        raise


class BatchEngine:
    def __init__(self, store: BatchStore, gateway_url, headers, audit=None):
        self.store, self.gateway_url, self.headers, self.audit = store, gateway_url, headers, audit
        self.tasks = []
        self.media_slot = asyncio.Lock()
        self.upload_slots = asyncio.Semaphore(2)
        self.export_slot = asyncio.Semaphore(1)
        self.poll_seconds = 1

    async def start(self):
        await run_io(self.store.initialize)
        await run_io(self.store.recover)
        self.tasks = [asyncio.create_task(self.worker(kind), name=f'batch-{kind}') for kind in MODELS]

    async def stop(self):
        for task in self.tasks: task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def check_cancel(self, item):
        if await run_io(self.store.cancelled, item['job_id']): raise CancelledBatch()

    async def media(self, item, action, output, page=0):
        # PDFium and decoders live in one short-lived process at a time. A bad
        # document cannot crash the web server or share decoder state across jobs.
        async with self.media_slot:
            await self.check_cancel(item)
            process = await asyncio.create_subprocess_exec(
                sys.executable, '-m', 'admin_app.batch_media', action,
                str(self.store.file_path(item['job_id'], item['id'])), item['extension'], str(output), '--page', str(page),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                cwd=str(Path(__file__).resolve().parent.parent),
                **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
            communication = asyncio.create_task(process.communicate())
            deadline = asyncio.get_running_loop().time() + 120
            try:
                while not communication.done():
                    await asyncio.wait({communication}, timeout=.3)
                    await self.check_cancel(item)
                    if asyncio.get_running_loop().time() > deadline:
                        raise BatchError(400, '文件预处理超过 120 秒，请拆分文件后重试')
                stdout, _ = communication.result()
                try: result = json.loads(stdout)
                except (ValueError, UnicodeError): raise BatchError(400, '文件无法解码，请检查文件内容或拆分后重试') from None
                if process.returncode: raise BatchError(400, result.get('error', '文件无法解码'))
                return result
            finally:
                if process.returncode is None:
                    try: process.kill()
                    except ProcessLookupError: pass
                await asyncio.shield(communication)

    async def infer(self, item, page, **kwargs):
        await self.check_cancel(item)
        request_id = f"batch-{item['id']}-{page}"
        await run_io(self.store.request_id, item, request_id)
        path = '/v1/chat/completions' if item['job']['kind'] == 'ocr' else '/v1/audio/transcriptions'
        async with httpx.AsyncClient(timeout=httpx.Timeout(630, connect=10), trust_env=False) as client:
            response = await client.post(self.gateway_url + path, headers={**self.headers(), 'X-Request-ID': request_id}, **kwargs)
        try: result = response.json()
        except ValueError: raise UncertainInference('推理响应无法解析，执行结果未知；未自动重试，请核查 Request ID') from None
        if response.status_code >= 400:
            error = result.get('error', {}) if isinstance(result, dict) else {}
            code = error.get('code', '') if isinstance(error, dict) else ''
            if not isinstance(code,str): code=''
            if (response.status_code == 429 and code in {'resource_busy','rate_limit_exceeded','global_concurrency_exceeded',
                    'key_concurrency_exceeded','model_concurrency_exceeded','pool_concurrency_exceeded'}) or code in {'model_inactive', 'model_unavailable'}:
                try:
                    delay=float(response.headers.get('Retry-After',5))
                    delay=min(60,max(2,delay)) if math.isfinite(delay) else 5
                except (TypeError,ValueError): delay = 5
                raise WaitForResource('等待模型或可用资源，稍后自动继续', delay)
            if code in {'upstream_unavailable','upstream_error','invalid_upstream_response'} or code.startswith('npu_') or response.status_code in {502,504}:
                raise UncertainInference('推理后端异常或已隔离，执行结果未知；未自动重试，请核查 Request ID')
            if response.status_code in {401,403}:
                raise BatchError(response.status_code,'调用凭据失效或权限不足；请修复配置后重新提交，未自动重试')
            if code=='daily_quota_exceeded':
                raise BatchError(response.status_code,'调用配额已用尽；请调整配额或到期后重新提交，未自动重试')
            raise BatchError(response.status_code, f'推理请求失败（HTTP {response.status_code}），可用 Request ID 核查调用记录')
        if not isinstance(result, dict): raise BatchError(502, '推理服务返回了无效结果')
        return result

    async def process(self, item):
        output = self.store.directory(item['job_id']) / (item['id'] + '.tmp')
        try:
            if item['job']['kind'] == 'ocr':
                info = await self.media(item, 'inspect', output)
                await run_io(self.store.progress, item, pages=info['pages'])
                result = json.loads(item['result'])
                pages = result.setdefault('pages', [])
                for page in range(len(pages), info['pages']):
                    await self.check_cancel(item)
                    await run_io(self.store.progress, item, stage=f"识别第 {page+1}/{info['pages']} 页")
                    await self.media(item, 'image', output, page)
                    encoded = base64.b64encode(await run_io(output.read_bytes)).decode()
                    response = await self.infer(item, page+1, json={
                        'model': MODELS['ocr'], 'stream': False, 'max_tokens': 2048, 'temperature': 0,
                        'messages': [{'role': 'user', 'content': [
                            {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + encoded}},
                            {'type': 'text', 'text': 'Text Recognition:'}]}]})
                    try:
                        choice = response['choices'][0]
                        content = choice['message']['content']
                        if not isinstance(content, str): raise TypeError()
                    except (KeyError, IndexError, TypeError): raise BatchError(502, 'OCR 返回内容缺失') from None
                    warning = '输出达到长度上限，内容可能不完整；请拆分页面后重试' if choice.get('finish_reason') == 'length' else ''
                    pages.append({'page': page+1, 'text': content, 'usage': response.get('usage', {}),
                                  'warning': warning, 'request_id': f"batch-{item['id']}-{page+1}"})
                    result['text'] = '\n\n'.join(p['text'] for p in pages)
                    await run_io(self.store.progress, item, result=result,
                                            warning='；'.join(dict.fromkeys(p['warning'] for p in pages if p['warning'])))
            else:
                await run_io(self.store.progress, item, stage='音频转码中')
                info = await self.media(item, 'audio', output)
                await run_io(self.store.progress, item, stage=f"语音识别中 · {info['duration']:.1f} 秒音频")
                with output.open('rb') as audio:
                    result = await self.infer(item, 1, files={'file': ('audio.wav', audio, 'audio/wav')},
                                              data={'model': MODELS['asr'], 'response_format': 'verbose_json', 'language': item['job']['language']})
                if not isinstance(result.get('text'), str): raise BatchError(502, 'ASR 返回内容缺失')
                result.setdefault('duration', info['duration'])
                await run_io(self.store.progress, item, result=result)
            await self.check_cancel(item)
            await run_io(self.store.finish, item, 'succeeded')
        finally:
            output.unlink(missing_ok=True)

    async def worker(self, kind):
        while True:
            try:
                item = await run_io(self.store.claim, kind)
                if item is None:
                    await asyncio.sleep(self.poll_seconds)
                    continue
                try:
                    await self.process(item)
                except WaitForResource as exc:
                    await run_io(self.store.defer, item, exc.message, exc.delay)
                    continue
                except CancelledBatch:
                    await run_io(self.store.finish, item, 'cancelled')
                except BatchError as exc:
                    await run_io(self.store.finish, item, 'failed', exc.message)
                except UncertainInference as exc:
                    await run_io(self.store.finish, item, 'interrupted', str(exc))
                except httpx.TransportError:
                    await run_io(self.store.finish, item, 'interrupted', '推理连接中断，执行结果未知；未自动重试，请核查 Request ID 后重新提交')
                except Exception as exc:
                    LOGGER.error('Batch file %s failed (%s)', item['id'], type(exc).__name__)
                    await run_io(self.store.finish, item, 'failed', '文件处理失败，请检查文件内容或查看服务日志')
                if self.audit:
                    job = await run_io(self.store.get, item['job_id'])
                    finished = next(i for i in job['items'] if i['id'] == item['id'])
                    await run_io(self.audit, 'batch-file', job['id'], finished['status'], finished['request_id'],
                                            {'batch_id': job['id'], 'file_id': item['id'], 'kind': kind, 'error': finished['error']})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.error('Batch queue deferred (%s)', type(exc).__name__)
                await asyncio.sleep(self.poll_seconds)
