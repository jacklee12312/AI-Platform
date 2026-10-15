"""Bounded, versioned fixed samples; these are not general quality certification.

``request`` is private execution data. Never persist/return it in an evaluation
report: multimodal requests contain the actual image. ``sample`` is safe to show.
"""
from __future__ import annotations
import base64
import hashlib
import json
from .model_store import Catalog
from .runtime_probes import digit_image
VERSION = '2026-09-08.1'
SUITES = {'smoke': '快速自检', 'quality': '固定样例评测', 'repeat': '重复时延采样'}
MAX_CASES = 120
NOTICE = '仅固定样例评测，不代表通用质量认证；无 LLM 裁判、长上下文或压力测试。'

def input_hash(request):
    """Hash actual JSON/PNG inputs, or the pinned audio hash and size."""
    return hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

def _case(model, key, name, scope, checker, expected, sample, request, parameters=None, reason=None):
    row = {'id': model['id'] + ':' + key, 'model': model['id'], 'name': name, 'scope': scope, 'status': 'skipped' if reason else 'pending', 'input_sha256': input_hash(request), 'expected': expected, 'parameters': parameters or {}, 'sample': sample, 'checker': checker, 'request': request}
    if reason:
        row['reason'] = reason
    return row

def _requests(model, quality):
    mid, capability = (model['id'], model['capability'])
    scope = 'quality' if quality else 'execution'

    def case(key, name, checker, expected, sample, request, parameters=None, reason=None):
        return _case(model, key, name, scope, checker, expected, sample, request, parameters, reason)

    def post(path, body):
        return {'method': 'POST', 'path': path, 'json': {'model': mid, **body}}
    if quality and (capability.startswith('cv_') or capability in {'audio_asr', 'audio_tts'}):
        return [case('quality-unsupported', '质量数据集未提供', 'unsupported', '需要带标注数据集', '未提供带标注的固定质量样本', {}, reason='unsupported: 未提供 CV/ASR/TTS 带标注质量数据集')]
    if capability == 'chat':
        image_model = mid in {'qwen3-vl-8b', 'glm-ocr-0.9b'}
        content = '计算 3+4，只回答数字。'
        sample, key, name, expected = (content, 'digit7', '固定数字 7', '7')
        if image_model:
            image = digit_image()
            png = base64.b64decode(image.split(',', 1)[1])
            text = 'Text Recognition:' if mid == 'glm-ocr-0.9b' else '只输出图片里的数字。'
            content = [{'type': 'image_url', 'image_url': {'url': image}}, {'type': 'text', 'text': text}]
            sample = '固定黑字白底数字 7；PNG 96×96；sha256=' + hashlib.sha256(png).hexdigest()
        elif quality:
            content = sample = '计算 12*3-5，只回答最终整数，不要解释。'
            key, name, expected = ('arithmetic', '固定算术精确匹配', '31')
        parameters = {'temperature': 0, 'max_tokens': 64, 'stream': False}

        def chat(value):
            return post('/v1/chat/completions', {'messages': [{'role': 'user', 'content': value}], **parameters})
        rows = [case(key, name, 'exact_text', expected, sample, chat(content), parameters)]
        if quality and (not image_model):
            prompt = '计算 3+4。只输出一个 JSON 对象，唯一字段 answer 的值为整数答案；不要 Markdown 或解释。'
            rows.append(case('strict-json', '严格 JSON 字段匹配', 'strict_json', {'answer': 7}, prompt, chat(prompt), parameters))
        return rows
    if capability == 'embeddings':
        texts = ['北京是中国的首都。', '中国的首都是北京。', '苹果蛋糕在烤箱里烘焙。'] if quality else ['模型部署验证']
        return [case('semantic' if quality else 'embedding', '固定语义相似度' if quality else '向量执行校验', 'embedding_semantic' if quality else 'embedding', '3×1024 有限非零向量；相近文本余弦相似度 > 无关文本' if quality else '1024 维有限非零向量', texts, post('/v1/embeddings', {'input': texts}))]
    if capability == 'rerank':
        body = {'query': '中国首都', 'documents': ['北京是中国首都。', '苹果是一种水果。'], 'top_n': 2}
        return [case('rerank', '固定相关文档排序' if quality else '重排执行校验', 'rerank_related' if quality else 'rerank', '两项有限分数，索引唯一且为 0/1' + ('；相关文档 0 排第一且分数严格更高' if quality else ''), body, post('/v1/rerank', body), {'top_n': 2})]
    if capability.startswith('cv_'):
        image = digit_image()
        raw = base64.b64decode(image.split(',', 1)[1])
        return [case('cv-png', '固定 PNG 执行校验', 'cv_outputs', 'ok=true、模型 ID 匹配且包含 runtime_outputs', '固定数字 7 PNG 96×96；sha256=' + hashlib.sha256(raw).hexdigest(), {'method': 'POST', 'path': '/v1/cv/' + capability[3:] + '?model=' + mid, 'image': image, 'content_type': 'image/png'})]
    if capability == 'audio_tts':
        text = '你好，模型部署验证完成。'
        return [case('tts-wav', '固定语音合成执行校验', 'pcm16_wav', '完整、非空、非静音 PCM16 WAV', text, post('/v1/audio/speech', {'input': text, 'response_format': 'wav'}), {'response_format': 'wav'})]
    if capability == 'audio_asr':
        pinned = next((f for f in model['files'] if f['target'].endswith('test_wavs/zh.wav')))
        audio = {k: pinned[k] for k in ('target', 'size', 'sha256')}
        return [case('asr-pinned-zh', '固定中文语音执行校验', 'asr_text', '非空转写文本（不评估准确率）', '已有 zh.wav；bytes=' + str(audio['size']) + '；sha256=' + audio['sha256'], {'method': 'POST', 'path': '/v1/audio/transcriptions', 'model': mid, 'audio': audio})]
    return [case('unsupported', '不支持的能力', 'unsupported', '', '', {}, reason='unsupported: 暂无固定样例')]

def make_cases(models: list[str], suite: str, repeats: int=1) -> list[dict]:
    if not isinstance(models, list) or any((not isinstance(mid, str) for mid in models)):
        raise ValueError('models 必须是模型 ID 列表')
    if not isinstance(suite, str) or suite not in SUITES:
        raise ValueError('未知评测套件')
    if type(repeats) is not int or not 1 <= repeats <= (3 if suite == 'repeat' else 1):
        raise ValueError('smoke/quality 只能执行一次；repeat 次数必须为 1..3')
    selected = Catalog().select(models)
    rows = []
    for model in selected:
        for index in range(repeats):
            for row in _requests(model, suite == 'quality'):
                row['id'] = suite + ':' + row['id'] + ':' + str(index + 1)
                rows.append(row)
    if len(rows) > MAX_CASES:
        raise ValueError('单次评测最多 120 个样例')
    return rows
