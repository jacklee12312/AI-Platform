#!/usr/bin/env python3
"""Build a reproducible acceptance report from local soak evidence; never call a model."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path

KEYS=('A','B','C','D','E')


def read_json(path, limit=20_000_000):
    with path.open(encoding='utf-8') as stream: raw=stream.read(limit+1)
    if len(raw)>limit: raise ValueError('evidence file exceeds read limit: '+path.name)
    return json.loads(raw)


def strict_answer(answer, expected):
    try:
        pairs=json.loads(answer,object_pairs_hook=lambda value:value)
        return (isinstance(expected,dict) and list(expected)==list(KEYS) and isinstance(pairs,list)
                and len(pairs)==5 and all(isinstance(pair,tuple) and len(pair)==2 for pair in pairs)
                and [key for key,value in pairs]==list(KEYS)
                and all(isinstance(value,str) and value==expected[key] for key,value in pairs))
    except (TypeError,ValueError,KeyError): return False


def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>=0


def manifest_check(root):
    path=root/'evidence-manifest.json'
    if not path.exists(): return {'available':False,'checked':0,'errors':['final_manifest_missing']}
    try:
        values=read_json(path)
        if not isinstance(values,list) or len(values)>20000: raise ValueError('invalid manifest')
        seen=set();errors=[]
        for value in values:
            name=value['file']
            if not isinstance(name,str) or name in {'','.', '..'} or Path(name).name!=name or '/' in name or '\\' in name or name in seen:
                raise ValueError('unsafe or duplicate manifest name')
            seen.add(name);file=root/name
            if file.is_symlink() or not file.is_file():errors.append(name+': missing');continue
            with file.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
            if digest!=value['sha256'] or file.stat().st_size!=value['size']:errors.append(name+': mismatch')
        actual={file.name for file in root.iterdir() if file.is_file() and file.name!='evidence-manifest.json'}
        errors.extend(name+': unlisted' for name in sorted(actual-seen))
        if not {'status.json','baseline-versions.json'}.issubset(seen):
            errors.append('required final evidence missing from manifest')
        return {'available':True,'checked':len(values),'errors':errors}
    except (OSError,ValueError,KeyError,TypeError) as exc:
        return {'available':True,'checked':0,'errors':['manifest_invalid: '+str(exc)]}


def percentile(values, fraction):
    return sorted(values)[max(0,math.ceil(len(values)*fraction)-1)] if values else None


def generate(root: Path, output: Path):
    root,output=root.resolve(),output.resolve()
    if output==root or root in output.parents:
        raise ValueError('write reports outside the sealed evidence directory')
    status=read_json(root/'status.json')
    if not isinstance(status,dict):raise ValueError('status.json: invalid object')
    issues=[];rows=[];ids=set();groups={}
    def optional(name, fallback=None):
        path=root/name
        if not path.exists():return fallback
        try:return read_json(path)
        except (OSError,ValueError,TypeError):
            issues.append(name+': unreadable evidence');return fallback
    def optional_object(name, fallback=None):
        value=optional(name,fallback)
        if value is not None and not isinstance(value,dict):
            issues.append(name+': invalid object');return {'kind':'invalid_evidence'}
        return value
    files=sorted(root.glob('request-*.json'))
    if len(files)>10000: raise ValueError('too many request files')
    for path in files:
        if not re.fullmatch(r'request-\d{6}\.json',path.name):continue
        try:
            row=read_json(path,2_000_000)
            if not isinstance(row,dict):raise ValueError('invalid row')
            request_id=row.get('request_id')
            if not request_id or request_id in ids:issues.append(path.name+': missing or duplicate request ID')
            ids.add(request_id)
            correct=row.get('status')==200 and strict_answer(row.get('answer'),row.get('expected'))
            if row.get('correct') is not correct:issues.append(path.name+': saved grade disagrees with strict recheck')
            row={**row,'rechecked_correct':correct};rows.append(row)
            target=row.get('target_tokens')
            if not isinstance(target,int) or isinstance(target,bool) or target<=0:raise ValueError('invalid target length')
            group=groups.setdefault(target,{'requests':0,'http_success':0,'correct':0,'latencies':[],'tokenizer_tokens':[]})
            group['requests']+=1;group['http_success']+=int(row.get('status')==200);group['correct']+=int(correct)
            if finite(row.get('elapsed_seconds')):group['latencies'].append(row['elapsed_seconds'])
            else:issues.append(path.name+': latency missing')
            if finite(row.get('tokenizer_tokens')):group['tokenizer_tokens'].append(row['tokenizer_tokens'])
        except (OSError,ValueError,TypeError,KeyError) as exc:issues.append(path.name+': '+str(exc))
    events=[];final_probe_ns=None
    for path in sorted(root.glob('timeline-*.ndjson')):
        with path.open(encoding='utf-8') as stream:
            while raw := stream.readline(1_000_001):
                if len(raw)>1_000_000:issues.append(path.name+': oversized event');break
                try:
                    event=json.loads(raw)
                    if event.get('kind')=='phase' and event.get('stage')=='final_probe':
                        final_probe_ns=event.get('monotonic_ns')
                    if event.get('kind') in {'first_fault','containment_begin','slow_request','diagnostic_signal','slow_snapshot_error'}:
                        if len(events)<200:events.append(event)
                except (ValueError,AttributeError):issues.append(path.name+': invalid event');break
    start_ns=status.get('start_monotonic_ns');duration=None
    if finite(start_ns) and finite(final_probe_ns) and final_probe_ns>=start_ns:duration=(final_probe_ns-start_ns)/1e9
    planned=status.get('requested_minutes')
    full_duration=bool(finite(planned) and planned>0 and duration is not None and duration>=planned*60
                       and status.get('duration_completed') is True and status.get('state')=='completed')
    counts={'completed':len(rows),'http_success':sum(row.get('status')==200 for row in rows),
            'correct':sum(row['rechecked_correct'] for row in rows)}
    for key,field in [('completed','completed_requests'),('http_success','http_success'),('correct','correct_requests')]:
        if status.get(field)!=counts[key]:issues.append('status count mismatch: '+field)
    targets=status.get('targets') or []
    if not isinstance(targets,list) or not all(isinstance(target,int) and not isinstance(target,bool) and target>0 for target in targets):
        issues.append('status.json: invalid targets');targets=[]
    coverage=all(target in groups for target in set(targets)) and bool(targets)
    final_probe=optional('final-probe.json',{})
    if not isinstance(final_probe,dict):
        issues.append('final-probe.json: invalid object');final_probe={}
    final_ok=final_probe.get('status')==200 and str(final_probe.get('answer','')).strip()=='RECOVERY_OK'
    manifest=manifest_check(root)
    first=optional_object('first-fault.json',status.get('first_fault'))
    npu=optional_object('first-npu-fault.json')
    inflight=status.get('inflight')
    inflight=optional_object('interrupted-request.json',inflight)
    capabilities=status.get('capture_capabilities') or {}
    if not isinstance(capabilities,dict):
        issues.append('status.json: invalid capture capabilities');capabilities={}
    limits=list(capabilities.get('limits') or [])
    if 'npu_telemetry_degraded' in capabilities:limits.append('hm_smi telemetry was degraded')
    if capabilities.get('trace_error'):limits.append('kernel trace unavailable: '+str(capabilities['trace_error']))
    if (root/'trace-cleanup-error.json').exists():issues.append('trace cleanup requires review')
    infrastructure_ok=bool(full_duration and counts['completed']>0 and counts['http_success']==counts['completed']
                           and not first and not npu and not inflight and final_ok)
    correctness_ok=counts['completed']>0 and counts['correct']==counts['completed']
    provenance=optional('launch-provenance.json',{})
    if not isinstance(provenance,dict):
        issues.append('launch-provenance.json: invalid object');provenance={}
    evidence_ok=manifest['available'] and not manifest['errors'] and not issues
    telemetry_ok='npu_telemetry_degraded' not in capabilities and not capabilities.get('trace_error')
    passed=infrastructure_ok and correctness_ok and evidence_ok and coverage and telemetry_ok and status.get('test_key_revoked') is True
    by_target=[]
    for target,group in sorted(groups.items()):
        latency=group.pop('latencies');tokens=group.pop('tokenizer_tokens')
        by_target.append({'target_tokens':target,**group,
          'http_success_rate':group['http_success']/group['requests'],'correct_rate':group['correct']/group['requests'],
          'latency_seconds':{'mean':statistics.mean(latency) if latency else None,'p50':percentile(latency,.5),'p95':percentile(latency,.95),'max':max(latency) if latency else None},
          'actual_token_range':[min(tokens),max(tokens)] if tokens else None})
    categories=[]
    for condition,label in [(npu or (first and first.get('kind')=='npu_fatal'),'NPU 底层故障'),
            (first and first.get('kind')=='monitor_error','监控采集故障'),
            (first and first.get('kind')=='container_lifecycle_change','容器生命周期变化'),
            (counts['http_success']<counts['completed'],'HTTP 请求失败'),
            (counts['correct']<counts['http_success'],'答案不符合严格规则'),
            (inflight,'在途请求结果未知'),(not full_duration,'时长未完成或证据不足'),
            (not evidence_ok,'证据完整性或计数不一致'),(not telemetry_ok,'设备遥测不完整'),
            (status.get('test_key_revoked') is not True,'测试 Key 撤销未确认')]:
        if condition:categories.append(label)
    result={'schema_version':1,'generated_at':datetime.now(timezone.utc).isoformat(),'run_id':status.get('run_id'),
            'source_commit':provenance.get('source_commit'),'evidence_root':str(root),'state':status.get('state'),
            'planned_minutes':planned,'observed_test_seconds':duration,'duration_completed':full_duration,
            'counts':counts,'by_target':by_target,'all_target_lengths_covered':coverage,'final_probe':final_probe,
            'infrastructure_passed':infrastructure_ok,'correctness_passed':correctness_ok,'evidence_passed':evidence_ok,
            'test_key_revoked':status.get('test_key_revoked'),'passed':passed,'inflight':inflight,
            'first_fault':first,'first_npu_fault':npu,'timeline':events,'capture_limits':limits,
            'failure_categories':categories,
            'manifest_verification':manifest,'issues':issues,
            'scope':'synthetic marker retrieval, single client, nonstreaming; not all S09 acceptance scenarios'}
    output.mkdir(parents=True,exist_ok=True)
    (output/'acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (output/'acceptance.md').write_text(markdown(result),encoding='utf-8')
    return result


def markdown(result):
    def yes(value):return '通过' if value else '未通过 / 证据不足'
    def number(value):return '—' if value is None else f'{value:.2f}'
    lines=['# CoPaw 稳定性验收报告','',f"运行：{result['run_id']}；记录状态：{result['state']}",
           f"代码提交：{result['source_commit'] or '证据中未记录'}",'',
           f"**综合结论：{yes(result['passed'])}**",'',
           '异常分类：'+('；'.join(result['failure_categories']) or '无'),'',
           '| 项目 | 结果 |','|---|---|',
           f"| 测试时长 | 计划 {result['planned_minutes']} 分钟；从测试开始到末次探针前 {number(result['observed_test_seconds'])} 秒；{yes(result['duration_completed'])} |",
           f"| 运行稳定性 | {yes(result['infrastructure_passed'])} |",
           f"| 严格答案正确性 | {yes(result['correctness_passed'])} |",
           f"| 所有目标输入长度均覆盖 | {yes(result['all_target_lengths_covered'])} |",
           f"| 证据清单及计数核对 | {yes(result['evidence_passed'])} |",
           f"| 独立测试 Key 已撤销 | {yes(result['test_key_revoked'] is True)} |",'',
           '## 不同输入长度','',
           '| 目标 Token | 请求数 | HTTP 成功 | 严格正确 | 平均秒 | P50 秒 | P95 秒 | 最大秒 |',
           '|---:|---:|---:|---:|---:|---:|---:|---:|']
    for row in result['by_target']:
        lat=row['latency_seconds']
        lines.append(f"| {row['target_tokens']} | {row['requests']} | {row['http_success']} | {row['correct']} | {number(lat['mean'])} | {number(lat['p50'])} | {number(lat['p95'])} | {number(lat['max'])} |")
    lines+=['','## 首个故障与异常时间线','']
    if not result['first_fault'] and not result['first_npu_fault']:lines.append('现有证据中未记录首故障；这一表述不等于所有底层故障均可被探针捕获。')
    else:lines+=['```json',json.dumps({'first_fault':result['first_fault'],'first_npu_fault':result['first_npu_fault']},ensure_ascii=False,indent=2),'```']
    if result['inflight']:lines+=['','在途请求终态未知：','```json',json.dumps(result['inflight'],ensure_ascii=False,indent=2),'```']
    for event in result['timeline']:
        lines.append(f"- {event.get('at','—')} · {event.get('kind')} · {event.get('request_id') or event.get('source') or '见原始时间线'}")
    lines+=['','## 证据与验收边界','',
            f"清单核验 {result['manifest_verification']['checked']} 个文件；报告在原始证据目录外生成。SHA-256 校验用于一致性核对，不构成第三方真实性认证。",'',
            '本轮是单客户端、非流式、合成标记检索测试。并发、业务数据质量、开机加载中断、故障恢复、其他模型长稳等 S09 场景仍需单独验收。',
            '没有末次探针时间线时，不以包含收尾时间的 elapsed_seconds 推断已跑满。没有完整原始文件时，不把摘要副本当作证据核验通过。','']
    lines.extend('- '+str(value) for value in [*result['capture_limits'],*result['issues'],*result['manifest_verification']['errors']])
    return '\n'.join(lines)+'\n'


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir',required=True,type=Path);parser.add_argument('--output-dir',required=True,type=Path)
    args=parser.parse_args();result=generate(args.run_dir,args.output_dir)
    print(json.dumps({key:result[key] for key in ('run_id','state','passed','counts','duration_completed')},ensure_ascii=False))
