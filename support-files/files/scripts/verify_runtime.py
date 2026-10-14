#!/usr/bin/env python3
"""Real inference acceptance. --all-modes deliberately switches accelerator modes."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parent.parent
HOME=Path('/home/cix')
DEPLOY=HOME/'houmo-docker-deployment'
REPORT=DEPLOY/'state/offline-acceptance.json'

def run(*command, timeout=1500):
    return subprocess.run(list(command),check=True,capture_output=True,text=True,timeout=timeout).stdout

def request(path, key, body=None, content_type='application/json'):
    if isinstance(body,dict):body=json.dumps(body,ensure_ascii=False).encode()
    headers={'Content-Type':content_type}
    if key:headers['Authorization']='Bearer '+key
    req=urllib.request.Request('http://127.0.0.1:17700'+path,data=body,headers=headers)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req,timeout=300) as response:return response.read()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--all-modes',action='store_true')
    parser.add_argument('--run-id',default=None)
    args=parser.parse_args()
    result={'started_at':datetime.now(timezone.utc).isoformat(),'scope':'all-modes' if args.all_modes else 'active-mode-and-cpu','run_id':args.run_id,'checks':[],'passed':False}
    current='containers-and-autostart'
    def record(name):
        result['checks'].append({'name':name,'passed':True})
        print('PASS '+name,flush=True)
    try:
        env=dict(line.strip().split('=',1) for line in (DEPLOY/'.env').read_text().splitlines() if line and not line.startswith('#') and '=' in line)
        key=env['HOUMO_API_KEY'].strip("'\"")
        for unit in ['docker','houmo-ai','ai-model-gateway','gateway-admin']:
            run('systemctl','is-active','--quiet',unit);run('systemctl','is-enabled','--quiet',unit)
        for name in ['ai-model-gateway','gateway-admin','gateway-admin-agent','houmo-qwen3guard-stream-0.6b','cix-cv','houmo-sensevoice-cpu','houmo-kokoro-tts','houmo-matcha-tts','ballot-reader']:
            state=json.loads(run('docker','inspect',name))[0]['State']
            if not state['Running'] or state.get('Health',{}).get('Status')!='healthy':raise RuntimeError('Container is not healthy: '+name)
        record('containers-and-autostart')
        current='active-accelerator-real-inference'
        run('python3',str(HOME/'gateway-admin/startup_verify.py'),'--force','--source','manual')
        record('active-accelerator-real-inference')
        current='authenticated-gateway-registry'
        registry=json.loads(request('/v1/models',key))
        ids={row['id'] for row in registry['data']}
        expected_ids=set(json.loads((ROOT/'manifests/manifest.json').read_text())['models'])
        if ids!=expected_ids or len(registry['data'])!=len(expected_ids):raise RuntimeError('Gateway registry differs from the complete release model list')
        result['registry_models']=sorted(ids)
        record('authenticated-gateway-registry')
        # Built-in runtime probes cover all installed CPU/CIX-CV models, using non-private fixtures.
        code="""import json
from agent.model_store import Catalog
from agent.model_runtime import ModelRuntime
c=Catalog();r=ModelRuntime('/home/cix',c)
out=[]
for m in c.models.values():
    if 'LQ50' not in m['device']:out.append(r.probe(m))
print(json.dumps(out))
"""
        current='cpu-cix-real-inference'
        probes=json.loads(run('docker','exec','gateway-admin-agent','python','-c',code,timeout=1500))
        for probe in probes:
            if not probe.get('passed'):raise RuntimeError('Probe failed: '+str(probe.get('model','unknown')))
            record('inference:'+probe['model'])
        current='ballot-gateway-36-selections'
        ballot=json.loads(request('/v1/documents/analyze?model=democratic-eval-v1',key,(ROOT/'tests/synthetic-ballot.png').read_bytes(),'image/png'))
        pages=ballot.get('pages',[])
        if not pages or pages[0].get('selection_count')!=36:raise RuntimeError('Ballot fixture did not produce 36 selections')
        expected=json.loads((ROOT/'tests/synthetic-ballot-expected.json').read_text())
        actual=[[None]*6 for _ in range(6)]
        for row in pages[0]['selections']:actual[row['person_index']][row['dimension_index']]=row['grade_index']
        if actual!=expected:raise RuntimeError('Ballot fixture grade mapping mismatch')
        record('ballot-gateway-36-selections')
        original=(DEPLOY/'state/active-mode').read_text().strip()
        if args.all_modes:
            # On an inference failure, stop testing; do not automatically reset the device or continue loading models.
            for mode in ['copaw','spark-x1','qwen38','qwen-vl','glm-ocr','bge-m3','reranker','cosyvoice']:
                current='accelerator-mode:'+mode
                run('/usr/local/bin/modelctl','switch',mode)
                run('python3',str(HOME/'gateway-admin/startup_verify.py'),'--force','--source','manual')
                record('accelerator-mode:'+mode)
            current='restore-mode:'+original
            run('/usr/local/bin/modelctl','switch',original)
            run('python3',str(HOME/'gateway-admin/startup_verify.py'),'--force','--source','manual')
            record('restore-mode:'+original)
        if (DEPLOY/'state/active-mode').read_text().strip()=='copaw':
            current='gateway-sse-and-usage'
            data=request('/v1/chat/completions',key,{'model':'copaw-flash-9b','messages':[{'role':'user','content':'计算3+4，只回答数字。'}],'max_tokens':32,'temperature':0,'stream':True})
            if b'data: [DONE]' not in data or b'"usage"' not in data:raise RuntimeError('SSE or usage event is missing')
            record('gateway-sse-and-usage')
        result['passed']=True
    except Exception as exc:
        # Subprocess arguments never include credentials. Do not export raw container inspection or service logs.
        result['error']=(current+': '+str(exc))[:600]
        print('FAIL '+result['error'],file=sys.stderr)
    finally:
        result['finished_at']=datetime.now(timezone.utc).isoformat()
        REPORT.parent.mkdir(exist_ok=True)
        REPORT.write_text(json.dumps(result,ensure_ascii=False,indent=2))
        REPORT.chmod(0o644)
        print('Acceptance report: '+str(REPORT),flush=True)
    return 0 if result['passed'] else 1

if __name__=='__main__':sys.exit(main())
