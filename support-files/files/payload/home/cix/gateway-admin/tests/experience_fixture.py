"""Explicit local UI fixtures, never installed on a real device."""
import asyncio
import io
import json
import math
import struct
import wave
from urllib.parse import parse_qs, urlsplit


def audio_sample():
    target = io.BytesIO()
    with wave.open(target, 'wb') as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
        output.writeframes(b''.join(struct.pack('<h', int(1500 * math.sin(i * math.tau * 440 / 16000))) for i in range(32000)))
    return target.getvalue()


async def media(request, path, **kwargs):
    await asyncio.sleep(.2)
    route = urlsplit(path)
    if route.path.startswith('/v1/cv/'):
        from PIL import Image
        with Image.open(io.BytesIO(kwargs['content'])) as image: width, height = image.size
        model = parse_qs(route.query)['model'][0]
        result = {'detections': [{'box': [width*.1, height*.2, width*.6, height*.8], 'class_id': 0, 'score': .93},
                                 {'box': [width*.7, height*.1, width*.9, height*.4], 'class_id': 2, 'score': .42}], 'count': 2}
        return 200, 'application/json', json.dumps({'ok': True, 'model': model, 'original_size': [width, height], 'result': result, 'fixture': True}).encode()
    if route.path == '/v1/audio/transcriptions':
        return 200, 'application/json', json.dumps({'text': '本地测试转写。第二句话。', 'duration': 2,
            'segments': [{'start': 0, 'end': 1, 'text': '本地测试转写。'}, {'start': 1, 'end': 2, 'text': '第二句话。'}], 'fixture': True}).encode()
    return 200, 'audio/wav', audio_sample()


def chat(payload):
    return {'choices': [{'message': {'content': '7\nMODELDECK' if isinstance(payload['messages'][0]['content'], list) else '1 2 3 4 5'}}]}


async def streaming(request, payload):
    from fastapi.responses import StreamingResponse
    async def chunks():
        for text in ['1 ', '2 ', '3 ', '4 ', '5']:
            await asyncio.sleep(.1)
            yield 'data: ' + json.dumps({'choices': [{'delta': {'content': text}}]}) + '\n\n'
        yield 'data: [DONE]\n\n'
    return StreamingResponse(chunks(), media_type='text/event-stream')
