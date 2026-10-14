"""Bounded, ephemeral media requests through the existing management gateway."""
from __future__ import annotations

import asyncio
import io
import json
import time
import wave
from typing import Literal
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

MAX_IMAGE = 12 * 1024 * 1024
MAX_AUDIO = 20 * 1024 * 1024
MAX_RESPONSE = 16 * 1024 * 1024
TIMEOUT = 180
CV_MODELS = {
    'classify': {'mobilenet_v2', 'inception_v3'},
    'detect': {'yolov11_l', 'yolov11_n', 'yolov11n_obb', 'yolov12_s', 'yolov12_m', 'bytetrack_s'},
    'face': {'retinaface', 'yolov5s_face', 'scrfd', 'arcface'},
    'segment': {'yolov8l_seg'},
}


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: Literal['matcha-zh', 'kokoro-82m']
    input: str = Field(min_length=1, max_length=500)
    speed: float = Field(default=1, ge=.5, le=2, allow_inf_nan=False)


async def read_body(request, limit):
    body = bytearray()
    try:
        async with asyncio.timeout(30):
            async for chunk in request.stream():
                if len(body) + len(chunk) > limit:
                    raise HTTPException(413, '文件超过体验页大小限制')
                body.extend(chunk)
    except TimeoutError:
        raise HTTPException(408, '文件上传超时，尚未提交模型推理') from None
    if not body:
        raise HTTPException(422, '请选择非空文件')
    return bytes(body)


def image_type(raw):
    from PIL import Image
    try:
        with Image.open(io.BytesIO(raw)) as picture:
            if (picture.format not in {'PNG', 'JPEG', 'WEBP'} or picture.width * picture.height > 20_000_000
                    or max(picture.size) > 8192 or getattr(picture, 'n_frames', 1) != 1):
                raise ValueError()
            mime = Image.MIME[picture.format]
            picture.verify()
        return mime
    except Exception:
        raise HTTPException(422, '图片须为单张 PNG、JPEG 或 WebP，最多 2000 万像素、单边 8192 像素') from None


def wave_info(raw, *, seconds=300):
    try:
        with wave.open(io.BytesIO(raw), 'rb') as audio:
            channels, rate, frames = audio.getnchannels(), audio.getframerate(), audio.getnframes()
            if (audio.getcomptype() != 'NONE' or audio.getsampwidth() != 2 or channels not in {1, 2}
                    or not 8000 <= rate <= 48000 or not 0 < frames <= rate * seconds
                    or len(audio.readframes(frames)) != frames * channels * 2):
                raise ValueError()
        return {'duration': frames / rate, 'sample_rate': rate, 'channels': channels}
    except Exception:
        raise HTTPException(422, '请使用完整的 PCM 16-bit WAV：单/双声道、8–48 kHz，最长 5 分钟') from None


async def forward(owner, request, path, **kwargs):
    """Fixed route targets only. No retries; cancellation closes our upstream connection."""
    if not path.startswith(('/v1/cv/', '/v1/audio/')) or '://' in path or '\\' in path:
        raise HTTPException(400, '不支持的体验接口')
    async def receive():
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(TIMEOUT, connect=10)) as client:
            async with client.stream('POST', owner.GATEWAY_URL + path,
                    headers={**owner._gateway_headers(), 'X-Request-ID': request.state.request_id, 'Accept-Encoding': 'identity'}, **kwargs) as upstream:
                raw = bytearray()
                async for chunk in upstream.aiter_bytes():
                    if len(raw) + len(chunk) > MAX_RESPONSE:
                        raise HTTPException(502, '模型响应超过 16 MB，已停止接收；未重试')
                    raw.extend(chunk)
                return upstream.status_code, upstream.headers.get('Content-Type', ''), bytes(raw)

    task = asyncio.create_task(receive())
    try:
        async with asyncio.timeout(TIMEOUT):
            while not task.done():
                if await request.is_disconnected():
                    raise HTTPException(499, '客户端已停止接收；模型计算不保证立即停止')
                await asyncio.wait({task}, timeout=.2)
            return task.result()
    except (httpx.HTTPError, TimeoutError):
        raise HTTPException(504, '网关请求超时或连接中断，执行结果待确认；未自动重试') from None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def perform(owner, request, model, path, *, binary=False, **kwargs):
    started = time.monotonic()
    await owner._audit_request(request, 'api-test', model, 'started', {'kind': 'media', 'endpoint': path.split('?')[0]})
    try:
        status, content_type, raw = await owner._experience_proxy(request, path, **kwargs)
        if not 200 <= status < 300:
            # Raw upstream diagnostics can contain keys/paths; retain status, not body.
            raise HTTPException(status if 400 <= status < 600 else 502,
                                f'网关返回 HTTP {status}，请结合请求 ID 查看运行诊断；未重试')
        if binary:
            try:
                wave_info(raw)
            except HTTPException:
                raise HTTPException(502, 'TTS 未返回完整的可播放 PCM WAV，请查看运行诊断') from None
            mime = 'audio/wav'
        else:
            try:
                value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if not isinstance(value, dict):
                    raise ValueError()
            except (ValueError, RecursionError, UnicodeError):
                raise HTTPException(502, '模型没有返回有效 JSON 对象') from None
            mime = 'application/json'
        await owner._audit_request(request, 'api-test', model, 'succeeded', {'kind': 'media', 'bytes': len(raw)})
        return Response(raw, media_type=mime, headers={'X-Experience-Elapsed-Ms': str(round((time.monotonic() - started)*1000, 1))})
    except BaseException as error:
        status = 'failed' if isinstance(error, HTTPException) and error.status_code not in {499, 504} else 'unknown'
        await owner._audit_request(request, 'api-test', model, status, {'kind': 'media', 'http_status': getattr(error, 'status_code', None)})
        raise


def router(owner):
    routes = APIRouter(prefix='/api/test', dependencies=[Depends(owner.require_csrf)])

    @routes.post('/cv/{operation}')
    async def cv(operation: Literal['classify', 'detect', 'face', 'segment'], request: Request,
                 model: str, confidence: float = Query(.25, ge=0, le=1), iou: float = Query(.45, ge=0, le=1)):
        if model not in CV_MODELS[operation]:
            raise HTTPException(422, '模型与视觉任务类型不匹配')
        raw = await read_body(request, MAX_IMAGE)
        await asyncio.to_thread(image_type, raw)
        return await perform(owner, request, model, '/v1/cv/' + operation + '?' + urlencode({'model': model, 'confidence': confidence, 'iou': iou}), content=raw)

    @routes.post('/asr')
    async def asr(request: Request, model: Literal['sensevoice'] = 'sensevoice',
                  language: Literal['auto', 'zh', 'en', 'ja', 'ko', 'yue'] = 'auto'):
        raw = await read_body(request, MAX_AUDIO)
        await asyncio.to_thread(wave_info, raw)
        return await perform(owner, request, model, '/v1/audio/transcriptions',
                             data={'model': model, 'language': language, 'response_format': 'verbose_json'},
                             files={'file': ('audio.wav', raw, 'audio/wav')})

    @routes.post('/tts')
    async def tts(request: Request, payload: SpeechRequest):
        if not payload.input.strip():
            raise HTTPException(422, '请输入要合成的文字')
        body = payload.model_dump() | {'response_format': 'wav', 'voice': 'zh-baker-female' if payload.model == 'matcha-zh' else 'zf_xiaoyi'}
        return await perform(owner, request, payload.model, '/v1/audio/speech', binary=True, json=body)

    return routes
