"""Small fixed inference probes. This verifies execution, not domain quality."""
import base64
import json
import math
import re
import struct
import wave
import zlib
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen


def digit_image():
    # A fixed black 7 on white, encoded with stdlib so the agent needs no decoder.
    rows=[]
    for y in range(96):
        pixels=bytearray()
        for x in range(96):
            ink=(18 <= y < 27 and 22 <= x < 73) or (27 <= y < 79 and 68-(y-27)//2 <= x < 77-(y-27)//2)
            pixels.extend([0 if ink else 255]*3)
        rows.append(b'\0'+pixels)
    def chunk(kind, data):
        return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data))
    png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',96,96,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b''.join(rows)))+chunk(b'IEND',b'')
    return 'data:image/png;base64,'+base64.b64encode(png).decode()


def specification(name):
    if name=='houmo-cosyvoice3':
        reference=Path('/home/cix/cix_telecom_projects/sensevoice/model/test_wavs/zh.wav').read_bytes()
        return 8091,'/v1/audio/speech',{
            'model':'cosyvoice3','input':'这是 ModelDeck 切换模式后的语音合成验收。',
            'ref_audio':'data:audio/wav;base64,'+base64.b64encode(reference).decode(),
            'ref_text':'开饭时间早上9点至下午5点。','response_format':'wav'
        },'wav'
    if name=='houmo-bge-m3':
        return 17702,'/v1/embeddings',{'model':'bge-m3','input':['北京是中国的首都。']},'embedding'
    if name=='houmo-reranker':
        return 17703,'/v1/rerank',{'model':'bge-reranker-v2-m3','query':'中国首都','documents':['北京是中国首都。','苹果是一种水果。'],'top_n':2},'rerank'
    if name=='houmo-qwen38-27b':
        model,port='qwen3.8-27b',17707
        baseline=json.loads(Path(__file__).with_name('runtime_baseline.json').read_text(encoding='utf-8'))
        variant=baseline.get('models',{}).get(model,{}).get('hardware_variant')
        if variant in ('single','dual'):
            content=[{'type':'image_url','image_url':{'url':digit_image()}},{'type':'text','text':'Read the single digit shown in the image. Reply with only that digit.'}]
            kind='digit7'
        else:raise RuntimeError('Qwen3.8 设备配置缺少硬件版本')
    elif name=='houmo-qwen36-35b-a3b':
        model,port='qwen3.6-35b-a3b',17712
        content=[{'type':'image_url','image_url':{'url':digit_image()}},{'type':'text','text':'Read the single digit shown in the image. Reply with only that digit.'}]
    elif name=='houmo-qwen3-vl-8b':
        model,port,content='qwen3-vl-8b',17705,'计算 3+4，只回答数字。'
    elif name=='houmo-glm-ocr-0.9b':
        model,port='glm-ocr-0.9b',17709
        content=[{'type':'image_url','image_url':{'url':digit_image()}},{'type':'text','text':'Text Recognition:'}]
    else: raise ValueError('no fixed inference probe for '+name)
    return port,'/v1/chat/completions',{'model':model,'messages':[{'role':'user','content':content}],
                                     'temperature':0,'max_tokens':64,'stream':False},kind if name=='houmo-qwen38-27b' else 'digit7'


def validate(body, kind):
    def finite(value): return isinstance(value,(float,int)) and not isinstance(value,bool) and math.isfinite(value)
    try:
        if kind=='embedding':
            vector=body['data'][0]['embedding']
            return isinstance(vector,list) and len(vector)==1024 and all(map(finite,vector)) and any(v!=0 for v in vector)
        if kind=='rerank':
            rows=body['results']
            return (len(rows)==2 and sorted(row['index'] for row in rows)==[0,1]
                    and all(finite(row['relevance_score']) for row in rows))
        if kind in ('digit7','text7'): return re.findall(r'\d+',body['choices'][0]['message']['content'])==['7']
    except (KeyError,TypeError,IndexError,ValueError): pass
    return False


def execute(name, address, key):
    port,path,payload,kind=specification(name)
    request=Request(f'http://{address}:{port}{path}',data=json.dumps(payload).encode(),
                    headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    with urlopen(request,timeout=40) as response:
        body=response.read(8_000_001 if kind=='wav' else 2_000_001)
        if kind=='wav':
            if len(body)>8_000_000: raise RuntimeError('语音验证响应超过上限')
            try:
                with wave.open(BytesIO(body),'rb') as audio:
                    frames=audio.readframes(audio.getnframes())
                    valid=(audio.getnchannels()==1 and audio.getsampwidth()==2 and audio.getframerate()==24000
                           and audio.getnframes()>=12000 and any(frames))
                    duration=audio.getnframes()/audio.getframerate()
            except (wave.Error,EOFError):
                valid=False; duration=0
            if not valid: raise RuntimeError(name+' 未返回有效的非静音 24kHz 单声道 WAV')
            return {'container':name,'test':'wav','passed':True,'duration_seconds':round(duration,3),
                    'bytes':len(body),'scope':'execution_liveness_not_quality_acceptance'}
        if len(body)>2_000_000: raise RuntimeError('验证响应超过上限')
        body=json.loads(body)
    if not validate(body,kind): raise RuntimeError(name+' 实际推理验证未通过')
    return {'container':name,'test':kind,'passed':True,'scope':'execution_liveness_not_quality_acceptance'}
