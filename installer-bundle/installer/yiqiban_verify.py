"""Real container-to-gateway acceptance; preserve the operator's accelerator mode."""
import json
import math
import secrets
from common import atomic_json
from yiqiban_config import GATEWAY_URL, MODES, read_env

SERVICES = ('weknora-backend', 'agent-runner', 'qwenpaw')


def response_valid(kind, body):
    if not isinstance(body, dict):return False
    if kind == 'chat':
        choices = body.get('choices')
        message = choices[0].get('message') if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        return bool(isinstance(message, dict) and isinstance(message.get('content'), str) and message['content'].strip())
    if kind == 'embedding':
        rows = body.get('data')
        vector = rows[0].get('embedding') if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
        return bool(isinstance(vector, list) and len(vector) == 1024 and
                    all(type(v) in (float, int) and math.isfinite(v) for v in vector))
    if kind == 'rerank':
        rows = body.get('results', body.get('data'))
        if not isinstance(rows, list) or len(rows) != 2:return False
        return all(isinstance(r, dict) and type(r.get('index')) is int and r['index'] in (0, 1)
                   and type(r.get('relevance_score', r.get('score'))) in (int, float)
                   and math.isfinite(r.get('relevance_score', r.get('score'))) for r in rows) and {r['index'] for r in rows} == {0, 1}
    return False


def container_request(app, service, path, key, body=None):
    ids = app.docker_compose('ps', '-q', service).stdout.split()
    if len(ids) != 1:raise RuntimeError('翼起办容器未就绪：' + service)
    command = 'curl -sS --max-time 240 -H "Authorization: Bearer $YIQIBAN_TEST_KEY" '
    if body is not None:command += '-H "Content-Type: application/json" --data-binary @- '
    command += '-w "\\n%{http_code}" "$YIQIBAN_TEST_URL"'
    env = dict(__import__('os').environ, YIQIBAN_TEST_KEY=key, YIQIBAN_TEST_URL=GATEWAY_URL + path)
    result = app.run(['docker', 'exec', '-i', '--env', 'YIQIBAN_TEST_KEY', '--env', 'YIQIBAN_TEST_URL', ids[0],
                      'sh', '-c', command], input=json.dumps(body) if body is not None else None,
                     env=env, timeout=270)
    try:
        payload, status = result.stdout.rsplit('\n', 1)
        return int(status), json.loads(payload)
    except (ValueError, TypeError):raise RuntimeError('翼起办网关响应格式错误：' + service) from None


def verify(app):
    owner = app.owner;plan = app.plan
    key = read_env(owner.home / 'houmo-docker-deployment/.env')['HOUMO_API_KEY']
    mode_path = owner.home / 'houmo-docker-deployment/state/active-mode'
    original = app.state.get('verification_original_mode') or mode_path.read_text().strip()
    if original not in set(MODES.values()) | {'glm-ocr', 'cosyvoice'}:raise RuntimeError('无法识别验收前的模型运行模式')
    app.state['verification_original_mode'] = original;app.save()
    report = {'schema': 1, 'run_id': secrets.token_hex(16), 'scope': 'yiqiban-container-to-gateway', 'passed': False,
              'mode_policy': 'explicit-switch-during-install', 'original_mode': original, 'checks': [],
              'not_installed': [kind for kind in ('embedding', 'rerank') if not plan[kind]],
              'business_activation': 'pending-first-admin-registration-and-valid-license',
              'application_business_api_acceptance': 'not_run'}
    expected = {m for kind in ('chat', 'embedding', 'rerank') for m in plan[kind]}
    def record(name):
        report['checks'].append({'name': name, 'passed': True})
        owner.event('翼起办联通检查通过：' + name)
    try:
        for service in SERVICES:
            owner.pause_point()
            status, data = container_request(app, service, '/models', key)
            rows=data.get('data') if isinstance(data, dict) else None
            if status != 200 or not isinstance(rows, list) or not expected.issubset({r.get('id') for r in rows if isinstance(r, dict)}):
                raise RuntimeError('翼起办无法获取所配置的模型清单：' + service)
            record('models:' + service)
        status, _ = container_request(app, 'weknora-backend', '/models', 'invalid-install-check-' + secrets.token_hex(12))
        if status not in (401, 403):raise RuntimeError('模型网关未拒绝错误密钥')
        record('reject-invalid-api-key')
        for kind in ('chat', 'embedding', 'rerank'):
            for model in plan[kind]:
                owner.pause_point();mode = MODES[model]
                owner.update('翼起办模型联通测试：切换至 ' + model, force=True)
                app.run(['/usr/local/bin/modelctl', 'switch', mode], timeout=1500)
                app.run(['python3', owner.home / 'gateway-admin/startup_verify.py', '--force', '--source', 'manual'], timeout=1500)
                if kind == 'chat':
                    path = '/chat/completions'
                    body = {'model': model, 'messages': [{'role': 'user', 'content': 'Reply with one short word: OK'}],
                            'max_tokens': 256, 'temperature': 0, 'stream': False}
                elif kind == 'embedding':path = '/embeddings';body = {'model': model, 'input': ['YiQiBan local model connection test']}
                else:
                    path = '/rerank';body = {'model': model, 'query': 'local model',
                                            'documents': ['local model service', 'weather forecast'], 'top_n': 2}
                for service in SERVICES if kind == 'chat' else SERVICES[:2]:
                    owner.pause_point();status, data = container_request(app, service, path, key, body)
                    if status != 200 or not response_valid(kind, data):
                        raise RuntimeError('翼起办真实推理联通失败：' + service + ' / ' + model)
                    record(kind + ':' + service + ':' + model)
        report['passed'] = True
    finally:
        try:
            app.run(['/usr/local/bin/modelctl', 'switch', original], timeout=1500)
            app.run(['python3', owner.home / 'gateway-admin/startup_verify.py', '--force', '--source', 'manual'], timeout=1500)
            if mode_path.read_text().strip() != original:raise RuntimeError('原模型运行模式未恢复')
            report['restored_mode'] = original
            app.state.pop('verification_original_mode', None);app.save()
        except Exception:
            report['passed'] = False;report['mode_restore_failed'] = True
            raise
        finally:atomic_json(app.base / 'acceptance.json', report, 0o600)
    return report
