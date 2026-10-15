#!/usr/bin/env python3
"""Real inference acceptance for a selected-model fresh install."""
from __future__ import annotations

import argparse
import json
import importlib.util
import re
from pathlib import Path
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

HOME=Path('/home/cix')
DEPLOY=HOME/'houmo-docker-deployment'
REPORT=DEPLOY/'state/offline-acceptance.json'
MODES={'copaw-flash-9b':'copaw','spark-x1-4b':'spark-x1','bge-m3':'bge-m3',
       'bge-reranker-v2-m3':'reranker','cosyvoice3-0.5b':'cosyvoice',
       'qwen3-vl-8b':'qwen-vl','glm-ocr-0.9b':'glm-ocr','qwen3.8-27b':'qwen38','qwen3.6-35b-a3b':'qwen36'}
CPU_CONTAINERS={'sensevoice':'houmo-sensevoice-cpu','kokoro-82m':'houmo-kokoro-tts',
                'matcha-zh':'houmo-matcha-tts'}
CV={'mobilenet_v2','inception_v3','yolov11_l','yolov11_n','yolov11n_obb',
    'yolov12_s','yolov12_m','bytetrack_s','retinaface','yolov5s_face','scrfd','arcface','yolov8l_seg'}


def run(*command,timeout=1500):
    return subprocess.run([str(x) for x in command],check=True,capture_output=True,text=True,timeout=timeout).stdout


def check_container(name):
    state=json.loads(run('docker','inspect',name,timeout=20))[0]['State']
    if not state['Running'] or state.get('Health',{}).get('Status')!='healthy':
        raise RuntimeError('容器未健康运行：'+name)


def request(path,key,body=None,content_type='application/json'):
    headers={'Authorization':'Bearer '+key,'Content-Type':content_type}
    req=urllib.request.Request('http://127.0.0.1:17700'+path,data=body,headers=headers)
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=300) as response:
        return response.read()


def verify_qwen_image(key,model='qwen3.8-27b'):
    spec=importlib.util.spec_from_file_location('qwen38_runtime_probes', HOME/'gateway-admin/agent/runtime_probes.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    body={'model':model,'messages':[{'role':'user','content':[
        {'type':'image_url','image_url':{'url':module.digit_image()}},
        {'type':'text','text':'Read the single digit shown in the image. Reply with only that digit.'}
    ]}],'temperature':0,'max_tokens':64,'stream':False}
    answer=json.loads(request('/v1/chat/completions',key,json.dumps(body).encode()))
    content=answer['choices'][0]['message']['content']
    if re.findall(r'\d+',content)!=['7']:raise RuntimeError(model+' image inference did not return 7')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run-id',required=True);parser.add_argument('--selected-json',required=True);parser.add_argument('--resources',type=Path,required=True);parser.add_argument('--device-count',type=int,required=True,choices=(1,2))
    args=parser.parse_args();selected=json.loads(args.selected_json)
    if not isinstance(selected,list) or not selected or any(not isinstance(x,str) for x in selected):
        raise SystemExit('无效的模型验收选择')
    result={'run_id':args.run_id,'scope':'selected-models','selected_models':selected,'device_count':args.device_count,
            'started_at':datetime.now(timezone.utc).isoformat(),'checks':[],'passed':False}
    stage='初始化'
    def record(name):
        result['checks'].append({'name':name,'passed':True});print('PASS '+name,flush=True)
    try:
        stage='服务与容器'
        units=['docker','ai-model-gateway','gateway-admin']
        if any(model in MODES for model in selected):units.append('houmo-ai')
        for unit in units:run('systemctl','is-active','--quiet',unit,timeout=20);run('systemctl','is-enabled','--quiet',unit,timeout=20)
        for name in ['ai-model-gateway','gateway-admin','gateway-admin-agent']:
            check_container(name)
        for model in selected:
            if model in CPU_CONTAINERS:check_container(CPU_CONTAINERS[model])
        if CV.intersection(selected):check_container('cix-cv')
        record('selected-containers-and-autostart')
        stage='网关模型清单'
        env=dict(line.split('=',1) for line in (DEPLOY/'.env').read_text().splitlines() if '=' in line)
        key=env['HOUMO_API_KEY'].strip("'\"")
        registry=json.loads(request('/v1/models',key))
        ids={row['id'] for row in registry['data']}
        if not set(selected).issubset(ids):raise RuntimeError('网关未登记全部所选模型')
        result['registry_models']=sorted(ids);record('selected-gateway-registry')
        stage='CPU / CIX 模型真实推理'
        probe_ids=[model for model in selected if model not in MODES]
        if probe_ids:
            code="""import json,sys
from agent.model_store import Catalog
from agent.model_runtime import ModelRuntime
c=Catalog();r=ModelRuntime('/home/cix',c)
print(json.dumps([r.probe(c.models[mid]) for mid in json.loads(sys.argv[1])]))
"""
            probes=json.loads(run('docker','exec','gateway-admin-agent','python','-c',code,json.dumps(probe_ids),timeout=3600))
            if {row.get('model') for row in probes}!=set(probe_ids):raise RuntimeError('CPU / CIX 验收结果不完整')
            for probe in probes:
                if not probe.get('passed'):raise RuntimeError('真实推理失败：'+str(probe.get('model')))
                record('inference:'+probe['model'])
        modes=[MODES[model] for model in selected if model in MODES]
        if modes:
            stage='加速卡模型真实推理';original=(DEPLOY/'state/active-mode').read_text().strip()
            for mode in modes:
                run('/usr/local/bin/modelctl','switch',mode)
                run('python3',HOME/'gateway-admin/startup_verify.py','--force','--source','manual')
                record('accelerator-mode:'+mode)
                if mode in ('qwen38','qwen36'):
                    model='qwen3.8-27b' if mode=='qwen38' else 'qwen3.6-35b-a3b'
                    stage=model+' 图片推理'
                    verify_qwen_image(key,model)
                    record('multimodal:'+model)
                    stage='加速卡模型真实推理'
            run('/usr/local/bin/modelctl','switch',original)
            run('python3',HOME/'gateway-admin/startup_verify.py','--force','--source','manual')
            record('restore-mode:'+original)
        result['passed']=True
    except Exception as exc:
        result['error']=(stage+': '+str(exc))[:600]
        print('FAIL '+result['error'],file=sys.stderr)
    finally:
        result['finished_at']=datetime.now(timezone.utc).isoformat()
        REPORT.parent.mkdir(exist_ok=True);REPORT.write_text(json.dumps(result,ensure_ascii=False,indent=2));REPORT.chmod(0o644)
    return 0 if result['passed'] else 1


if __name__=='__main__':sys.exit(main())
