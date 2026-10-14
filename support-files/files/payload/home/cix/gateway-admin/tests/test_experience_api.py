import asyncio
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from admin_app import experience_api as media
from experience_fixture import audio_sample


def png():
    out = io.BytesIO(); Image.new('RGB', (80, 40), 'white').save(out, format='PNG'); return out.getvalue()


class MediaTests(unittest.TestCase):
    def setUp(self):
        from admin_app import main
        self.main = main
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.object(main, 'DATA_DIR', Path(self.temp.name)))
        self.enterContext(patch.object(main, 'DB_PATH', Path(self.temp.name)/'modeldeck.db'))
        self.proxy = self.enterContext(patch.object(main, '_experience_proxy', AsyncMock(return_value=(200, 'application/json', b'{"ok":true}'))))
        self.client = self.enterContext(TestClient(main.app))
        main.SESSIONS['experience-test'] = {'csrf': 'test-csrf', 'expires_at': time.time()+3600}
        self.addCleanup(main.SESSIONS.pop, 'experience-test', None)
        self.client.cookies.set('modeldeck_session', 'experience-test')
        self.headers = {'X-CSRF-Token': 'test-csrf', 'X-Request-ID': 'experience-request'}

    def test_auth_csrf_and_csp(self):
        for path in ['/api/test/cv/detect?model=yolov11_n', '/api/test/asr', '/api/test/tts', '/api/test/chat']:
            self.assertEqual(self.client.post(path, json={}).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.post('/api/test/asr', content=audio_sample(), headers=self.headers).status_code, 401)
        self.assertIn("media-src 'self' blob:", self.client.get('/').headers['content-security-policy'])
        self.proxy.assert_not_awaited()

    def test_cv_image_validation_and_fixed_gateway_route(self):
        response = self.client.post('/api/test/cv/detect?model=yolov11_n&confidence=0.4&iou=0.5', content=png(), headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn('/v1/cv/detect?model=yolov11_n', self.proxy.call_args.args[1])
        self.assertEqual(self.proxy.call_args.kwargs['content'], png())
        for path, data in [('/api/test/cv/detect?model=arcface', png()), ('/api/test/cv/detect?model=yolov11_n', b'<svg/>'), ('/api/test/cv/detect?model=yolov11_n&confidence=nan', png())]:
            self.assertEqual(self.client.post(path, content=data, headers=self.headers).status_code, 422)
        self.assertEqual(self.proxy.await_count, 1)

    def test_asr_pcm_validation_fixed_filename_and_verbose_segments(self):
        self.assertEqual(self.client.post('/api/test/asr', content=b'not wav', headers=self.headers).status_code, 422)
        response = self.client.post('/api/test/asr?language=zh', content=audio_sample(), headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.proxy.call_args.kwargs['files']['file'][0], 'audio.wav')
        self.assertEqual(self.proxy.call_args.kwargs['data']['response_format'], 'verbose_json')
        self.assertEqual(self.client.post('/api/test/asr', content=audio_sample()[:-10], headers=self.headers).status_code, 422)

    def test_tts_only_installed_voices_and_validated_audio(self):
        self.proxy.return_value = 200, 'audio/wav', audio_sample()
        response = self.client.post('/api/test/tts', headers=self.headers, json={'model':'matcha-zh', 'input':'你好'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers['content-type'].startswith('audio/wav'))
        self.assertEqual(self.proxy.call_args.kwargs['json']['voice'], 'zh-baker-female')
        for body in [{'model':'unknown','input':'x'}, {'model':'matcha-zh','input':' '}, {'model':'matcha-zh','input':'a'*501}, {'model':'matcha-zh','input':'x','voice':'download-new-voice'}]:
            self.assertEqual(self.client.post('/api/test/tts', headers=self.headers, json=body).status_code, 422)
        self.proxy.return_value = 200, 'application/json', b'{"error":"secret"}'
        self.assertEqual(self.client.post('/api/test/tts', headers=self.headers, json={'model':'matcha-zh','input':'x'}).status_code, 502)

    def test_errors_are_not_retried_or_echoed_and_audit_has_no_media(self):
        self.proxy.return_value = 503, 'application/json', b'{"secret":"private-key"}'
        response = self.client.post('/api/test/asr', content=audio_sample(), headers=self.headers)
        self.assertEqual(response.status_code, 503); self.assertNotIn('private-key', response.text)
        self.assertEqual(self.proxy.await_count, 1)
        records = self.client.get('/api/audit?request_id=experience-request').json()['data']
        self.assertTrue(any(row['action'] == 'api-test' for row in records))
        self.assertNotIn('RIFF', json.dumps(records)); self.assertNotIn('private-key', json.dumps(records))

    def test_image_bodies_are_bounded_before_inference(self):
        with patch.object(media, 'MAX_IMAGE', 10):
            self.assertEqual(self.client.post('/api/test/cv/detect?model=yolov11_n', content=png(), headers=self.headers).status_code, 413)
        self.proxy.assert_not_awaited()


class ForwardTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_upload_reports_timeout_before_inference(self):
        async def chunks():
            yield b'first'
            raise TimeoutError()
        with self.assertRaises(HTTPException) as error:
            await media.read_body(SimpleNamespace(stream=chunks), 100)
        self.assertEqual(error.exception.status_code, 408)

    async def test_gateway_only_auth_identity_and_response_limit(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=b'12345')
        owner = SimpleNamespace(GATEWAY_URL='http://gateway:17700', _gateway_headers=lambda: {'X-Gateway-Admin-Token':'private-test-token'})
        request = SimpleNamespace(state=SimpleNamespace(request_id='media-id'), is_disconnected=AsyncMock(return_value=False))
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.object(media.httpx, 'AsyncClient', return_value=client), patch.object(media, 'MAX_RESPONSE', 4):
            with self.assertRaises(HTTPException) as error:
                await media.forward(owner, request, '/v1/audio/speech', json={'model':'matcha-zh'})
        self.assertEqual(error.exception.status_code, 502)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers['X-Request-ID'], 'media-id')
        self.assertEqual(requests[0].headers['X-Gateway-Admin-Token'], 'private-test-token')
        self.assertNotIn('Authorization', requests[0].headers)

    async def test_disconnect_stops_receiving_without_retry(self):
        request = SimpleNamespace(state=SimpleNamespace(request_id='media-id'), is_disconnected=AsyncMock(return_value=True))
        owner = SimpleNamespace(GATEWAY_URL='http://gateway:17700', _gateway_headers=lambda: {})
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
        with patch.object(media.httpx, 'AsyncClient', return_value=client):
            with self.assertRaises(HTTPException) as error: await media.forward(owner, request, '/v1/audio/speech', json={})
        await client.aclose()
        self.assertEqual(error.exception.status_code, 499)
