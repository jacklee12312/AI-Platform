"""Opt-in live access-policy acceptance; uses only dedicated keys and restores limits.

Run against the authorized local box only, in a quiet test window. Requires
MODELDECK_ADMIN_PASSWORD. Set KEY_ACCESS_PERSISTENCE_COMMAND to a reviewed JSON
argv for an optional gateway/admin restart check; model services stay running.
"""
import http.cookiejar
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from websockets.sync.client import connect

sys.stdout.reconfigure(encoding='utf-8')
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'output/outputs/key-access-20260907'
OUT.mkdir(parents=True, exist_ok=True)
ADMIN = 'http://192.168.133.1:17780'
GATEWAY = 'http://192.168.133.1:17700'
stamp = datetime.now().strftime('%Y%m%dT%H%M%S')
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
csrf = ''
created = []
result = {'run': stamp, 'started_at': datetime.now(timezone.utc).isoformat(), 'checks': [], 'keys': []}

def call(path, method='GET', payload=None, key=None, expected=200):
    headers = {'X-CSRF-Token': csrf, 'X-Request-ID': 'key-access-' + uuid.uuid4().hex}
    if key: headers['Authorization'] = 'Bearer ' + key['key']
    if payload is not None: headers['Content-Type'] = 'application/json'
    request = urllib.request.Request((GATEWAY if key else ADMIN) + path, method=method, headers=headers,
        data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None)
    started = time.monotonic()
    try: response = opener.open(request, timeout=45)
    except urllib.error.HTTPError as error: response = error
    with response:
        status = response.status; body = json.load(response)
    result['checks'].append({'path': path, 'method': method, 'status': status,
                             'expected': expected, 'seconds': round(time.monotonic()-started, 3),
                             'error_code': body.get('error', {}).get('code')})
    assert status == expected, f'{method} {path}: {status} != {expected}'
    return body

def create(suffix, policy):
    key = call('/api/api-keys', 'POST', {'name': '__KEY_ACCESS_' + stamp + '_' + suffix, 'policy': policy}, expected=201)
    created.append(key)
    result['keys'].append({k: key[k] for k in ['id', 'name', 'prefix']})
    return key

def update(key, policy): return call('/api/api-keys/' + key['id'], 'PATCH', {'policy': policy})
def resource(policy): return call('/api/resource-policy', 'PUT', policy)
def visible_key(key): return next(k for k in call('/api/api-keys')['data'] if k['id'] == key['id'])
def stable_key(key): return {f: key[f] for f in ['id','name','prefix','created_at','enabled','revoked_at','policy']}

original = None
old_keys = []
try:
    call('/api/resource-policy', expected=401)
    csrf = call('/api/login', 'POST', {'password': os.environ['MODELDECK_ADMIN_PASSWORD']})['csrf']
    old_keys = call('/api/api-keys')['data']
    snapshot = call('/api/resource-policy'); original = snapshot['policy']
    assert snapshot['in_flight']['global'] == 0, 'Wait for a quiet test window'
    a = create('A', {'allowed_models':['bge-m3'], 'application':'权限实机验收', 'expires_at':'2030-01-01T00:00:00Z',
                     'rate_limit_per_minute':20, 'max_concurrent_requests':1, 'daily_request_limit':1, 'monthly_token_warning':1})
    b = create('B', {'allowed_models':[], 'expires_at':'2020-01-01T00:00:00Z'})
    c = create('C', {'allowed_models':[], 'rate_limit_per_minute':1})
    d = create('D', {'allowed_models':['sensevoice','bge-m3'], 'max_concurrent_requests':1})
    assert [m['id'] for m in call('/v1/models', key=a)['data']] == ['bge-m3']
    assert call('/v1/chat/completions','POST',{'model':'copaw-flash-9b','messages':[{'role':'user','content':'test'}]},a,403)['error']['code']=='model_not_allowed'
    payload = {'model':'bge-m3','input':'权限与资源控制验收'}
    embedding = call('/v1/embeddings','POST',payload,a)
    assert len(embedding['data'][0]['embedding']) == 1024
    result['embedding_usage'] = embedding.get('usage')
    assert call('/v1/embeddings','POST',payload,a,429)['error']['code']=='daily_quota_exceeded'
    summary = visible_key(a)
    assert summary['access']['daily_requests'] == 1 and summary['access']['daily_remaining'] == 0
    assert summary['access']['token_warning_reached']
    saved = update(a, {'daily_request_limit':2})
    assert saved['policy']['allowed_models'] == ['bge-m3']
    assert saved['policy']['expires_at'] == '2030-01-01T00:00:00Z'
    call('/v1/embeddings','POST',payload,a)
    assert call('/v1/models',key=b,expected=401)['error']['code']=='api_key_expired'
    update(b, {'expires_at':None})
    assert call('/v1/models',key=b)['data']==[]
    assert call('/v1/embeddings','POST',payload,b,403)['error']['code']=='model_not_allowed'
    assert call('/v1/models',key=c)['data']==[]
    assert call('/v1/models',key=c,expected=429)['error']['code']=='rate_limit_exceeded'
    update(b, {'allowed_models':['sensevoice','bge-m3','matcha-zh']})
    with connect('ws://192.168.133.1:17700/v1/audio/transcriptions/stream',
                 additional_headers={'Authorization':'Bearer '+d['key']}, open_timeout=10, close_timeout=5) as ws:
        ws.send(json.dumps({'type':'start','model':'sensevoice','sample_rate':16000}))
        assert json.loads(ws.recv(timeout=10))['type']=='ready'
        assert call('/v1/embeddings','POST',payload,d,429)['error']['code']=='key_concurrency_exceeded'
        resource({'global_max_concurrent':1})
        assert call('/v1/embeddings','POST',payload,b,429)['error']['code']=='global_concurrency_exceeded'
        resource({'model_limits':{'sensevoice':1}})
        with connect('ws://192.168.133.1:17700/v1/audio/transcriptions/stream',
                     additional_headers={'Authorization':'Bearer '+b['key']}, open_timeout=10, close_timeout=5) as blocked:
            assert json.loads(blocked.recv(timeout=5))['code']=='model_concurrency_exceeded'
        resource({'pool_limits':{'CPU':1}})
        assert call('/v1/audio/speech','POST',{'model':'matcha','input':'测试'},b,429)['error']['code']=='pool_concurrency_exceeded'
        snap = call('/api/resource-policy')
        assert snap['in_flight']['global']==1 and snap['in_flight']['key'][d['id']]==1
        result['in_flight_observed'] = snap['in_flight']
        ws.send(json.dumps({'type':'stop'}))
        assert json.loads(ws.recv(timeout=5))['type']=='done'
    for _ in range(20):
        snap = call('/api/resource-policy')
        if snap['in_flight']['global']==0: break
        time.sleep(.1)
    assert snap['in_flight']['global']==0
    result['rejections'] = [r for r in snap['recent_rejections'] if r['api_key_id'] in {k['id'] for k in created}]
    assert len(result['rejections']) >= 8
    persistent_policy = {'global_max_concurrent':64,'model_limits':{'bge-m3':2},'pool_limits':{'CPU':16}}
    resource(persistent_policy)
    if os.environ.get('KEY_ACCESS_PERSISTENCE_COMMAND'):
        (OUT/('live-acceptance-'+stamp+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        process = subprocess.run(json.loads(os.environ['KEY_ACCESS_PERSISTENCE_COMMAND']),capture_output=True,text=True,timeout=65)
        assert process.returncode==0, process.stderr[-500:]
        result['restart_persistence'] = json.loads(process.stdout)
        # Admin sessions are in memory and intentionally expire after a restart.
        csrf = call('/api/login', 'POST', {'password': os.environ['MODELDECK_ADMIN_PASSWORD']})['csrf']
        assert call('/api/resource-policy')['policy']==persistent_policy
        assert visible_key(a)['access']['daily_requests']==2
        assert call('/v1/embeddings','POST',payload,a,429)['error']['code']=='daily_quota_exceeded'
    result['passed'] = True
finally:
    if csrf:
        try: csrf = call('/api/login', 'POST', {'password': os.environ['MODELDECK_ADMIN_PASSWORD']})['csrf']
        except Exception as exc: result['cleanup_login_error'] = str(exc)
    if original is not None:
        try: resource(original); result['resource_policy_restored']=True
        except Exception as exc: result['restore_error']=str(exc)
    for key in created:
        try: call('/api/api-keys/'+key['id'], 'DELETE')
        except Exception as exc: result.setdefault('cleanup_errors',[]).append({'id':key['id'],'error':str(exc)})
    if csrf:
        try:
            fresh=call('/api/api-keys')['data']
            result['all_test_keys_revoked']=all(next(k for k in fresh if k['id']==c['id'])['revoked_at'] for c in created)
            result['existing_keys_unchanged']=all(stable_key(next(k for k in fresh if k['id']==o['id']))==stable_key(o) for o in old_keys)
        except Exception as exc: result['cleanup_verification_error'] = str(exc)
    (OUT/('live-acceptance-'+stamp+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (OUT/'live-acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ['keys','checks','rejections','in_flight_observed']},ensure_ascii=False))

assert result.get('passed') and result.get('resource_policy_restored') and result.get('all_test_keys_revoked') and result.get('existing_keys_unchanged'), 'Acceptance or cleanup incomplete; see saved evidence'
