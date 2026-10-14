"""Copyable public-API examples; never insert a stored business or admin key."""
from __future__ import annotations

import copy
import json

DEFAULT_URL = 'http://192.168.133.1:17700'


def examples_for(model_id: str, profile: str, base_url: str = DEFAULT_URL):
    chat = {'model': model_id, 'messages': [{'role': 'user', 'content': '请用一句话介绍边缘计算。'}],
            'max_tokens': 128, 'temperature': 0.2, 'stream': False}
    examples = []
    def add(id, title, path, body=None, transport='json', note=''):
        examples.append({'id': id, 'title': title, 'method': 'POST', 'path': path,
                         'body': body, 'transport': transport, 'note': note})
    if profile in {'chat', 'vision', 'ocr'}:
        if profile != 'chat':
            chat['messages'][0]['content'] = [
                {'type': 'image_url', 'image_url': {'url': '__IMAGE_DATA_URI__'}},
                {'type': 'text', 'text': 'Text Recognition:' if profile == 'ocr' else '请描述这张图片。'}]
        add('json', '图片文字识别' if profile == 'ocr' else '图片问答' if profile == 'vision' else '文本对话',
            '/v1/chat/completions', chat, 'image_json' if profile != 'chat' else 'json')
        stream = copy.deepcopy(chat); stream['stream'] = True
        add('sse', 'SSE 流式返回', '/v1/chat/completions', stream, 'image_sse' if profile != 'chat' else 'sse',
            '收到 HTTP 200 后仍需检查 error 事件和 [DONE]；Python 示例会校验流是否完整结束。')
        if profile == 'ocr':
            add('legacy', '旧 OCR 路径兼容', '/v1/ocr/mineru', copy.deepcopy(chat), 'image_json',
                '仍由 GLM-OCR 处理图片；不是 MinerU 引擎，不接收 PDF，也不支持 stream=true。')
    elif profile == 'embeddings':
        add('json', '生成文本向量', '/v1/embeddings', {'model': model_id, 'input': ['边缘计算在设备附近处理数据。']})
    elif profile == 'rerank':
        add('json', '文档相关度重排', '/v1/rerank', {'model': model_id, 'query': '中国的首都是哪里？',
            'documents': ['北京是中国的首都。', '巴黎是法国的首都。', '东京是日本的首都。'], 'top_n': 3},
            note='历史记录存在异常分数，尚未闭环；这是接口接入样例，不是排序质量已达标的声明。')
    elif profile == 'moderation':
        add('json', '独立文本审核', '/v1/moderations', {'model': model_id, 'input': '请介绍社区图书馆的开放时间。'},
            note='这是独立审核接口；聊天是否自动审核另由全局 Guard 模式和模型 guard 标记决定。')
    elif profile.startswith('cv_'):
        operation = profile.removeprefix('cv_')
        if operation in {'tracking', 'obb'}: operation = 'detect'
        if operation == 'embedding': operation = 'face'
        add('image', '单张图片推理', f'/v1/cv/{operation}?model={model_id}&confidence=0.25&iou=0.45', transport='raw_image')
    elif profile in {'matcha', 'kokoro'}:
        add('wav', '生成完整 WAV 音频', '/v1/audio/speech', {
            'model': model_id, 'input': '你好，欢迎使用本地语音合成服务。',
            'voice': 'zh-baker-female' if profile == 'matcha' else 'zf_xiaoyi', 'speed': 1.0, 'response_format': 'wav'},
            'wav', '等待整段音频生成后返回；不提供客户端音频流。请显式指定 model，避免默认路由到其他 TTS。')
    elif profile == 'cosyvoice':
        add('wav', '使用参考音频生成完整 WAV', '/v1/audio/speech', {
            'model': model_id, 'input': '你好，欢迎使用六耳 AI 盒子。',
            'ref_audio': '__REFERENCE_AUDIO_DATA_URI__', 'ref_text': '参考音频中实际说出的完整文字。',
            'response_format': 'wav'}, 'voice_clone',
            '把 reference.wav 替换为已获授权的参考音频；ref_text 必须与音频内容一致。当前整段返回，实测速度低于实时。')
    elif profile == 'asr':
        add('wav', '上传 PCM WAV 转写', '/v1/audio/transcriptions', transport='multipart',
            note='公共 API 直接接收 PCM WAV。MP3/M4A 等格式请先转码，或使用管理员批量任务页面。')
        examples.append({'id': 'ws', 'title': 'VAD 分段 WebSocket', 'method': 'WS', 'path': '/v1/audio/transcriptions/stream',
                         'body': None, 'transport': 'websocket', 'note': '需安装 websockets==15.0.1；发送 PCM 帧，VAD 句段结束后返回 transcript，最后必须收到 done。'})
    elif profile == 'ballot':
        add('pdf', '上传民主测评票 PDF', f'/v1/documents/analyze?model={model_id}', transport='raw_document',
            note='示例上传 ballot.pdf；也可上传 PNG/JPEG。status=review 或 review_count>0 时必须进入人工复核，不能直接计票。')
    for example in examples:
        example['snippets'] = {'python': python_example(example, base_url)}
        if example['transport'] != 'websocket':
            example['snippets'].update(bash=shell_example(example, base_url=base_url),
                                       powershell=shell_example(example, powershell=True, base_url=base_url))
    return examples


def python_example(example, base_url: str = DEFAULT_URL):
    if example['transport'] == 'websocket': return websocket_example(base_url)
    transport = example['transport']
    lines = ['# Python 3；标准库即可。先设置环境变量 API_KEY，可选 GATEWAY_URL。',
             'import os, json, base64, uuid', 'from pathlib import Path',
             'from urllib.request import Request, urlopen', 'from urllib.error import HTTPError', '',
             f'base_url = os.environ.get("GATEWAY_URL", "{base_url}").rstrip("/")',
             'headers = {"Authorization": "Bearer " + os.environ["API_KEY"]}',
             f'url = base_url + {example["path"]!r}']
    if transport == 'raw_image':
        lines += ['data = Path("image.png").read_bytes()', 'headers["Content-Type"] = "image/png"']
    elif transport == 'raw_document':
        lines += ['document = Path("ballot.pdf")', 'data = document.read_bytes()',
                  'headers["Content-Type"] = "application/pdf" if document.suffix.lower() == ".pdf" else "application/octet-stream"']
    elif transport == 'multipart':
        lines += ['# input.wav 必须是未压缩 PCM WAV；不要仅修改 MP3 文件扩展名。',
                  'boundary = "modeldeck-" + uuid.uuid4().hex', 'parts = []',
                  'for key, value in {"model": "sensevoice", "language": "auto", "response_format": "verbose_json"}.items():',
                  '    parts.append((f"--{boundary}\\r\\nContent-Disposition: form-data; name=\\\"{key}\\\"\\r\\n\\r\\n{value}\\r\\n").encode())',
                  'parts.append((f"--{boundary}\\r\\nContent-Disposition: form-data; name=\\\"file\\\"; filename=\\\"input.wav\\\"\\r\\nContent-Type: audio/wav\\r\\n\\r\\n").encode())',
                  'parts.append(Path("input.wav").read_bytes())',
                  'parts.append((f"\\r\\n--{boundary}--\\r\\n").encode())',
                  'data = b"".join(parts)', 'headers["Content-Type"] = "multipart/form-data; boundary=" + boundary']
    else:
        lines += [f'payload = {example["body"]!r}']
        if transport.startswith('image_'):
            lines += ['# 准备真实 PNG 图片；PDF 请使用批量任务页面，不可直接作为 image_url。',
                      'payload["messages"][0]["content"][0]["image_url"]["url"] = "data:image/png;base64," + base64.b64encode(Path("image.png").read_bytes()).decode()']
        elif transport == 'voice_clone':
            lines += ['# reference.wav 应是已获授权的清晰单人语音；ref_text 与录音逐字一致。',
                      'payload["ref_audio"] = "data:audio/wav;base64," + base64.b64encode(Path("reference.wav").read_bytes()).decode()']
        lines += ['data = json.dumps(payload, ensure_ascii=False).encode("utf-8")', 'headers["Content-Type"] = "application/json"']
    lines += ['request = Request(url, data=data, headers=headers, method="POST")', 'try:',
              '    with urlopen(request, timeout=float(os.getenv("TIMEOUT_SECONDS", "630"))) as response:']
    if transport in {'sse', 'image_sse'}:
        lines += ['        if "text/event-stream" not in response.headers.get("Content-Type", ""): raise RuntimeError("未返回 SSE")',
                  '        done = False', '        for raw in response:', '            line = raw.decode("utf-8").strip()',
                  '            if not line.startswith("data:"): continue', '            data_line = line[5:].strip()',
                  '            if data_line == "[DONE]":', '                done = True', '                break',
                  '            event = json.loads(data_line)', '            if "error" in event: raise RuntimeError(event["error"])',
                  '            for choice in event.get("choices", []):',
                  '                print(choice.get("delta", {}).get("content") or "", end="", flush=True)',
                  '        if not done: raise RuntimeError("流提前结束，未收到 [DONE]，请核查后再决定是否重试")', '        print()']
    elif transport == 'wav':
        lines += ['        audio = response.read()', '        if audio[:4] != b"RIFF" or audio[8:12] != b"WAVE": raise RuntimeError("未返回有效 WAV")',
                  '        Path("output.wav").write_bytes(audio)', '        print("已保存 output.wav")']
    else:
        lines += ['        print(json.dumps(json.load(response), ensure_ascii=False, indent=2))']
    lines += ['except HTTPError as error:', '    raise SystemExit("HTTP %s: %s" % (error.code, error.read().decode("utf-8", errors="replace")))']
    return '\n'.join(lines) + '\n'


def shell_example(example, powershell=False, base_url: str = DEFAULT_URL):
    transport = example['transport']
    if powershell:
        lines = ['# PowerShell：使用 curl.exe；API_KEY 从环境变量读取。',
                 'if (-not $env:API_KEY) { throw "请先设置环境变量 API_KEY" }',
                 f'$gateway = if ($env:GATEWAY_URL) {{ $env:GATEWAY_URL.TrimEnd("/") }} else {{ "{base_url}" }}']
        url, auth, cmd = '"$gateway' + example['path'] + '"', '"Authorization: Bearer $env:API_KEY"', 'curl.exe'
    else:
        lines = ['# Bash：先设置 API_KEY，可选 GATEWAY_URL。', ': "${API_KEY:?请先设置 API_KEY}"',
                 f'GATEWAY_URL="${{GATEWAY_URL:-{base_url}}}"']
        url, auth, cmd = '"${GATEWAY_URL%/}' + example['path'] + '"', '"Authorization: Bearer $API_KEY"', 'curl'
    args = ['--fail-with-body --silent --show-error --max-time 630', '-X POST', '-H ' + auth]
    if transport == 'raw_image':
        args += ['-H "Content-Type: image/png"', '--data-binary "@image.png"']
    elif transport == 'raw_document':
        args += ['-H "Content-Type: application/pdf"', '--data-binary "@ballot.pdf"']
    elif transport == 'multipart':
        args += ['-F "model=sensevoice"', '-F "language=auto"', '-F "response_format=verbose_json"', '-F "file=@input.wav;type=audio/wav"']
    else:
        body = json.dumps(example['body'], ensure_ascii=False, indent=2)
        if powershell:
            lines += ["$body = @'", body, "'@"]
            if transport.startswith('image_'):
                lines += ['$image = "data:image/png;base64," + [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $PWD "image.png")))',
                          '$body = $body.Replace("__IMAGE_DATA_URI__", $image)']
            elif transport == 'voice_clone':
                lines += ['$reference = "data:audio/wav;base64," + [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $PWD "reference.wav")))',
                          '$body = $body.Replace("__REFERENCE_AUDIO_DATA_URI__", $reference)']
            lines += ['[IO.File]::WriteAllText((Join-Path $PWD "request.json"), $body, [Text.UTF8Encoding]::new($false))']
        else:
            if transport.startswith('image_'):
                lines += ['IMAGE_BASE64=$(base64 < image.png | tr -d "\\r\\n")', "cat > request.json <<JSON"]
                body = body.replace('__IMAGE_DATA_URI__', 'data:image/png;base64,${IMAGE_BASE64}')
            elif transport == 'voice_clone':
                lines += ['REFERENCE_BASE64=$(base64 < reference.wav | tr -d "\\r\\n")', "cat > request.json <<JSON"]
                body = body.replace('__REFERENCE_AUDIO_DATA_URI__', 'data:audio/wav;base64,${REFERENCE_BASE64}')
            else: lines += ["cat > request.json <<'JSON'"]
            lines += [body, 'JSON']
        args += ['-H "Content-Type: application/json"', '--data-binary "@request.json"']
    if transport in {'sse', 'image_sse'}:
        lines += ['# 输出为 SSE：须检查 error 事件和 [DONE]；自动校验可使用 Python 示例。']; args += ['--no-buffer']
    if transport == 'wav': args += ['--output "output.wav"']
    lines += [cmd + ' ' + ' '.join(args) + ' ' + url]
    if powershell: lines += ['if ($LASTEXITCODE -ne 0) { throw "请求失败，请查看 HTTP 错误；不要盲目重试" }']
    return '\n'.join(lines) + '\n'


def websocket_example(base_url: str = DEFAULT_URL):
    template = '''# Python 3；先安装 websockets==15.0.1，并设置 API_KEY。
import asyncio, json, os, wave
import websockets

async def main():
    base = os.getenv("GATEWAY_URL", "__GATEWAY_URL__").rstrip("/")
    url = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/v1/audio/transcriptions/stream"
    with wave.open("input.wav", "rb") as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getcomptype()) != (16000, 1, 2, "NONE"):
            raise ValueError("需 16 kHz、单声道、16-bit PCM WAV，程序仅发送其中的 PCM 帧")
        async with websockets.connect(url, additional_headers={"Authorization": "Bearer " + os.environ["API_KEY"]}, proxy=None) as ws:
            await ws.send(json.dumps({"type": "start", "model": "sensevoice", "sample_rate": 16000,
                                      "channels": 1, "format": "pcm_s16le", "language": "auto", "use_itn": True}))
            ready = json.loads(await ws.recv())
            if ready.get("type") != "ready": raise RuntimeError(ready)
            async def send_audio():
                while chunk := audio.readframes(1600):
                    await ws.send(chunk)
                    await asyncio.sleep(0.1)
                await ws.send(json.dumps({"type": "stop"}))
            sender = asyncio.create_task(send_audio())
            done = False
            try:
                async for raw in ws:
                    event = json.loads(raw)
                    if event.get("type") == "error": raise RuntimeError(event)
                    print(json.dumps(event, ensure_ascii=False), flush=True)
                    if event.get("type") == "done":
                        done = True
                        break
                if not done: raise RuntimeError("连接提前结束，未收到 done")
                await sender
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)

async def bounded():
    await asyncio.wait_for(main(), timeout=float(os.getenv("TIMEOUT_SECONDS", "3700")))

asyncio.run(bounded())
'''
    return template.replace("__GATEWAY_URL__", base_url)
