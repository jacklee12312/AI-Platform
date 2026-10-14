"""Explicitly invoked live S07/S08 acceptance. Creates/revokes ONLY its own keys.

Requires MODELDECK_ADMIN_PASSWORD and BOX_SSH_PASSWORD in the environment.
Runs short inference probes and one Matcha/Kokoro restart each; never switches
model mode, changes runtime parameters, or invokes NPU recovery.
"""
import http.cookiejar
import io
import json
import os
import re
import shlex
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
import paramiko

sys.stdout.reconfigure(encoding='utf-8')
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ai-model-gateway'))
from app.usage_quality import usage_details
OUT = ROOT / 'output/outputs/s07-s08-20260904'
OUT.mkdir(parents=True, exist_ok=True)
stamp = datetime.now().strftime('%Y%m%dT%H%M%S')
since = datetime.now(timezone.utc).isoformat()
admin = 'http://192.168.133.1:17780'
gateway = 'http://192.168.133.1:17700'
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
ssh = paramiko.SSHClient()
ssh.load_system_host_keys()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('192.168.133.1', username='cix', password=os.environ['BOX_SSH_PASSWORD'], look_for_keys=False, allow_agent=False, timeout=10)
created, events, jobs, checks = [], [], [], []
result = {'started_at': since, 'run': stamp, 'events': events, 'jobs': jobs, 'checks': checks, 'keys': []}
csrf = ''

def call(path, method='GET', payload=None, key=None, rid=None, expected=200, binary=False):
    headers = {'X-CSRF-Token': csrf}
    if key: headers['Authorization'] = 'Bearer ' + key
    if rid: headers['X-Request-ID'] = rid
    if payload is not None: headers['Content-Type'] = 'application/json'
    req = urllib.request.Request((gateway if key else admin) + path, method=method, headers=headers,
        data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode())
    start=time.perf_counter()
    try:
        response=opener.open(req, timeout=90)
    except urllib.error.HTTPError as error:
        response=error
    with response:
        body=response.read();status=response.code
    assert status==expected, f'{method} {path}: HTTP {status}, expected {expected}'
    return body if binary else json.loads(body), round(time.perf_counter()-start,3)

def remote(source):
    _,stdout,stderr=ssh.exec_command('python3 -c '+shlex.quote(source),timeout=40)
    body=stdout.read().decode();err=stderr.read().decode()
    assert stdout.channel.recv_exit_status()==0,err[-1000:]
    return json.loads(body)

def fatal_check(label):
    data=remote('SINCE='+repr(since)+'\n'+r'''
import subprocess,json,re
names=['houmo-copaw','houmo-qwen3-vl-8b','houmo-glm-ocr-0.9b','houmo-bge-m3','houmo-reranker']
logs={}
for n in names:
 p=subprocess.run(['docker','logs','--since',SINCE,n],capture_output=True,text=True);logs[n]=p.stdout+p.stderr
pattern=re.compile(r'ERR_FATAL|SyncYieldErr|synchronize.*timed?\s*out|synchronize.*timeout',re.I)
hits={n:[line for line in body.splitlines() if pattern.search(line)] for n,body in logs.items()}
print(json.dumps({'fatal':{n:v for n,v in hits.items() if v},'logs':logs}))
''')
    (OUT/f'{stamp}-{label}-logs.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    assert not data['fatal'], 'NPU fatal detected: stop testing; do not retry or recover'

def record(key, path, payload, *, rid=None, expected=200):
    rid=rid or 's08-'+uuid.uuid4().hex
    body,seconds=call(path,'POST',payload,key['key'],rid,expected)
    event={'key_id':key['id'],'request_id':rid,'endpoint':path,'model':payload.get('model'), 'status':'succeeded' if expected==200 else 'rejected',
           'usage':body.get('usage'), 'seconds':seconds}
    if path=='/v1/rerank': event['scores']=[r.get('relevance_score') for r in body.get('results',[])]
    events.append(event)
    fatal_check('request-'+str(len(events)))
    return body

def stream(key, cancel=False):
    rid='s08-'+uuid.uuid4().hex
    payload={'model':'copaw-flash-9b','messages':[{'role':'user','content':'请从一数到二十，用空格分隔。'}], 'stream':True,
             'stream_options':{'include_usage':True},'max_tokens':64 if cancel else 16,'temperature':0}
    request=urllib.request.Request(gateway+'/v1/chat/completions',data=json.dumps(payload).encode(),
        headers={'Content-Type':'application/json','Authorization':'Bearer '+key['key'],'X-Request-ID':rid})
    usage=None;done=False;chunks=0;start=time.perf_counter()
    with opener.open(request,timeout=90) as response:
        for raw in response:
            line=raw.decode('utf-8').strip()
            if not line.startswith('data:'):continue
            value=line[5:].strip()
            if value=='[DONE]':done=True;break
            body=json.loads(value);assert 'error' not in body, 'SSE error'
            if isinstance(body.get('usage'),dict):usage=body['usage']
            if any(c.get('delta',{}).get('content') for c in body.get('choices',[])):
                chunks+=1
                if cancel:break
    if not cancel:assert done and usage is not None,'Missing SSE usage or DONE'
    events.append({'key_id':key['id'],'request_id':rid,'endpoint':'/v1/chat/completions','model':'copaw-flash-9b',
        'status':'cancelled' if cancel else 'succeeded','usage':usage,'stream':True,'cancel_requested':cancel,'chunks_read':chunks,'done_read':done,'seconds':round(time.perf_counter()-start,3)})
    fatal_check('stream-'+str(len(events)))

try:
    login,_=call('/api/login','POST',{'password':os.environ['MODELDECK_ADMIN_PASSWORD']})
    csrf=login['csrf']
    existing,_=call('/api/api-keys')
    original={item['id']:{k:item[k] for k in ('name','prefix','enabled','revoked_at')} for item in existing['data']}
    overview,_=call('/api/overview')
    assert overview['gateway']['error_models']==0, 'Initial gateway unhealthy'
    fatal_check('before')
    for name in 'ABC':
        item,_=call('/api/api-keys','POST',{'name':f'__S08_{stamp}_{name}'},expected=201)
        created.append(item)
        result['keys'].append({k:item[k] for k in ('id','name','prefix')})
    assert len({k['key'] for k in created})==3
    for item in created:call('/v1/models',key=item['key'])
    checks.append('three_distinct_keys_authenticate')

    for service in ([] if '--skip-service-restarts' in sys.argv else ['matcha','kokoro']):
        before=remote('import subprocess,json; i=json.loads(subprocess.check_output(["docker","inspect",'+repr('houmo-'+service+'-tts')+'],text=True))[0];print(json.dumps({"started_at":i["State"]["StartedAt"]}))')
        job,_=call(f'/api/services/{service}/restart','POST')
        started=time.perf_counter();statuses=[]
        while time.perf_counter()-started<360:
            job,_=call('/api/jobs/'+job['id']);statuses.append(job['status'])
            if job['status'] in ('succeeded','failed','unknown'):break
            time.sleep(1)
        assert job['status']=='succeeded',f'{service} restart not healthy: '+str(job.get('error'))
        assert job['result']['ready'] and job['result']['started_at']!=before['started_at']
        jobs.append({'service':service,'job':job,'observed_statuses':list(dict.fromkeys(statuses)),'seconds':round(time.perf_counter()-started,3)})
    if jobs: checks.append('tts_restarts_changed_started_at_and_waited_healthy')
    else: result['service_restart_evidence'] = 'live-acceptance-20260904T163535.json'
    call('/api/services/qwen38/restart','POST',expected=409)
    checks.append('inactive_model_restart_rejected')
    for service in ['matcha','kokoro','glm-ocr']:
        logs,_=call('/api/logs?service='+service+'&tail=20');assert logs is not None
    checks.append('tts_ocr_logs_available')

    chat={'model':'copaw-flash-9b','messages':[{'role':'user','content':'只回答OK'}],'max_tokens':8,'temperature':0}
    record(created[0],'/v1/chat/completions',chat)
    stream(created[1])
    record(created[2],'/v1/embeddings',{'model':'bge-m3','input':['用量核对样例']})
    record(created[2],'/v1/rerank',{'model':'bge-reranker-v2-m3','query':'苹果','documents':['苹果水果','海洋'],'top_n':2})
    shared='s08-repeated-'+uuid.uuid4().hex
    for _ in range(2):record(created[0],'/v1/chat/completions',chat,rid=shared)
    record(created[2],'/v1/chat/completions',{'model':'copaw-flash-9b','messages':[]},expected=400)
    stream(created[1],cancel=True)

    management_id='s08-console-'+uuid.uuid4().hex
    call('/api/test/chat','POST',{'model':'copaw-flash-9b','prompt':'只回答OK','stream':False,'max_tokens':8,'temperature':0},rid=management_id)
    after,_=call('/api/usage?request_id='+management_id)
    assert after['summary']['request_count']==0,'Management inference incorrectly metered'
    result['management_request_id']=management_id
    checks.append('management_console_test_excluded')
    fatal_check('after-inference')

    # Match each actual response to the record; allow terminal write to finish after cancellation.
    deadline=time.monotonic()+15
    reports={}
    while time.monotonic()<deadline:
        reports={item['id']:call('/api/usage?'+urllib.parse.urlencode({'api_key_id':item['id'],'limit':100}))[0] for item in created}
        if sum(r['summary']['request_count'] for r in reports.values())==len(events):break
        time.sleep(.5)
    assert sum(r['summary']['request_count'] for r in reports.values())==len(events), 'Missing/duplicate usage event'
    for key_id,report in reports.items():
        expected=[e for e in events if e['key_id']==key_id]
        assert report['summary']['request_count']==len(expected)
        for event in expected:
            rows=[r for r in report['recent'] if r['request_id']==event['request_id']]
            assert len(rows)==sum(e['request_id']==event['request_id'] for e in expected)
            for row in rows:
                if event.get('cancel_requested'):
                    assert row['status'] in ('cancelled','interrupted','succeeded')
                    event['observed_terminal_status']=row['status']
                    event['server_usage_quality']=row['usage_quality']
                    # Completion may race client cancellation; preserve whatever upstream actually reported.
                else:
                    assert row['status']==event['status'], (event['request_id'],row['status'])
                    detail=usage_details(event['usage'],event['endpoint'])
                    assert {f:row[f] for f in detail['tokens']}==detail['tokens']
                    assert row['usage_quality']==detail['quality']
        for field in ('input_tokens','output_tokens','total_tokens'):
            total=sum(r[field] or 0 for r in report['recent'])
            assert total==report['summary'][field]==sum(r[field] for r in report['by_key'])==sum(r[field] for r in report['by_model'])
    result['reports']=reports
    checks.append('raw_responses_equal_details_key_model_summaries')
    report,_=call('/api/usage?'+urllib.parse.urlencode({'request_id':shared,'model':'copaw-flash-9b','limit':1,'offset':1}))
    assert report['summary']['request_count']==2 and len(report['recent'])==1
    checks.append('request_model_filter_pagination_and_caller_id_not_deduplicated')
    call('/api/usage?start=not-a-date',expected=400)
    item=created[0]
    call('/api/api-keys/'+item['id'],'PATCH',{'enabled':False})
    call('/v1/models',key=item['key'],expected=401)
    call('/api/api-keys/'+item['id'],'PATCH',{'enabled':True,'name':item['name']+'-renamed'})
    call('/v1/models',key=item['key'])
    checks.append('disable_reenable_rename_actual_authorization')
    result['passed']=True
except Exception as error:
    result['passed']=False
    result['error']=str(error)
    raise
finally:
    cleanup=[]
    for item in created:
        try:
            call('/api/api-keys/'+item['id'],'DELETE')
            call('/v1/models',key=item['key'],expected=401)
            historical,_=call('/api/usage?api_key_id='+item['id'])
            cleanup.append({'id':item['id'],'revoked':True,'historical_rows':historical['summary']['request_count']})
        except Exception as error:
            cleanup.append({'id':item['id'],'revoked':False,'error':str(error)})
            result['passed']=False
    result['cleanup']=cleanup
    if csrf:
        final_keys,_=call('/api/api-keys')
        result['existing_keys_unchanged']=all({k:item[k] for k in ('name','prefix','enabled','revoked_at')}==original[item['id']] for item in final_keys['data'] if item['id'] in original)
        result['passed']=result.get('passed',False) and result['existing_keys_unchanged']
    result['finished_at']=datetime.now(timezone.utc).isoformat()
    (OUT/'live-acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (OUT/f'live-acceptance-{stamp}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ['reports','jobs','events']},ensure_ascii=False))
    ssh.close()
