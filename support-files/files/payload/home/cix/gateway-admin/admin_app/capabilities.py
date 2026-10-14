"""Evidence-backed integration contracts, independent of health probe results."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from .capability_examples import DEFAULT_URL, examples_for

CATALOG_PATH = Path(__file__).with_name('capability_catalog.json')
LIMIT_DEFAULTS = {
    'max_body_mb': (25, 'JSON / CV 请求体', 'MB'),
    'audio_max_body_mb': (100, 'ASR multipart 整个请求体', 'MB'),
    'chat_timeout_seconds': (300, '聊天 / 向量 / 重排网关超时', '秒'),
    'audio_timeout_seconds': (600, '音频网关超时', '秒'),
    'asr_max_file_mb': (100, 'ASR 单文件', 'MB'),
    'asr_max_audio_seconds': (3600, 'ASR 单文件音频时长', '秒'),
    'asr_stream_max_seconds': (3600, 'ASR WebSocket 音频时长', '秒'),
}


def load_catalog():
    return json.loads(CATALOG_PATH.read_text(encoding='utf-8'))


def baseline_differences(expected, actual):
    differences = []
    for field in ('capability', 'backend'):
        if expected[field] != actual.get(field): differences.append(field)
    if sorted(expected['modes']) != sorted(actual.get('modes') or []): differences.append('modes')
    metadata = actual.get('metadata') or {}
    for field in ('model_version', 'service_version', 'type'):
        # Older registries omit versions. Report that gap separately from a known mismatch.
        if field != 'type' and not metadata.get(field): continue
        if expected['metadata'].get(field) != metadata.get(field): differences.append(field)
    return differences


def build_catalog(models, configuration=None, *, registry_error=False, config_error=False,
                  base_url=DEFAULT_URL):
    catalog = load_catalog()
    live = {m['id']: m for m in (models or {}).get('data', [])}
    settings = (configuration or {}).get('settings', {})
    limits = []
    for key, (default, label, unit) in LIMIT_DEFAULTS.items():
        saved = settings.get(key)
        valid = isinstance(saved, (int, float)) and not isinstance(saved, bool) and saved > 0
        limits.append({'id': key, 'label': label, 'value': saved if valid else default, 'unit': unit,
                       'source': 'saved_config' if valid else 'source_default'})
    entries = []
    for record in catalog['models']:
        item = copy.deepcopy(record)
        current = live.get(item['id'])
        registered = None if registry_error else current is not None
        drift = baseline_differences(item['baseline'], current) if current and item.get('baseline') else []
        unreported = [field for field in ('model_version', 'service_version') if not (current.get('metadata') or {}).get(field)] if current and item.get('baseline') else []
        item.update(registered=registered, runtime_status=current.get('status', 'unknown') if current else 'unknown' if registry_error else 'not_registered',
                    active_mode=current.get('active_mode') if current else None,
                    modes=current.get('modes', []) if current else (item.get('baseline') or {}).get('modes', []),
                    baseline_match=bool(current and item.get('baseline') and not drift and not unreported), differences=drift, unreported_versions=unreported)
        # A changed version never inherits a previous version's verification.
        item['evidence_applicability'] = 'version_changed' if drift else 'version_unreported' if unreported else 'baseline_matches' if item['baseline_match'] else 'not_confirmed'
        item['examples'] = examples_for(item['id'], item['profile'], base_url) if current and item['implementation'] != 'unavailable' and not drift else []
        entries.append(item)
    known = {item['id'] for item in entries}
    for identity, current in live.items():
        if identity not in known:
            entries.append({'id': identity, 'name': identity, 'category': '待说明', 'profile': 'unknown',
                'implementation': 'unknown', 'summary': '已在网关注册，尚未维护输入、输出和验收说明。',
                'registered': True, 'runtime_status': current.get('status', 'unknown'), 'active_mode': current.get('active_mode'),
                'modes': current.get('modes') or [], 'baseline_match': False, 'differences': [], 'unreported_versions': [], 'evidence_applicability': 'not_confirmed',
                'inputs': [], 'outputs': [], 'streaming': [], 'boundaries': ['没有能力说明和验收依据，不应仅凭注册或在线状态作为交付依据。'],
                'verification': {'status': 'pending', 'scope': '待维护和验证', 'sources': []}, 'examples': []})
    return {'schema_version': 1, 'reviewed_at': catalog['reviewed_at'],
            'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'registry_available': not registry_error, 'config_available': not config_error,
            'notice': '在线仅表示网关健康探测结果；样例通过、质量验收和正式交付签认分别记录，不自动互相升级。',
            'config_notice': '限制来自管理代理保存的配置，非容器实时环境；缺失项明确使用源码默认值。配置应用和模型实际边界需分别核对。',
            'base_url': base_url, 'limits': limits, 'sources': catalog['sources'], 'models': entries}


def export_markdown(catalog):
    implementations = {'implemented': '有接口实现', 'partial': '部分实现', 'unavailable': '未交付', 'unknown': '说明待补齐'}
    verifications = {'sample_verified': '指定样例通过', 'historical': '历史验证记录', 'issues': '已知问题待闭环', 'pending': '待专项验收', 'blocked': '适配受阻'}
    applicability = {'version_changed': '版本或协议变化，已暂停示例', 'version_unreported': '版本未上报，历史验收适用性未确认', 'baseline_matches': '登记基线匹配，仍需核对实际制品', 'not_confirmed': '未确认'}
    lines = ['# 模型能力与接入说明', '', f"说明核对日期：{catalog['reviewed_at']}；状态读取时间：{catalog['checked_at']}", '',
             catalog['notice'], '', '此文件是当前能力与证据快照，不是正式交付验收证书。', '',
             f'业务入口：{catalog["base_url"]}；以环境变量 GATEWAY_URL 修改。所有示例从 API_KEY 读取客户 Key。', '',
             '## 请求限制', '', catalog['config_notice'], '']
    for limit in catalog['limits']:
        source = '保存配置' if limit['source'] == 'saved_config' else '源码默认，未确认运行值'
        lines.append(f"- {limit['label']}：{limit['value']} {limit['unit']}（{source}）")
    for item in catalog['models']:
        lines += ['', f"## {item['id']} · {item['name']}", '', item['summary'], '',
                  f"运行状态：{item['runtime_status']}；模式：{', '.join(item['modes']) or '注册表无模式限制，仍依赖服务健康'}。",
                  f"实现范围：{implementations[item['implementation']]}；验收记录：{verifications[item['verification']['status']]}；版本适用性：{applicability[item['evidence_applicability']]}。", '',
                  f"验收覆盖：{item['verification']['scope']}", '', '输入限制：']
        lines += ['- ' + value for value in item['inputs']]
        lines += ['', '实际输出：'] + ['- ' + value for value in item['outputs']]
        lines += ['', '流式形式：'] + ['- ' + value for value in item['streaming']]
        lines += ['', '能力边界：'] + ['- ' + value for value in item['boundaries']]
        for source_id in item['verification']['sources']:
            source = catalog['sources'][source_id]
            lines += ['', f"依据：{source['title']}（{source['date']}），{source['path']}，SHA-256 {source['sha256']}", source['summary']]
        for example in item['examples']:
            lines += ['', '### ' + example['title'], '', example['method'] + ' ' + example['path'], '', example['note']]
            for language, snippet in example['snippets'].items():
                lines += ['', '#### ' + {'python': 'Python', 'bash': 'curl · Bash', 'powershell': 'PowerShell · curl.exe'}[language], '',
                          '```' + language, snippet.rstrip(), '```']
    return '\n'.join(lines) + '\n'
