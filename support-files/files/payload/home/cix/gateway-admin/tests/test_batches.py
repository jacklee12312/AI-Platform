from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
import uuid
import wave
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import httpx
from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from admin_app.batch_store import BatchStore, BatchError
from admin_app.batch_engine import BatchEngine, WaitForResource, UncertainInference
from admin_app.batch_api import result_zip, ResultDownload, store_call
from admin_app import batch_media


def png():
    output = io.BytesIO()
    Image.new('RGB', (240, 80), 'white').save(output, 'PNG')
    return output.getvalue()


def wav():
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(2); audio.setsampwidth(2); audio.setframerate(44100)
        audio.writeframes(b'\0' * 44100 * 4)
    return output.getvalue()


def add(store, names=('sample.png',), kind='ocr', contents=None, submit=True):
    job = store.create(uuid.uuid4().hex, '验收批次', kind, 'auto')
    for index, name in enumerate(names):
        body = contents[index] if contents else png()
        item, _ = store.reserve_file(job['id'], uuid.uuid4().hex, name, len(body))
        store.file_path(job['id'], item['id']).write_bytes(body)
        store.finish_upload(job['id'], item['id'], hashlib.sha256(body).hexdigest())
    if submit: store.submit(job['id'])
    return store.get(job['id'])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = BatchStore(Path(self.temp.name)); self.store.initialize()

    def tearDown(self): self.temp.cleanup()

    def test_atomic_claim_and_independent_queues(self):
        add(self.store, ('a.png', 'b.png'))
        add(self.store, ('a.wav',), 'asr', [wav()])
        with ThreadPoolExecutor(8) as pool:
            claimed = list(pool.map(lambda _: self.store.claim('ocr'), range(8)))
        self.assertEqual(sum(item is not None for item in claimed), 1)
        self.assertIsNotNone(self.store.claim('asr'))

    def test_queue_position_uses_submission_order_not_draft_creation(self):
        slow_upload = add(self.store, submit=False)
        early_submit = add(self.store)
        self.store.submit(slow_upload['id'])
        self.assertEqual(self.store.get(slow_upload['id'])['queue_position'], 2)
        self.assertEqual(self.store.get(early_submit['id'])['queue_position'], 1)
        self.assertEqual(self.store.claim('ocr')['job_id'], early_submit['id'])

    def test_idempotency_limits_and_confined_paths(self):
        identity = uuid.uuid4().hex
        self.store.create(identity, '同一任务', 'ocr', 'auto')
        self.assertEqual(self.store.create(identity, '同一任务', 'ocr', 'auto')['id'], identity)
        with self.assertRaises(BatchError): self.store.create(identity, '冲突', 'ocr', 'auto')
        item, fresh = self.store.reserve_file(identity, uuid.uuid4().hex, '../../evil.png', 3)
        self.assertEqual(item['filename'], 'evil.png')
        self.store.file_path(identity, item['id']).write_bytes(b'abc')
        self.store.finish_upload(identity, item['id'], 'hash')
        self.assertFalse(self.store.reserve_file(identity, item['id'], 'evil.png', 3)[1])
        self.store.abandon_upload(identity, item['id'])
        self.assertEqual(self.store.file_path(identity, item['id']).read_bytes(), b'abc')
        for filename, size in [('x.exe', 2), ('a.png', 10*1024*1024+1), ('a.pdf', 40*1024*1024+1)]:
            with self.assertRaises(BatchError): self.store.reserve_file(identity, uuid.uuid4().hex, filename, size)
        with self.assertRaises(BatchError): self.store.directory('../../outside')
        self.store.submit(identity); self.store.submit(identity)
        self.assertEqual(len(self.store.listing()['data']), 1)
        with self.assertRaises(BatchError): self.store.delete(identity)

    def test_restart_preserves_queue_and_never_repeats_unknown_item(self):
        job = add(self.store, ('a.png', 'b.png'))
        running = self.store.claim('ocr')
        self.store.progress(running, pages=2, result={'text': 'saved page', 'pages': [{'text': 'saved page'}]})
        recovered = BatchStore(Path(self.temp.name)); recovered.initialize(); recovered.recover()
        detail = recovered.get(job['id'], results=True)
        self.assertEqual(detail['items'][0]['status'], 'interrupted')
        self.assertEqual(detail['items'][0]['result']['text'], 'saved page')
        self.assertEqual(recovered.claim('ocr')['filename'], 'b.png')

    def test_cancel_keeps_completed_results_and_delete_releases_storage(self):
        job = add(self.store, ('a.png', 'b.png'))
        item = self.store.claim('ocr')
        self.store.progress(item, result={'text': 'partial', 'pages': [{'text': 'partial'}]})
        self.assertEqual(self.store.cancel(job['id'])['status'], 'cancelling')
        self.assertIsNone(self.store.claim('ocr'))
        self.store.finish(item, 'succeeded')
        result = self.store.get(job['id'], results=True)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['items'][0]['result']['text'], 'partial')
        self.store.delete(job['id'])
        self.assertEqual(self.store.listing()['storage_bytes'], 0)
        self.assertFalse(self.store.directory(job['id']).exists())

    def test_cancel_cannot_hide_unknown_inference_outcome_or_restart(self):
        for restart in (False,True):
            job=add(self.store,('unknown.png','not-started.png'))
            item=self.store.claim('ocr');self.store.cancel(job['id'])
            if restart:self.store.recover()
            else:self.store.finish(item,'interrupted','结果未知')
            result=self.store.get(job['id'])
            self.assertEqual(result['status'],'interrupted')
            self.assertEqual([value['status'] for value in result['items']],['interrupted','cancelled'])
            self.assertIsNone(self.store.claim('ocr'))

    def test_recover_abandoned_upload_and_output_limits(self):
        job = self.store.create(uuid.uuid4().hex, 'draft', 'ocr', 'auto')
        item, _ = self.store.reserve_file(job['id'], uuid.uuid4().hex, 'a.png', 3)
        self.store.file_path(job['id'], item['id']).write_bytes(b'a')
        self.store.recover()
        self.assertEqual(self.store.get(job['id'])['total'], 0)
        job = add(self.store)
        item = self.store.claim('ocr')
        with self.assertRaises(BatchError): self.store.progress(item, pages=201)
        with self.assertRaises(BatchError): self.store.progress(item, result={'text': 'x'*1024**2})

    def test_zip_duplicate_names_partial_status_and_csv_formula(self):
        job = add(self.store, ('=cmd.png', '=cmd.png'))
        item = self.store.claim('ocr')
        self.store.progress(item, result={'text': '已识别', 'pages': []})
        self.store.finish(item, 'succeeded'); self.store.cancel(job['id'])
        target = Path(self.temp.name) / 'result.zip'
        result_zip(self.store.get(job['id'], results=True), target)
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(archive.read('001__cmd.txt').decode(), '已识别')
            self.assertIn("'=cmd.png", archive.read('summary.csv').decode('utf-8-sig'))
            manifest = json.loads(archive.read('manifest.json'))
            self.assertFalse(manifest['complete'])
            self.assertEqual(manifest['items'][1]['status'], 'cancelled')


class MediaTests(unittest.TestCase):
    def test_pdf_pages_render_and_corrupt_image(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input.pdf'; target = Path(folder) / 'output.jpg'
            pages = [Image.new('RGB', (500, 300), color) for color in ['white', 'red']]
            pages[0].save(path, 'PDF', save_all=True, append_images=pages[1:])
            self.assertEqual(batch_media.inspect(path, '.pdf')['pages'], 2)
            batch_media.image_page(path, '.pdf', 1, target)
            with Image.open(target) as im: self.assertLessEqual(max(im.size), 2048)
            path.write_bytes(b'invalid')
            with self.assertRaises(Exception): batch_media.inspect(path, '.png')

    def test_audio_resamples_stereo_and_limits_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input.wav'; target = Path(folder) / 'output.wav'; path.write_bytes(wav())
            self.assertAlmostEqual(batch_media.audio_wave(path, '.wav', target)['duration'], 1)
            with wave.open(str(target)) as audio:
                self.assertEqual((audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes()), (16000, 1, 2, 16000))
            with mock.patch.object(batch_media, 'MAX_AUDIO_SECONDS', .5):
                with self.assertRaises(ValueError): batch_media.audio_wave(path, '.wav', target)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = BatchStore(Path(self.temp.name)); self.store.initialize()
        self.engine = BatchEngine(self.store, 'http://gateway', lambda: {'X-Gateway-Admin-Token': 'test-management'})
        self.engine.poll_seconds = .02

    async def asyncTearDown(self):
        await self.engine.stop(); self.temp.cleanup()

    async def wait_terminal(self, job_id):
        for _ in range(500):
            job = self.store.get(job_id, results=True)
            if job['status'] in {'succeeded', 'failed', 'partial', 'cancelled', 'interrupted'}: return job
            await asyncio.sleep(.02)
        self.fail('queue did not finish')

    async def test_real_preprocessing_and_failed_file_isolation(self):
        job = add(self.store, ('bad.png', 'good.png'), contents=[b'broken', png()])
        self.engine.infer = mock.AsyncMock(return_value={'choices': [{'message': {'content': 'OCR 文本'}, 'finish_reason': 'stop'}]})
        await self.engine.start()
        result = await self.wait_terminal(job['id'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['items'][1]['result']['text'], 'OCR 文本')
        self.assertEqual(self.engine.infer.await_count, 1)

    async def test_cancel_inference_preserves_page_and_stops_next(self):
        job = add(self.store, ('a.png', 'b.png'))
        entered, release = asyncio.Event(), asyncio.Event()
        async def infer(*args, **kwargs):
            entered.set(); await release.wait()
            return {'choices': [{'message': {'content': '已完成页面'}}]}
        self.engine.infer = mock.AsyncMock(side_effect=infer)
        await self.engine.start(); await asyncio.wait_for(entered.wait(), 10)
        self.store.cancel(job['id']); release.set()
        result = await self.wait_terminal(job['id'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.engine.infer.await_count, 1)
        self.assertEqual(result['items'][0]['result']['text'], '已完成页面')

    async def test_cancel_then_timeout_keeps_unknown_and_queue_can_process_new_job(self):
        job=add(self.store,('unknown.png','cancelled.png'));entered,release=asyncio.Event(),asyncio.Event()
        async def inference(*args,**kwargs):
            entered.set();await release.wait();raise httpx.ReadTimeout('upstream result unknown')
        self.engine.infer=mock.AsyncMock(side_effect=inference)
        await self.engine.start();await asyncio.wait_for(entered.wait(),10)
        self.store.cancel(job['id']);release.set()
        result=await self.wait_terminal(job['id'])
        self.assertEqual(result['status'],'interrupted')
        self.assertEqual(self.engine.infer.await_count,1)
        self.engine.infer=mock.AsyncMock(return_value={'choices':[{'message':{'content':'next job'}}]})
        next_job=add(self.store)
        self.assertEqual((await self.wait_terminal(next_job['id']))['status'],'succeeded')

    async def test_protocol_disconnect_is_unknown_not_a_known_failure(self):
        job=add(self.store)
        self.engine.infer=mock.AsyncMock(side_effect=httpx.RemoteProtocolError('peer closed early'))
        await self.engine.start();result=await self.wait_terminal(job['id'])
        self.assertEqual(result['status'],'interrupted');self.assertEqual(self.engine.infer.await_count,1)

    async def test_auth_quota_and_unknown_server_errors_are_not_retried(self):
        job=add(self.store);item=self.store.claim('ocr')
        for status,code,exception in [(401,'api_key_expired',BatchError),(403,'model_not_allowed',BatchError),
                                    (429,'daily_quota_exceeded',BatchError),(429,'unknown_limit',BatchError),
                                    (503,'upstream_unavailable',UncertainInference),(503,'npu_quarantined',UncertainInference)]:
            calls=[]
            def handler(request):
                calls.append(request);return httpx.Response(status,json={'error':{'code':code}})
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
            with self.subTest(code=code),mock.patch('admin_app.batch_engine.httpx.AsyncClient',return_value=client):
                with self.assertRaises(exception):await self.engine.infer(item,1,json={})
            self.assertEqual(len(calls),1)

    async def test_nonfinite_retry_after_is_bounded_and_malformed_response_is_unknown(self):
        job=add(self.store);item=self.store.claim('ocr')
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(429,json={'error':{'code':'resource_busy'}},headers={'Retry-After':'NaN'})))
        with mock.patch('admin_app.batch_engine.httpx.AsyncClient',return_value=client):
            with self.assertRaises(WaitForResource) as caught:await self.engine.infer(item,1,json={})
        self.assertEqual(caught.exception.delay,5)
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,content=b'broken-json')))
        with mock.patch('admin_app.batch_engine.httpx.AsyncClient',return_value=client):
            with self.assertRaises(UncertainInference):await self.engine.infer(item,1,json={})

    async def test_cancelled_store_call_drains_transaction_before_recovery(self):
        import threading
        entered,release=threading.Event(),threading.Event()
        job=add(self.store);item=self.store.claim('ocr')
        def committing():
            entered.set();release.wait(5);self.store.progress(item,result={'text':'committed page'})
        task=asyncio.create_task(store_call(committing))
        await asyncio.wait_for(asyncio.to_thread(entered.wait),5)
        task.cancel();await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):await task
        self.store.recover()
        result=self.store.get(job['id'],results=True)
        self.assertEqual(result['items'][0]['result']['text'],'committed page')
        self.assertEqual(result['items'][0]['status'],'interrupted')

    async def test_resource_wait_resumes_pdf_at_unfinished_page(self):
        path = Path(self.temp.name) / 'test.pdf'
        im = Image.new('RGB', (300, 100), 'white'); im.save(path, 'PDF', save_all=True, append_images=[im])
        job = add(self.store, ('two.pdf',), contents=[path.read_bytes()]); calls = []
        async def infer(item, page, **kwargs):
            calls.append(page)
            if len(calls) == 2: raise WaitForResource('等待', .03)
            return {'choices': [{'message': {'content': str(page)}, 'finish_reason': 'length' if page == 2 else 'stop'}]}
        self.engine.infer = infer
        await self.engine.start(); result = await self.wait_terminal(job['id'])
        self.assertEqual(calls, [1, 2, 2]); self.assertEqual(result['items'][0]['completed_pages'], 2)
        self.assertIn('可能不完整', result['items'][0]['warning'])

    async def test_network_timeout_not_retried_and_asr_formats(self):
        job = add(self.store, ('a.wav', 'b.wav'), 'asr', [wav(), wav()])
        self.engine.infer = mock.AsyncMock(side_effect=[httpx.ReadTimeout('unknown'), {'text': '识别文本', 'segments': []}])
        await self.engine.start(); result = await self.wait_terminal(job['id'])
        self.assertEqual([i['status'] for i in result['items']], ['interrupted', 'succeeded'])
        self.assertEqual(self.engine.infer.await_count, 2)
        self.assertEqual(result['items'][1]['result']['duration'], 1)

    async def test_infer_uses_gateway_admission_and_request_id(self):
        job = add(self.store); item = self.store.claim('ocr')
        def handler(request):
            self.assertEqual(request.headers['X-Gateway-Admin-Token'], 'test-management')
            self.assertNotIn('Authorization', request.headers)
            self.assertEqual(request.url.path, '/v1/chat/completions')
            self.assertEqual(request.headers['X-Request-ID'], f"batch-{item['id']}-1")
            return httpx.Response(429, json={'error': {'code': 'resource_busy'}}, headers={'Retry-After': '3'})
        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        with mock.patch('admin_app.batch_engine.httpx.AsyncClient', return_value=client):
            with self.assertRaises(WaitForResource) as caught: await self.engine.infer(item, 1, json={})
        self.assertEqual(caught.exception.delay, 3)
        self.assertTrue(self.store.get(job['id'])['items'][0]['request_id'].startswith('batch-'))

    async def test_worker_restart_does_not_repeat_active_inference(self):
        job = add(self.store, ('first.png', 'second.png'))
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set(); await asyncio.Event().wait()
        self.engine.infer = blocked
        await self.engine.start(); await asyncio.wait_for(entered.wait(), 10)
        await self.engine.stop()
        self.engine = BatchEngine(self.store, 'http://gateway', lambda: {})
        self.engine.poll_seconds = .02
        self.engine.infer = mock.AsyncMock(return_value={'choices': [{'message': {'content': 'recovered'}}]})
        await self.engine.start(); result = await self.wait_terminal(job['id'])
        self.assertEqual([i['status'] for i in result['items']], ['interrupted', 'succeeded'])
        self.assertEqual(self.engine.infer.await_count, 1)
        self.assertEqual(self.engine.infer.call_args.args[0]['filename'], 'second.png')


class APITests(unittest.TestCase):
    def setUp(self):
        from hash_password import encode
        self.temp = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(os.environ, ADMIN_DATA_DIR=self.temp.name, ADMIN_PASSWORD_HASH=encode('batch-test-password'))
        self.environment.start(); sys.modules.pop('admin_app.main', None)
        self.module = importlib.import_module('admin_app.main')
        self.worker = mock.patch.object(BatchEngine, 'worker', new=mock.AsyncMock())
        self.worker.start(); self.context = TestClient(self.module.app); self.client = self.context.__enter__()
        self.headers = {'X-CSRF-Token': self.client.post('/api/login', json={'password': 'batch-test-password'}).json()['csrf']}

    def tearDown(self):
        self.context.__exit__(None, None, None); self.worker.stop(); self.environment.stop(); self.temp.cleanup()

    def create(self):
        body = {'id': uuid.uuid4().hex, 'name': 'API 验收', 'kind': 'ocr'}
        response = self.client.post('/api/batches', json=body, headers=self.headers)
        self.assertEqual(response.status_code, 201); return response.json()

    def test_auth_csrf_and_schema(self):
        self.assertEqual(self.client.post('/api/batches', json={}).status_code, 422)
        self.assertEqual(self.client.post('/api/batches', json={'id': uuid.uuid4().hex, 'name': 'x', 'kind': 'ocr'}).status_code, 403)
        job = self.create(); self.client.cookies.clear()
        for path in ['/api/batches', f"/api/batches/{job['id']}", f"/api/batches/{job['id']}/results.zip"]:
            self.assertEqual(self.client.get(path).status_code, 401)

    def test_upload_size_cleanup_submit_idempotency_and_download(self):
        job = self.create(); file_id = uuid.uuid4().hex
        path = f"/api/batches/{job['id']}/files/{file_id}?filename=a.png&size=3"
        self.assertEqual(self.client.put(path, content=b'ab', headers=self.headers).status_code, 400)
        self.assertEqual(self.client.get('/api/batches').json()['storage_bytes'], 0)
        self.assertEqual(self.client.put(path, content=b'abcd', headers=self.headers).status_code, 413)
        self.assertEqual(self.client.put(path, content=b'abc', headers=self.headers).status_code, 200)
        self.assertTrue(self.client.put(path, content=b'abc', headers=self.headers).json()['reused'])
        for _ in range(2): self.assertEqual(self.client.post(f"/api/batches/{job['id']}/submit", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get(f"/api/batches/{job['id']}/files/{uuid.uuid4().hex}/result").status_code, 404)
        response = self.client.get(f"/api/batches/{job['id']}/results.zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive: self.assertEqual(json.loads(archive.read('manifest.json'))['total'], 1)
        self.client.post(f"/api/batches/{job['id']}/cancel", headers=self.headers)
        self.assertEqual(self.client.delete(f"/api/batches/{job['id']}", headers=self.headers).status_code, 200)

    def test_download_range_rebuilds_snapshot_without_changing_results(self):
        job=self.create();file_id=uuid.uuid4().hex
        self.client.put(f"/api/batches/{job['id']}/files/{file_id}?filename=a.png&size=3",content=b'abc',headers=self.headers)
        first=self.client.get(f"/api/batches/{job['id']}/results.zip",headers={'Range':'bytes=0-15'})
        self.assertEqual(first.status_code,206);self.assertEqual(first.content[:2],b'PK')
        second=self.client.get(f"/api/batches/{job['id']}/results.zip")
        self.assertEqual(second.status_code,200)
        self.assertFalse(list((self.module.BATCH_ENGINE.store.root/'exports').glob('*.tmp')))
        self.assertEqual(self.module.BATCH_ENGINE.store.get(job['id'])['items'][0]['status'],'ready')


class DownloadDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_failure_and_cancellation_both_remove_temporary_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            for exception in (OSError('client disconnected'),asyncio.CancelledError()):
                path=Path(folder)/'result.tmp';path.write_bytes(b'PK' + b'x'*100)
                response=ResultDownload(path)
                async def send(message):
                    if message['type']=='http.response.body':raise exception
                async def receive():return {'type':'http.disconnect'}
                with self.assertRaises(type(exception)):
                    await response({'type':'http','method':'GET','headers':[],
                                    'extensions':{'http.response.pathsend':{}}},receive,send)
                self.assertFalse(path.exists())


if __name__ == '__main__': unittest.main()
