"""Generate formal v1 hardware variants and locked model source mappings."""
from pathlib import Path
from copy import deepcopy
import json,hashlib,yaml

R=Path(r'F:\duoliuer\AI-Platform-1.0.0');B=R/'AI-Platform-Installer-1.0.0';S=R/'support-files/files';P=S/'payload/home/cix'
MID='qwen3.6-35b-a3b';BASE='/home/cix/cix_telecom_projects/dual-core'
REPOS={1:'aistore/qwen3-8-27b',2:'aistore/qwen3-8-27b-2'}
def read(p):return json.loads(p.read_text('utf-8'))
def write(p,x):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def entry(j,mid):return j['models'][mid] if isinstance(j['models'],dict) else next(r for r in j['models'] if r['id']==mid)
def source(row,repo,count=None):
    out={'registry':'http://36.111.34.121:15436','repository':repo,'reference':'v1','resolve_tag':True,'manifest_digest':None,'blob_digest':'sha256:'+row['sha256'],'blob_size':row['size'],'format':'blob','files':{row['path']:None}}
    if count:out['device_count']=count
    return out

cat=read(B/'catalog/release-catalog.json');old36=[r for r in cat['resources'] if MID in r['models']]
cat['resources']=[r for r in cat['resources'] if MID not in r['models']]
q36=[]
for f in read(R/'qwen36-files.json'):
    q36.append({'path':'payload'+BASE+'/qwen36_35B/'+f['name'],'size':f['size'],'sha256':f['sha256'],'category':'models','models':[MID],'device_count':2})
cat['resources']+=q36
if MID not in cat['models']:cat['models'].insert(cat['models'].index('qwen3.8-27b')+1,MID)
cat['model_device_counts']={MID:[2],'qwen3.8-27b':[1,2]}
cat['components']=[r for r in cat['components'] if r['id']!=MID]
cat['components'].append({'id':MID,'kind':'model','files':[r['path'] for r in q36],'requires':['platform','runtime','images'],'remote_status':'oci-upload-pending'})
for component in cat['components']:
    if component['id']=='qwen3.8-27b':component['remote_status']='oci-upload-pending'

config=read(B/'sources.json')
config['file_urls']={p:v for p,v in config['file_urls'].items() if '/qwen3.8-27b/' not in p}
config['oci_artifacts']={n:s for n,s in config['oci_artifacts'].items() if not n.startswith(('qwen38-','qwen36-'))}
q38=[r for r in cat['resources'] if 'qwen3.8-27b' in r['models']]
for count,repo in REPOS.items():
    for row in q38:
        if row.get('device_count',count)!=count:continue
        key=f'qwen38-{count}-'+('mmproj' if '/mmproj' in row['path'] else 'model')
        config['oci_artifacts'][key]=source(row,repo,count)
for row in q36:
    key='qwen36-2-'+('mmproj' if '/mmproj' in row['path'] else 'model')
    config['oci_artifacts'][key]=source(row,'aistore/qwen3.6-35b-a3b-2',2)
for name in ('sources.json','sources.example.json'):write(B/name,config)

# A container reuses the pinned Debian 12 Python runtime image, with the accepted vLLM environment mounted read-only.
compose_path=P/'houmo-docker-deployment/compose.yaml';compose=yaml.safe_load(compose_path.read_text('utf-8'))
sv=deepcopy(compose['services']['cosyvoice']);template=compose['services']['qwen38']
sv.update(profiles=['qwen36'],container_name='houmo-qwen36-35b-a3b',working_dir=BASE,devices=deepcopy(template['devices']))
sv['environment']={'TZ':'Asia/Shanghai','HOUMO_API_KEY':'${HOUMO_API_KEY}','HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','PIP_NO_INDEX':'1','PYTHONDONTWRITEBYTECODE':'1','HOME':'/tmp','XDG_CACHE_HOME':'/tmp/.cache','TCIM_RUNTIME_PATH':BASE+'/houmo_tcim_runtime_xh2_linux_aarch64-1.4.0','LD_LIBRARY_PATH':BASE+'/.venv/lib/python3.11/site-packages/tcim_lite/lib:'+BASE+'/houmo_tcim_runtime_xh2_linux_aarch64-1.4.0/lib'}
sv['volumes']=[{'type':'bind','source':BASE,'target':BASE,'read_only':True},
    {'type':'bind','source':'/usr/local/houmo-drv-xh2_v1.4.0','target':'/usr/local/houmo-drv-xh2_v1.4.0','read_only':True}]
for vol in compose['services']['cosyvoice']['volumes']:
    if '/modeldeck-runtime/lib/' in vol['source']:sv['volumes'].append(deepcopy(vol))
sv['entrypoint']=[BASE+'/.venv/bin/vllm']
projector=next(Path(r['path']).name for r in q36 if '/mmproj' in r['path'])
sv['command']=['serve',BASE+'/qwen36_35B/','--port','17712','--host','0.0.0.0','--no-enable-prefix-caching','--served-model-name',MID,'--max-num-batched-tokens','262144','--max-num-seqs','1','--max-model-len','262144','--enable-auto-tool-choice','--tool-call-parser','qwen3_coder','--additional-config',json.dumps({'visual_model_path':BASE+'/qwen36_35B/'+projector,'device_ids':[0,1],'vl_device_ids':[0]}),'--default-chat-template-kwargs','{"enable_thinking":false}','--api-key','${HOUMO_API_KEY}']
sv['healthcheck']=deepcopy(template['healthcheck']);sv['healthcheck']['test'][-1]='http://127.0.0.1:17712/health'
compose['services']['qwen36']=sv
compose_path.write_text(yaml.safe_dump(compose,allow_unicode=True,sort_keys=False),encoding='utf-8',newline='\n')

for count,root in [(1,S/'variants/single'),(2,P)]:
    baseline_path=root/'gateway-admin/agent/runtime_baseline.json';baseline=read(baseline_path)
    baseline['components']['hlievllm-qwen36']={'version':'0.21.0rc1+cpu / 0825hsc','image':'houmo-hlievllm-cosyvoice:1.4.0','runtime':'Houmo TCIM 1.4.0 / plugin 1.4.0-0825hsc','driver':'XH2 v1.4.0'}
    if count==2:
        baseline['models'][MID]={'model_version':'v1.4.0-20260716+mmproj-v1.5.0-20260824','service_component':'hlievllm-qwen36','compose_service':'qwen36','gateway_service':'qwen36','port':17712,'context_size':262144,'compose_context':262144,'concurrency':1,'modes':['qwen36'],'device':'LQ50','gateway_enabled':True,'hardware_variant':'dual'}
    write(baseline_path,baseline)
    agent_path=root/'gateway-admin/agent/model_catalog.json';agent=read(agent_path)
    e=entry(agent,'qwen3.8-27b');e['files']=[]
    for row in q38:
        if row.get('device_count',count)==count:
            e['files'].append({'target':row['path'].removeprefix('payload/home/cix/'),'size':row['size'],'sha256':row['sha256'],'oci_source':source(row,REPOS[count])})
    if count==2:
        agent['models']=[r for r in agent['models'] if r['id']!=MID]
        q=deepcopy(e);q.update(id=MID,component='hlievllm-qwen36',service='qwen36',container='houmo-qwen36-35b-a3b',min_ram_gb=8)
        q['files']=[{'target':r['path'].removeprefix('payload/home/cix/'),'size':r['size'],'sha256':r['sha256'],'oci_source':source(r,'aistore/qwen3.6-35b-a3b-2')} for r in q36]
        agent['models'].append(q)
    write(agent_path,agent)
    gp=root/'ai-model-gateway/config/models.json';g=read(gp)
    if count==2:
        g['models']=[r for r in g['models'] if r['id']!=MID];q=deepcopy(entry(g,'qwen3.8-27b'))
        q.update(id=MID,base_url='http://qwen36:17712',upstream_model=MID,modes=['qwen36']);q['metadata'].update(model_version='v1.4.0-20260716',service_version='HLIEvLLM-1.4.0-0825hsc',service_port=17712,hardware_variant='dual');g['models'].append(q)
    write(gp,g)
    cp=root/'gateway-admin/admin_app/capability_catalog.json';cc=read(cp)
    # Repair legacy malformed Chinese in the Qwen3.8 multimodal description.
    e=entry(cc,'qwen3.8-27b');e.update(name='Qwen3.8 27B 多模态',category='文本对话',summary='文本对话、静态图片理解及流式输出；安装时执行文本和图片验收。',inputs=['OpenAI 兼容 messages；图片使用 image_url 数据 URL。'],outputs=['JSON 或 SSE 流式响应；Token 统计以实际响应为准。'],streaming=['stream=true 使用 SSE，成功结束应收到 [DONE]。'],boundaries=['仅验收文本与静态图片；视频和音频输入未纳入验收。'],verification={'status':'installation-verification-required','scope':'每台新设备安装后执行真实文本与图片推理。','sources':['installer']})
    if count==2:
        cc['models']=[r for r in cc['models'] if r['id']!=MID];q=deepcopy(e);q.update(id=MID,name='Qwen3.6 35B A3B 双卡多模态',summary='双卡 MoE 文本与图片推理，声明上下文 256K；运行时为 HLIEvLLM。')
        q['baseline']['modes']=['qwen36'];q['baseline']['metadata'].update(model_version='v1.4.0-20260716',service_version='HLIEvLLM-1.4.0-0825hsc');cc['models'].append(q)
    write(cp,cc)
# Single-card compose still declares the dormant dual service so shared lifecycle commands can name it.
# It is never created unless selected, and model selection forbids it on a single-card device.
single=S/'variants/single/houmo-docker-deployment/compose.yaml';v=yaml.safe_load(single.read_text('utf-8'));v['services']['qwen36']=deepcopy(sv);single.write_text(yaml.safe_dump(v,allow_unicode=True,sort_keys=False),encoding='utf-8',newline='\n')
write(B/'catalog/release-catalog.json',cat)
print('Configured 27 models, Qwen3.8 single/dual OCI sources and Qwen3.6 dual container')
