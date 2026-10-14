#!/usr/bin/env python3
"""CoPaw soak with continuous host evidence; no startup/model configuration changes."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import importlib.util
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from npu_soak_monitor import (DEVICE, FATAL, Monitor, atomic_json, command, inspect_containers, journal_timestamp,
                              observed_status, read_text, safe_container, stamp)

DEPLOY = Path(os.environ.get('HOUMO_DEPLOY_DIR', '/home/cix/houmo-docker-deployment'))
MODEL = 'copaw-flash-9b'
GATEWAY_HOST = '192.168.133.1'


def existing_env() -> dict:
    # Read credentials in-process, never expose them through argv or evidence.
    result = {}
    for file in (DEPLOY/'.env', Path('/home/cix/ai-model-gateway/.env')):
        if file.exists():
            for line in file.read_text().splitlines():
                if '=' in line and not line.lstrip().startswith('#'):
                    key, value = line.split('=', 1)
                    if key in {'HOUMO_API_KEY', 'GATEWAY_MANAGEMENT_TOKEN'}:
                        result[key] = value.strip().strip('\"\'')
    item = inspect_containers(['ai-model-gateway'])[0]
    for entry in item['Config'].get('Env', []):
        key, _, value = entry.partition('=')
        if key in {'HOUMO_API_KEY', 'GATEWAY_MANAGEMENT_TOKEN'}:
            result[key] = value
    if not result.get('HOUMO_API_KEY') or not result.get('GATEWAY_MANAGEMENT_TOKEN'):
        raise RuntimeError('gateway/runtime credentials are unavailable')
    return result


class RequestAborted(RuntimeError):
    pass


def request(host: str, port: int, path: str, payload, key: str, *, method='POST',
            timeout=330, monitor: Monitor | None = None, request_id: str | None = None,
            max_body=4_000_000, slow_after: float | None = None) -> tuple[int, dict, float]:
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    outcome = {}; complete = threading.Event(); begin = time.monotonic()
    def work():
        try:
            headers = {'Content-Type': 'application/json'}
            if path.startswith('/internal/'):
                headers['X-Gateway-Admin-Token'] = key
            else:
                headers['Authorization'] = 'Bearer ' + key
            if request_id:
                headers['X-Request-ID'] = request_id
            raw = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
            connection.request(method, path, body=raw, headers=headers)
            response = connection.getresponse()
            body = response.read(max_body + 1)
            if len(body) > max_body:
                raise RuntimeError('HTTP response exceeded capture limit')
            outcome['result'] = response.status, json.loads(body), time.monotonic() - begin
        except Exception as exc:
            outcome['error'] = exc
        finally:
            connection.close(); complete.set()
    thread = threading.Thread(target=work, daemon=True)
    if monitor and monitor.abort.is_set():
        raise RequestAborted('monitor aborted before request submission')
    thread.start()
    slow_captured = False
    while not complete.wait(.1):
        if monitor and monitor.abort.is_set():
            # Closing the client does not promise cancellation of NPU execution.
            # Confirmed NPU failures are subsequently contained by stopping models.
            connection.close()
            raise RequestAborted('monitor stopped in-flight request; upstream result is unknown')
        if time.monotonic() - begin > timeout + 2:
            connection.close()
            raise TimeoutError('request exceeded its wall-clock deadline')
        if monitor and slow_after and not slow_captured and time.monotonic()-begin > slow_after:
            slow_captured = True
            monitor.event('slow_request', request_id=request_id, elapsed_seconds=time.monotonic()-begin)
            def capture_slow():
                try:
                    monitor.snapshot('slow-'+(request_id or 'request'),stacks=True)
                except Exception as exc:
                    monitor.event('slow_snapshot_error',error=str(exc))
            capture_thread=threading.Thread(target=capture_slow,daemon=True)
            capture_thread.start();monitor.threads.append(capture_thread)
    if 'error' in outcome:
        raise outcome['error']
    return outcome['result']


def load_runner():
    os.environ['HOUMO_DEPLOY_DIR'] = str(DEPLOY)
    spec = importlib.util.spec_from_file_location('soak_context', DEPLOY/'qwen38-long-context-test.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module; spec.loader.exec_module(module)
    module.MODEL_ID, module.CONTAINER, module.MODEL_PORT = MODEL, 'houmo-copaw', 17701
    return module


def idle_preflight(management: str) -> dict:
    status, resource, _ = request(GATEWAY_HOST, 17700, '/internal/resource-policy', None,
                                  management, method='GET', timeout=10)
    if status != 200:
        raise RuntimeError('resource preflight failed')
    if resource.get('in_flight', {}).get('global') != 0:
        raise RuntimeError('gateway has active requests or its in-flight state is unavailable')
    # Preserve the exact snapshot; supported schema is checked by the live preflight.
    for file, sql in (
        ('batch-jobs.db', "SELECT id,status FROM batch_jobs WHERE status IN ('queued','running','waiting','cancelling')"),
        ('modeldeck.db', "SELECT id,status FROM audit WHERE status IN ('accepted','queued','running')"),
    ):
        path = Path('/var/lib/docker/volumes/gateway-admin_admin-data/_data')/file
        with sqlite3.connect('file:'+str(path)+'?mode=ro', uri=True) as database:
            if database.execute(sql).fetchall():
                raise RuntimeError('batch or management jobs are active')
    return resource


def result_markdown(report: dict) -> str:
    state = observed_status(report)
    lines = ['# CoPaw 四小时监控测试', '',
             f"- 状态：{state.get('state')}；原因：{state.get('reason', '—')}",
             f"- 测试开始：{state.get('started_at')}；预计结束：{state.get('expected_finish_at')}",
             f"- 最后心跳：{state.get('heartbeat_at')}；结束：{state.get('finished_at', '尚未结束')}",
             f"- 计划 {state.get('requested_minutes')} 分钟；已执行 {state.get('elapsed_seconds', 0):.1f} 秒",
             f"- 实际槽位/每槽上下文：{state.get('slots')} / {state.get('context_size')}",
             f"- 输入轮转：{state.get('targets')}；客户端并发 1；非流式；temperature=0；输出上限 160 Token",
             f"- 已完成请求 {state.get('completed_requests', 0)}；HTTP 成功 {state.get('http_success', 0)}；严格正确 {state.get('correct_requests', 0)}",
             f"- 完整时长：{state.get('duration_completed', False)}；综合通过：{state.get('passed', False)}",
             f"- 当前请求：{json.dumps(state.get('inflight'), ensure_ascii=False)}", '',
             '日志与请求使用 UTC 和主机单调时钟，保存 boot ID、请求 ID、seed、实际分词数及响应。',
             '温度/功耗等 hm_smi 每 15 秒查询一次，主机与进程资源约每 2 秒采样。',
             '内核未开启 kprobe，无法直接观察 DMA 通道 STOP 完成、传输代次与旧 IRQ；只能结合已有驱动日志缩小原因。',
             '本轮保持原运行配置，没有测试开机加载被中断的触发条件。其他模型保持驻留，外部请求可能影响测量，相关日志一并留存。',
             '严格正确率仅针对合成标记检索；本轮不是所有 S09 场景或业务质量的完整验收。', '']
    if state.get('first_fault'):
        lines.extend(['## 首个故障', '', '```json', json.dumps(state['first_fault'], ensure_ascii=False, indent=2), '```', ''])
    if state.get('containment'):
        lines.extend(['## 故障处置', '', '已记录故障现场及停止同卡模型的结果。没有自动复位、重启或改变模型配置。', ''])
    return '\n'.join(lines)


def run(args) -> int:
    if os.name != 'posix' or os.geteuid() != 0:
        raise RuntimeError('run on the ARM64 host as root for kernel/procfs evidence')
    import fcntl
    run_id = args.run_id or datetime.now().strftime('%Y%m%dT%H%M%S')
    if not run_id.replace('-', '').replace('_', '').isalnum() or len(run_id) > 60:
        raise ValueError('invalid run ID')
    root = DEPLOY/'state/monitored-soak'/run_id
    with contextlib.ExitStack() as locks:
        for name in ('long-context-test.lock', 'modelctl.lock'):
            file = locks.enter_context((DEPLOY/'state'/name).open('a'))
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if read_text(DEPLOY/'state/active-mode') != 'copaw':
            raise RuntimeError('CoPaw mode is inactive; this runner never switches modes')
        names = command(['docker', 'ps', '-a', '--format', '{{.Names}}'])['out'].splitlines()
        items = inspect_containers([name for name in (*DEVICE, 'ai-model-gateway') if name in names])
        copaw = next(item for item in items if item['Name'] == '/houmo-copaw')
        if not copaw['State']['Running'] or copaw['State'].get('Health', {}).get('Status') != 'healthy':
            raise RuntimeError('CoPaw is not healthy; no automatic recovery will be attempted')
        env = existing_env()
        credentials = list(env.values())
        # Commands of other model services may carry a different runtime key.
        for item in items:
            cmd = item['Config'].get('Cmd', [])
            credentials.extend(cmd[i+1] for i, value in enumerate(cmd[:-1]) if value == '--api-key')
        monitor = Monitor(root, items, credentials)
        report = {'schema_version': 1, 'run_id': run_id, 'state': 'preparing', 'pid': os.getpid(),
                  'boot_id': monitor.boot_id, 'requested_minutes': args.minutes, 'targets': args.targets,
                  'completed_requests': 0, 'http_success': 0, 'correct_requests': 0, 'inflight': None,
                  'duration_completed': False, 'passed': False, 'evidence_root': str(root)}
        report_lock = threading.RLock(); heartbeat_stop = threading.Event(); key_id = None
        old_handlers = {sig: signal.signal(sig, lambda *_: monitor.fail('cancelled', 'signal', 'runner received termination signal'))
                        for sig in (signal.SIGTERM, signal.SIGINT)}

        def checkpoint():
            with report_lock:
                report.update(heartbeat_at=stamp()['at'], heartbeat_epoch=time.time(),
                              first_fault=monitor.first_fault, capture_capabilities=monitor.capabilities)
                if report.get('start_monotonic_ns'):
                    report['elapsed_seconds'] = (time.monotonic_ns()-report['start_monotonic_ns']) / 1e9
                monitor.save('status.json', report)
                (root/'report.md').write_text(result_markdown(report), encoding='utf-8')

        def heartbeat():
            while not heartbeat_stop.wait(5):
                try:
                    checkpoint()
                except Exception as exc:
                    monitor.fail('monitor_error', 'heartbeat', str(exc))

        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        started = None

        def contain_once():
            if monitor.hardware_fault and 'containment' not in report:
                try:
                    report['containment'] = monitor.contain_npu_fault()
                except Exception as stop_error:
                    report['containment'] = {'error': monitor.clean(str(stop_error)), 'manual_intervention_required': True}

        try:
            checkpoint(); monitor.start(); heartbeat_thread.start()
            if monitor.abort.is_set():
                raise RequestAborted('monitor startup failed')
            report['resources_at_start'] = idle_preflight(env['GATEWAY_MANAGEMENT_TOKEN'])
            kernel = command(['journalctl', '-k', '-b', '--since', journal_timestamp(copaw['State']['StartedAt']),
                              '--no-pager', '-o', 'json', '--grep',
                              'xh2a|houmo|hm_smi|ERR_FATAL|SyncYieldErr|group sync|exec timeout|auto reset|Out of memory|oom-kill|Killed process|watchdog|BUG:|panic|I/O error|AER:'], limit=16_000_000)
            kernel['scope'] = 'current CoPaw instance lifetime; NPU/driver/OOM/panic/storage related journal entries'
            monitor.save('kernel-before-test.json', kernel)
            if kernel['code'] not in {0,1} or kernel['truncated']:
                raise RuntimeError('kernel preflight did not provide complete bounded history')
            if FATAL.search(kernel['out']):
                raise RuntimeError('existing NPU fatal since current CoPaw start; no test was submitted')
            monitor.snapshot('baseline')
            monitor.save('baseline-versions.json', {
                'files': {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in
                          [Path(__file__), DEPLOY/'npu_soak_monitor.py', DEPLOY/'qwen38-long-context-test.py',
                           DEPLOY/'llm-benchmark.py', DEPLOY/'modelctl', DEPLOY/'compose.yaml', DEPLOY/'soak_report.py']},
                'uname': command(['uname', '-a']), 'driver': command(['modinfo', 'xh2a_drv']),
                'systemd': command(['systemctl', 'show', 'houmo-ai.service', '-p', 'ActiveState', '-p', 'ExecMainStatus', '-p', 'DropInPaths'])})
            if args.monitor_only_seconds:
                monitor.phase('monitor_only_validation')
                monitor.closed.wait(args.monitor_only_seconds)
                if monitor.abort.is_set():
                    raise RequestAborted('monitor-only validation failed')
                report.update(state='monitor_verified', reason='no_inference_submitted')
                return 0
            direct_host = next(v['IPAddress'] for v in copaw['NetworkSettings']['Networks'].values() if v.get('IPAddress'))
            code, props, _ = request(direct_host, 17701, '/props', None, env['HOUMO_API_KEY'], method='GET', timeout=15, monitor=monitor)
            if code != 200:
                raise RuntimeError('runtime props unavailable')
            report.update(slots=int(props['total_slots']), context_size=int(props['default_generation_settings']['n_ctx']))
            if max(args.targets) > report['context_size'] - 512:
                raise RuntimeError('requested inputs exceed actual per-slot context')
            expiry = (datetime.now(timezone.utc)+timedelta(minutes=args.minutes+60)).isoformat()
            code, key, _ = request(GATEWAY_HOST, 17700, '/internal/api-keys',
                {'name': '__S09_CoPaw_'+run_id, 'policy': {'application': 'S09 monitored soak',
                 'allowed_models': [MODEL], 'expires_at': expiry, 'max_concurrent_requests': 1,
                 'rate_limit_per_minute': 120, 'daily_request_limit': 5000}}, env['GATEWAY_MANAGEMENT_TOKEN'], timeout=15)
            if code != 201:
                raise RuntimeError('dedicated test Key creation failed')
            key_id = key['id']; business_key = key['key']; monitor.secrets.append(business_key)
            report['test_key_id'] = key_id
            monitor.save('test-key-metadata.json', {'id': key_id, 'expires_at': expiry, 'name': key['name']})
            runner = load_runner()
            # Reuse prompt generation while replacing its HTTP path with guarded calls.
            runner.request_json = lambda host, port, path, payload, api_key, timeout=120: request(
                host, port, path, payload, api_key, timeout=min(timeout, 120), monitor=monitor,
                max_body=8_000_000)
            started = time.monotonic()
            report.update(state='running', started_at=stamp()['at'], start_monotonic_ns=time.monotonic_ns(),
                          expected_finish_at=(datetime.now(timezone.utc)+timedelta(minutes=args.minutes)).isoformat())
            checkpoint()
            index = 0
            while time.monotonic()-started < args.minutes*60:
                if monitor.abort.is_set():
                    raise RequestAborted('monitor stopped the run')
                target = args.targets[index % len(args.targets)]
                req_id = f'copaw-soak-{run_id}-{index+1:06d}'
                seed = f'{run_id}-{index+1:06d}'
                inflight = {**stamp(), 'request_id': req_id, 'seed': seed, 'target_tokens': target, 'stage': 'tokenizing'}
                with report_lock:
                    report['inflight'] = inflight
                monitor.phase('tokenizing', **{k:v for k,v in inflight.items() if k!='stage'})
                checkpoint()
                prompt, expected, actual = runner.build_prompt(direct_host, env['HOUMO_API_KEY'], target, seed)
                with report_lock:
                    report['inflight'] = {**inflight, **stamp(), 'stage': 'inference', 'tokenizer_tokens': actual,
                                          'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest()}
                monitor.phase('inference', **{k:v for k,v in report['inflight'].items() if k!='stage'})
                checkpoint()
                code, body, elapsed = request(GATEWAY_HOST, 17700, '/v1/chat/completions',
                    {'model': MODEL, 'messages': [{'role':'user','content':prompt}],
                     'temperature':0, 'max_tokens':160, 'stream':False}, business_key,
                    request_id=req_id, monitor=monitor,slow_after=240 if target>65536 else 120 if target>=65536 else 30)
                answer = runner.answer_content(body)
                correct, fields = runner.validate_answer(answer, expected)
                row = {**report['inflight'], 'request_started_at': report['inflight']['at'], **stamp(), 'stage': 'completed', 'status': code,
                       'elapsed_seconds': elapsed, 'correct': code==200 and correct, 'field_checks': fields,
                       'expected': expected, 'answer': answer, 'response': body}
                monitor.save(f'request-{index+1:06d}.json', row)
                monitor.event('request_completed', request_id=req_id, target_tokens=target, status=code,
                              correct=row['correct'], elapsed_seconds=elapsed)
                with report_lock:
                    report['completed_requests'] += 1; report['http_success'] += int(code==200)
                    report['correct_requests'] += int(row['correct']); report['inflight'] = None
                checkpoint()
                print(json.dumps({'request':index+1,'target':target,'http':code,'correct':row['correct'],'seconds':round(elapsed,2)}), flush=True)
                if code != 200:
                    raise RuntimeError(f'HTTP {code}; stopped without automatic recovery or retries')
                index += 1
            final_request_id = f'copaw-soak-{run_id}-final'
            report['inflight'] = {**stamp(), 'stage':'final_probe', 'request_id':final_request_id}
            monitor.phase('final_probe', **{k:v for k,v in report['inflight'].items() if k!='stage'})
            checkpoint()
            code, body, elapsed = request(GATEWAY_HOST, 17700, '/v1/chat/completions',
                {'model':MODEL,'messages':[{'role':'user','content':'只输出 RECOVERY_OK'}],
                 'temperature':0,'max_tokens':16},business_key,request_id=final_request_id,monitor=monitor,slow_after=30)
            final_probe = {'status':code,'answer':runner.answer_content(body),'elapsed_seconds':elapsed}
            final_probe['passed'] = code==200 and final_probe['answer'].strip()=='RECOVERY_OK'
            monitor.save('final-probe.json',final_probe)
            report['inflight'] = None
            if monitor.abort.is_set():
                raise RequestAborted('fault during final probe')
            report.update(state='completed', duration_completed=True, final_probe=final_probe,
                          passed=report['correct_requests']==report['completed_requests'] and final_probe['passed'])
            report['reason'] = 'all_checks_passed' if report['passed'] else 'answer_validation_failed'
            return 0 if report['passed'] else 2
        except BaseException as exc:
            report.update(state='failed', reason=monitor.clean(str(exc)), duration_completed=False, passed=False)
            if monitor.first_fault is None:
                monitor.fail('test_error','runner',str(exc))
            contain_once()
            if not monitor.hardware_fault and monitor.first_fault:
                with contextlib.suppress(Exception):
                    monitor.snapshot('fault-without-device-stop', stacks=True)
            if report['inflight']:
                monitor.save('interrupted-request.json', {**report['inflight'], 'state':'interrupted', 'upstream_result':'unknown'})
            return 2
        finally:
            heartbeat_stop.set()
            if heartbeat_thread.is_alive():
                heartbeat_thread.join(timeout=6)
            if key_id:
                try:
                    code, _, _ = request(GATEWAY_HOST,17700,'/internal/api-keys/'+key_id,None,
                                         env['GATEWAY_MANAGEMENT_TOKEN'],method='DELETE',timeout=10)
                    report['test_key_revoked'] = code in {200,404}
                except Exception:
                    report['test_key_revoked'] = False
                if not report['test_key_revoked']:
                    report.update(state='failed', reason='test_key_cleanup_failed', passed=False)
            with contextlib.suppress(Exception):
                monitor.snapshot('final')
            try:
                contain_once()
                monitor.close(before_writer_close=contain_once)
            except Exception as close_error:
                report.update(state='failed', reason='monitor cleanup: '+monitor.clean(str(close_error)), passed=False)
            if monitor.first_fault and report['state'] in {'completed','monitor_verified'}:
                report.update(state='failed', reason=monitor.first_fault['kind'], passed=False)
            report['finished_at'] = stamp()['at']; checkpoint()
            manifest=[]
            for path in sorted(root.iterdir()):
                if path.is_file() and path.name!='evidence-manifest.json':
                    with path.open('rb') as stream:
                        digest=hashlib.file_digest(stream,'sha256').hexdigest()
                    manifest.append({'file':path.name,'size':path.stat().st_size,'sha256':digest})
            monitor.save('evidence-manifest.json',manifest)
            report_error = None
            report_output = DEPLOY/'state/monitored-soak-reports'/run_id
            try:
                from soak_report import generate
                generate(root, report_output)
            except Exception as exc:
                report_error = monitor.clean(str(exc))
                atomic_json(report_output/'generation-error.json', {'error':report_error,'evidence_root':str(root)})
            for sig, handler in old_handlers.items():
                signal.signal(sig,handler)
            print(json.dumps({'state':report['state'],'run_id':run_id,'evidence_root':str(root),'passed':report['passed'],
                              'acceptance_report':str(report_output),'report_error':report_error}),flush=True)
            if report_error or report['state'] not in {'completed','monitor_verified'} or (report['state']=='completed' and not report['passed']):
                return 2  # A late capture failure must also fail the process exit code.


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--minutes',type=float,default=240)
    parser.add_argument('--targets',type=int,nargs='+',default=[8192,65536,118000,118000])
    parser.add_argument('--run-id')
    parser.add_argument('--monitor-only-seconds',type=int,default=0)
    parser.add_argument('--status',type=Path)
    args=parser.parse_args()
    if args.status:
        result=observed_status(json.loads(args.status.read_text()))
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    if not 0<args.minutes<=240 or any(v<512 or v>130560 for v in args.targets) or not 0<=args.monitor_only_seconds<=60:
        parser.error('invalid test duration, targets or monitor-only duration')
    return run(args)


if __name__=='__main__':
    raise SystemExit(main())
