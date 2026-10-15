"""Apply v1 source integration to the isolated release staging directory."""
from pathlib import Path
import json, copy, shutil
import yaml

ROOT=Path(r'F:\duoliuer\AI-Platform-1.0.0')
B=ROOT/'AI-Platform-Installer-1.0.0'
S=ROOT/'support-files/files'
P=S/'payload/home/cix'
MID='qwen3.6-35b-a3b'

def edit(path,old,new):
    text=path.read_text(encoding='utf-8')
    if old not in text:raise RuntimeError('Missing anchor: '+str(path)+' '+old[:60])
    path.write_text(text.replace(old,new),encoding='utf-8',newline='\n')

def read(path):return json.loads(path.read_text(encoding='utf-8'))
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')

edit(B/'installer/common.py',"0.2.3 预览","1.0.0")
edit(B/'installer/common.py',"'qwen38':'qwen3.8-27b'","'qwen38':'qwen3.8-27b','qwen36':'qwen3.6-35b-a3b'")
edit(B/'installer/common.py',"len(m['models'])==26","len(m['models'])==27")
edit(B/'installer/common.py',"26 个可选模型及服务","27 个可选模型及服务（Qwen3.6 仅双卡）")
for rel in ['engine.py','verify_selected.py']:
    edit(B/'installer'/rel,"'qwen3.8-27b':'qwen38'","'qwen3.8-27b':'qwen38','qwen3.6-35b-a3b':'qwen36'")
edit(B/'installer/engine.py',"('cosyvoice-runtime.tar.gz','')","('qwen36-vllm-runtime.tar.gz',''),('cosyvoice-runtime.tar.gz','')")
edit(B/'installer/engine.py',"if 'qwen3.8-27b' in self.selected:expected.add('multimodal:qwen3.8-27b')","for model in ('qwen3.8-27b','qwen3.6-35b-a3b'):\n            if model in self.selected:expected.add('multimodal:'+model)")
edit(B/'installer/verify_selected.py',"def verify_qwen38_image(key):","def verify_qwen_image(key,model='qwen3.8-27b'):")
edit(B/'installer/verify_selected.py',"body={'model':'qwen3.8-27b'","body={'model':model")
edit(B/'installer/verify_selected.py',"'Qwen3.8 image inference did not return 7'","model+' image inference did not return 7'")
edit(B/'installer/verify_selected.py',"if mode=='qwen38':\n                    stage='Qwen3.8 图片推理'\n                    verify_qwen38_image(key)\n                    record('multimodal:qwen3.8-27b')","if mode in ('qwen38','qwen36'):\n                    model='qwen3.8-27b' if mode=='qwen38' else 'qwen3.6-35b-a3b'\n                    stage=model+' 图片推理'\n                    verify_qwen_image(key,model)\n                    record('multimodal:'+model)")
edit(B/'installer/artifacts.py','AI-Platform-Installer/0.2.3','AI-Platform-Installer/1.0.0')

w=B/'installer/wizard.py'
edit(w,'from selection import selected_catalog,validate_selected','from selection import selected_catalog,validate_selected,supported_on')
edit(w,'plan_sources,remote_candidates,SourceError','plan_sources,remote_candidates,oci_source,SourceError')
edit(w,"    for model,source in config.get('oci_artifacts',{}).items():\n        paths=remote.keys() & source['files'].keys()\n        if paths:\n            checks.append(('oci',model,source));covered.update(paths)","    seen=set()\n    for path,row in remote.items():\n        chosen=oci_source(config,row)\n        if chosen:\n            name,source=chosen;covered.add(path)\n            if name not in seen:checks.append(('oci',name,source));seen.add(name)")
edit(w,"from registry import RegistryClient\n","from registry import RegistryClient,RegistryError\n")
edit(w,"        except Exception:\n            return {'name':name+' 远端模型','ok':False,\n                    'detail':'仓库或模型文件暂不可用，可能仍在上传或目标机网络不通；完成后重新检查。','warning':False}","        except Exception as exc:\n            detail=str(exc) if isinstance(exc,RegistryError) else '仓库或模型文件暂不可用，请检查网络后重试。'\n            return {'name':name+' 远端模型','ok':False,'detail':detail,'warning':False}")
edit(w,"[('26','注册模型及服务')","[(str(len(self.catalog['models'])),'注册模型及服务')")
edit(w,'Qwen3.8 会按检测到的单卡或双卡设备自动选择权重。','Qwen3.8 自动选择单卡或双卡多模态权重；Qwen3.6-35B-A3B 仅支持双卡。')
edit(w,'下载对应的 Qwen3.8 权重','自动选择对应模型版本')
edit(w,"check.setCheckState(Qt.Checked)","check.setCheckState(Qt.Checked if supported_on(self.catalog,model,self.device_count) else Qt.Unchecked)\n            if not supported_on(self.catalog,model,self.device_count):check.setFlags(Qt.NoItemFlags)")
edit(w,"QTableWidgetItem(model)","QTableWidgetItem(model+('（仅双卡）' if self.catalog.get('model_device_counts',{}).get(model)==[2] else ''))")
edit(w,"label('已选全部 26 项','subtitle')","label('已按设备卡数选择可用模型','subtitle')")
edit(w,"for row in range(self.models_table.rowCount()):self.models_table.item(row,0).setCheckState(Qt.Checked if checked else Qt.Unchecked)","for row,model in enumerate(self.catalog['models']):\n            self.models_table.item(row,0).setCheckState(Qt.Checked if checked and supported_on(self.catalog,model,self.device_count) else Qt.Unchecked)")
edit(w,"    def model_size_label(self,model):\n","    def model_size_label(self,model):\n        if self.device_count is not None and not supported_on(self.catalog,model,self.device_count):return '仅双卡可用'\n")
edit(w,"            for index,model in enumerate(self.catalog['models']):\n                self.models_table.item(index,2)","            for index,model in enumerate(self.catalog['models']):\n                check=self.models_table.item(index,0)\n                enabled=supported_on(self.catalog,model,self.device_count)\n                check.setFlags(Qt.ItemIsEnabled|Qt.ItemIsUserCheckable if enabled else Qt.NoItemFlags)\n                if not enabled:check.setCheckState(Qt.Unchecked)\n                self.models_table.item(index,2)")
# A library is reusable across releases. Every selected file is still checked against the signed catalog.
edit(w,"try:return json.loads(marker.read_text(encoding='utf-8')).get('release_id')==release_id","try:return isinstance(json.loads(marker.read_text(encoding='utf-8')).get('files'),list)")
edit(w,'同版模型库','完整模型库')
edit(w,'同版 AI-Platform-Model-Library','AI-Platform-Model-Library')
edit(w,'同版 model-library.json','model-library.json')

# All lifecycle allowlists must include the newly supported accelerator mode.
for rel in ['gateway-admin/startup_verify.py','gateway-admin/admin_app/main.py','gateway-admin/agent/diagnostics.py','gateway-admin/agent/evaluation_runtime.py','gateway-admin/agent/model_runtime.py','scripts-placeholder']:
    path=P/rel
    if path.exists():
        text=path.read_text('utf-8').replace('"qwen38",','"qwen38", "qwen36",').replace("'qwen38',","'qwen38', 'qwen36',")
        # Compose profile lists require their own --profile flag.
        text=text.replace("'--profile', 'qwen38', 'qwen36',","'--profile', 'qwen38', '--profile', 'qwen36',")
        path.write_text(text,encoding='utf-8',newline='\n')
edit(S/'scripts/offline_install.py',"'qwen38',","'qwen38','qwen36',")
ctl=P/'houmo-docker-deployment/modelctl'
edit(ctl,'qwen38 qwen-vl','qwen38 qwen36 qwen-vl')
edit(ctl,'--profile qwen38 --profile qwen-vl','--profile qwen38 --profile qwen36 --profile qwen-vl')
edit(ctl,"        qwen38) printf '%s\\n' houmo-qwen38-27b ;;","        qwen38) printf '%s\\n' houmo-qwen38-27b ;;\n        qwen36) printf '%s\\n' houmo-qwen36-35b-a3b ;;")
edit(ctl,"'qwen3.8-27b':'qwen38'","'qwen3.8-27b':'qwen38','qwen3.6-35b-a3b':'qwen36'")
for rel in ['gateway-admin/agent/main.py','gateway-admin/agent/device_recovery.py']:
    edit(P/rel,'"qwen38": "houmo-qwen38-27b",','"qwen38": "houmo-qwen38-27b",\n    "qwen36": "houmo-qwen36-35b-a3b",')
edit(P/'gateway-admin/agent/main.py','"qwen38": {"qwen38"},','"qwen38": {"qwen38"},\n    "qwen36": {"qwen36"},')
edit(P/'gateway-admin/agent/main.py','"qwen38": 17707,','"qwen38": 17707, "qwen36": 17712,')
edit(P/'gateway-admin/agent/main.py','"qwen38": "qwen3.8-27b",','"qwen38": "qwen3.8-27b",\n    "qwen36": "qwen3.6-35b-a3b",')
rec=P/'gateway-admin/agent/device_recovery.py'
edit(rec,'"houmo-qwen38-27b": "qwen3.8-27b",','"houmo-qwen38-27b": "qwen3.8-27b",\n    "houmo-qwen36-35b-a3b": "qwen3.6-35b-a3b",')
edit(rec,"'houmo-copaw', 'houmo-qwen38-27b', 'houmo-spark-x1'","'houmo-copaw', 'houmo-qwen38-27b', 'houmo-qwen36-35b-a3b', 'houmo-spark-x1'")
edit(rec,"'houmo-qwen38-27b': (17707, 'qwen3.8-27b'),","'houmo-qwen38-27b': (17707, 'qwen3.8-27b'),\n            'houmo-qwen36-35b-a3b': (17712, 'qwen3.6-35b-a3b'),")
edit(rec,"if name == 'houmo-qwen38-27b' and qwen38_variant in ('single', 'dual'):","if name == 'houmo-qwen36-35b-a3b' or (name == 'houmo-qwen38-27b' and qwen38_variant in ('single', 'dual')):")
edit(P/'gateway-admin/agent/runtime_probes.py',"    elif name=='houmo-qwen3-vl-8b':","    elif name=='houmo-qwen36-35b-a3b':\n        model,port='qwen3.6-35b-a3b',17712\n        content=[{'type':'image_url','image_url':{'url':digit_image()}},{'type':'text','text':'Read the single digit shown in the image. Reply with only that digit.'}]\n    elif name=='houmo-qwen3-vl-8b':")
edit(P/'ai-model-gateway/app/runtime_safety.py',"17707:'houmo-qwen38-27b',","17707:'houmo-qwen38-27b',17712:'houmo-qwen36-35b-a3b',")

# Bind the changed source into the existing pinned images, exactly like the existing probe overlays.
for rel,service,files in [
    ('gateway-admin/compose.yaml','agent',['main.py','diagnostics.py','evaluation_runtime.py','model_runtime.py','model_store.py','registry.py']),
    ('gateway-admin/compose.yaml','admin',['main.py']),
    ('ai-model-gateway/compose.yaml','gateway',['runtime_safety.py'])]:
    path=P/rel;data=yaml.safe_load(path.read_text('utf-8'))
    folder={'agent':'agent','admin':'admin_app','gateway':'app'}[service]
    host='ai-model-gateway' if service=='gateway' else 'gateway-admin'
    for file in files:data['services'][service]['volumes'].append(f'/home/cix/{host}/{folder}/{file}:/app/{folder}/{file}:ro')
    path.write_text(yaml.safe_dump(data,allow_unicode=True,sort_keys=False),encoding='utf-8',newline='\n')

# The installed model manager uses the same digest-pinned OCI client as the installer.
shutil.copyfile(B/'installer/registry.py',P/'gateway-admin/agent/registry.py')
store=P/'gateway-admin/agent/model_store.py'
edit(store,"                parsed = urlsplit(item['url'])\n                if parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.username or parsed.password:\n                    raise ValueError('下载地址不在发布目录允许的来源中')", "                if 'oci_source' in item:\n                    from .registry import validate_sources\n                    def allowed(url,allow_http):\n                        if url!='http://36.111.34.121:15436':raise ValueError('模型仓库不在发布来源中')\n                    validate_sources({'allow_http':True,'oci_artifacts':{'model':item['oci_source']}},safe_relative,allowed,ValueError)\n                    source=item['oci_source']\n                    if source['format']!='blob' or source['blob_digest']!='sha256:'+item['sha256'] or source['blob_size']!=item['size']:\n                        raise ValueError('模型 OCI 来源与固定摘要不符')\n                else:\n                    parsed = urlsplit(item['url'])\n                    if parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.username or parsed.password:\n                        raise ValueError('下载地址不在发布目录允许的来源中')")
edit(store,"            with self.opener(Request(item['url'], headers=headers), timeout=40) as response:","            opener=self.opener;url=item.get('url')\n            if 'oci_source' in item:\n                from .registry import RegistryClient\n                client=RegistryClient(item['oci_source'],40);client.verify_manifest()\n                opener=client.open;url=client.blob_url()\n            with opener(Request(url, headers=headers), timeout=40) as response:")
print('Source integration prepared')
