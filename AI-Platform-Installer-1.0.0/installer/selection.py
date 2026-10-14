"""Derive an install plan from the signed, immutable release catalog."""
from __future__ import annotations

from copy import deepcopy

ACCELERATOR={'copaw-flash-9b','spark-x1-4b','bge-m3','bge-reranker-v2-m3',
             'cosyvoice3-0.5b','qwen3-vl-8b','glm-ocr-0.9b','qwen3.8-27b','qwen3.6-35b-a3b'}


def supported_on(catalog, model, device_count):
    counts = catalog.get('model_device_counts', {}).get(model, [1, 2])
    if device_count is None:return set(counts)=={1,2}
    return device_count in counts


def validate_selected(catalog, selected):
    available = catalog['models']
    if not isinstance(selected, list) or not selected or len(selected) != len(set(selected)):
        raise ValueError('请至少勾选一个模型，且模型不能重复。')
    if any(not isinstance(value, str) or value not in available for value in selected):
        raise ValueError('勾选项不属于本次发行的模型清单。')
    if not ACCELERATOR.intersection(selected):
        raise ValueError('当前平台开机服务至少需要一个 LQ50 主模型；请至少勾选一个加速卡模型。')
    return [value for value in available if value in selected]


def selected_catalog(catalog, selected, device_count):
    """Keep signed parent intact; model file rows alone are optional.

    Platform, Docker, runtime and the pinned all-images archive remain mandatory.
    """
    chosen = set(validate_selected(catalog, selected))
    if type(device_count) is not int or device_count not in (1,2):raise ValueError('无法确定设备是单卡还是双卡。')
    unsupported = sorted(model for model in chosen if not supported_on(catalog, model, device_count))
    if unsupported:raise ValueError('当前 '+str(device_count)+' 卡设备不支持所选型号：'+', '.join(unsupported))
    result = deepcopy(catalog)
    result['models'] = [value for value in catalog['models'] if value in chosen]
    result['components'] = [row for row in catalog['components']
                            if row['kind'] != 'model' or row['id'] in chosen]
    result['resources'] = [row for row in catalog['resources']
                           if (row['category'] != 'models' or chosen.intersection(row['models'])
                               or ('cosyvoice3-0.5b' in chosen and row['path'].endswith('/sensevoice/model/test_wavs/zh.wav')))
                           and (not row.get('device_count') or row['device_count']==device_count)]
    for row in result['resources']:
        row['selected_device_count'] = device_count
    for component in result['components']:
        if component.get('kind')=='model' and 'files' in component:
            paths={row['path'] for row in result['resources']}
            component['files']=[path for path in component['files'] if path in paths]
    return result
