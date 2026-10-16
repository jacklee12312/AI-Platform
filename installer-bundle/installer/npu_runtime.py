"""Select a working NPU userspace and render recoverable local configuration."""
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import subprocess

from common import atomic_json,atomic_text,no_symlink_parents,read_json,sha256
from npu_payload import MEMBERS,RELEASE_ID

RELATIVE='payload/home/cix/kokoro-cix/docker-compose.yml'
IMAGE='kokoro-cix:0.2.0'
CONFIGS={'kokoro':(RELATIVE,IMAGE,'kokoro-cix/docker-compose.yml'),
         'cv':('payload/home/cix/cv_deploy/compose.yaml','cix-cv-noe:1.0.0','cv_deploy/compose.yaml')}
CV_MODELS={'mobilenet_v2','inception_v3','yolov11_l','yolov11_n','yolov11n_obb',
           'yolov12_s','yolov12_m','bytetrack_s','retinaface','yolov5s_face','scrfd','arcface','yolov8l_seg'}


def trusted(path):
    path=Path(path);no_symlink_parents(path,Path('/'))
    if path.is_symlink() or not path.is_file():raise RuntimeError('NPU 适配文件不是普通文件：'+str(path))
    for item in [path,*path.parents]:
        if item.stat().st_uid!=os.geteuid() or item.stat().st_mode&0o022:
            raise RuntimeError('NPU 适配文件或目录所有者、权限不可信：'+str(item))
    return path


def runtime_directory(record,root):
    runtime=Path(record.get('runtime_dir',Path(root)/'host-runtime/kokoro'))
    parent=Path(root)/'host-runtime'
    if runtime!=parent/'kokoro' and runtime!=parent/'releases'/RELEASE_ID:
        raise RuntimeError('NPU 本机运行库路径不属于安装器管理范围')
    if set(record.get('files',{}))!={str(runtime/name) for name in MEMBERS}:
        raise RuntimeError('NPU 本机运行库文件集合不完整')
    return runtime


def record_file(root,component):return Path(root)/('private/'+component+'-runtime.json')


def effective_compose(resources,root,component='kokoro',verify_files=True):
    base=Path(resources)/CONFIGS[component][0];path=record_file(root,component)
    if not path.exists():return base
    record=read_json(trusted(path));runtime_directory(record,root)
    local=Path(root)/'private/local-configs'
    generated=Path(record.get('compose_path',local/(component+'-compose.yml')))
    if generated.parent!=local or not generated.name.startswith(component+'-compose') or generated.suffix!='.yml':
        raise RuntimeError('NPU 本机配置路径不属于安装器管理范围')
    if record.get('base_sha256')!=sha256(base) or record.get('compose_sha256')!=sha256(trusted(generated)):
        raise RuntimeError('NPU 本机适配与签名基础配置不一致')
    if verify_files:
        for name,digest in record['files'].items():
            if sha256(trusted(name))!=digest:raise RuntimeError('NPU 本机运行库校验失败')
    return generated


def apply_local(original,generated,target,home,previous=None):
    no_symlink_parents(target,home)
    if target.is_symlink() or not target.is_file():raise RuntimeError('NPU 本机配置路径不安全')
    actual=target.read_bytes()
    if actual==generated.read_bytes():return
    allowed=[original.read_bytes()]
    if previous is not None:allowed.append(previous)
    if actual not in allowed:raise RuntimeError('已有配置不同，不能自动应用 NPU 适配')
    atomic_text(target,generated.read_text(),0o644)


def apply_record(resources,root,component,target,home):
    generated=effective_compose(resources,root,component,verify_files=False)
    previous=None;path=record_file(root,component)
    if path.exists():
        record=read_json(trusted(path));backup=record.get('previous_compose')
        if backup:
            source=Path(backup['path'])
            if not source.is_relative_to(Path(root)/'private/local-configs') or sha256(trusted(source))!=backup['sha256']:
                raise RuntimeError('NPU 配置恢复备份校验失败')
            previous=source.read_bytes()
    apply_local(Path(resources)/CONFIGS[component][0],generated,target,home,previous)


def render(base,runtime):
    if base.count('    volumes:\n')!=1 or base.count('    environment:\n')!=1:raise RuntimeError('NPU 签名基础配置结构无法识别')
    base=base.replace('    volumes:\n','    volumes:\n'
                      f'    - {runtime}/lib:/opt/cix-host-lib:ro\n'
                      f'    - {runtime}/libnoe:/usr/local/lib/python3.11/site-packages/libnoe:ro\n')
    return base.replace('    environment:\n','    environment:\n'
                        '      LD_LIBRARY_PATH: /opt/cix-host-lib:/usr/local/lib\n'
                        '      AIPU_LIB_PATH: /opt/cix-host-lib/libaipu_driver.so\n')


SMOKE="""import os,sys,numpy as np
from NOE_Engine import EngineInfer
from libnoe import NOE_TENSOR_TYPE_OUTPUT
m=EngineInfer(sys.argv[1]);m._create_job()
for index,(d,t) in enumerate(zip(m.in_tensor_desc,m.input_type)):
    assert 0<d.size<=64*1024**2
    data=np.zeros(d.size//np.dtype(t).itemsize,dtype=t).tobytes()
    assert m.npu.noe_load_tensor(m.job_id,index,data)==0
assert m.npu.noe_job_infer_sync(m.job_id,10000)==0
assert m.out_tensor_desc
for index,(d,t) in enumerate(zip(m.out_tensor_desc,m.output_type)):
    status,data=m.npu.noe_get_tensor(m.job_id,NOE_TENSOR_TYPE_OUTPUT,index)
    assert status==0 and len(data)==d.size and d.size>0
    assert np.isfinite(np.frombuffer(data,dtype=t)).all()
assert m.npu.noe_clean_job(m.job_id)==0
m.clean(); print('AI_PLATFORM_NPU_INFERENCE_OK',flush=True);os._exit(0)
"""


def probe(extra=(),image=IMAGE,model=None):
    name='ai-platform-npu-check-'+secrets.token_hex(8)
    code=SMOKE if model else 'import os; from libnoe import NPU; n=NPU(); status=n.noe_init_context(); print(status,flush=True); os._exit(0 if status==0 else 1)'
    command=['docker','run','--rm','--pull','never','--name',name,'--label','com.ai-platform.runtime-check=true',
             '--network','none','--device','/dev/aipu',*extra,'--entrypoint','python',image,'-c',code]
    if model:command.append(model)
    try:
        result=subprocess.run(command,capture_output=True,text=True,timeout=90)
        if model and 'AI_PLATFORM_NPU_INFERENCE_OK' not in result.stdout:result.returncode=1
        return result
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command,1,stdout='',stderr='NPU 实际推理检查超时')
    finally:
        subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=20)


def mounts(runtime):
    return ['-v',str(runtime/'lib')+':/opt/cix-host-lib:ro',
            '-v',str(runtime/'libnoe')+':/usr/local/lib/python3.11/site-packages/libnoe:ro',
            '-e','LD_LIBRARY_PATH=/opt/cix-host-lib:/usr/local/lib',
            '-e','AIPU_LIB_PATH=/opt/cix-host-lib/libaipu_driver.so']


def prepare(installer):
    # The private runtime must survive removal of the installer and support
    # models added later through ModelDeck, even when none are selected today.
    from npu_payload import install
    result=install(installer.bundle,installer.root,installer.event)
    installer.npu_prepared=result
    return result


def configure(installer):
    # Recheck even within one worker: a missing/corrupt snapshot must be restored
    # before testing it, including after a failed or interrupted earlier start.
    prepared=prepare(installer)
    for component in CONFIGS:configure_component(installer,component,prepared)


def configure_component(installer,component,prepared=None):
    runtime,manifest=prepared or prepare(installer)
    root=installer.root;relative,image,installed=CONFIGS[component]
    label='Kokoro' if component=='kokoro' else 'CIX CV'
    original=installer.resources/relative;target=installer.home/installed
    current=effective_compose(installer.resources,root,component,verify_files=False)
    if component=='kokoro' and 'kokoro-82m' in installer.selected:
        model_dir=installer.home/'kokoro-cix/model';model='/models/bert2.cix'
    elif CV_MODELS.intersection(installer.selected):
        model_dir=installer.home/'cv_deploy/models';model='/models/'+sorted(CV_MODELS.intersection(installer.selected))[0]+'/model.cix'
    elif 'kokoro-82m' in installer.selected:
        model_dir=installer.home/'kokoro-cix/model';model='/models/bert2.cix'
    else:model=None
    model_mount=['-v',str(model_dir)+':/models:ro'] if model else []
    verified='实际 NPU 推理' if model else 'NPU 设备初始化；补装模型时仍需实际推理验收'
    if current!=original:
        try:
            effective_compose(installer.resources,root,component)
            old=runtime_directory(read_json(record_file(root,component)),root)
            if probe(mounts(old)+model_mount,image,model).returncode==0:
                apply_record(installer.resources,root,component,target,installer.home)
                installer.event(label+' 已有环境通过'+verified+'，复用现有适配');return
        except (OSError,RuntimeError):pass
        installer.event(label+' 已有运行库缺失或不兼容，自动切换至包内运行库')
    elif probe(model_mount,image,model).returncode==0:
        installer.event(label+' 原镜像通过'+verified+'，无需调整');return
    check=probe(mounts(runtime)+model_mount,image,model)
    if check.returncode:
        raise RuntimeError(label+' 已补齐离线运行库，但实际 NPU 推理仍失败；请检查底层 NPU 设备和内核驱动。'+check.stderr[-600:])
    local=root/'private/local-configs';local.mkdir(exist_ok=True,mode=0o700)
    previous=None
    if current!=original:
        previous=current.read_bytes()
        backup=local/(component+'-compose.previous-'+secrets.token_hex(8)+'.yml')
        atomic_text(backup,previous.decode('utf-8'),0o600)
        shutil.copyfile(record_file(root,component),backup.with_suffix('.json'))
    if target.is_symlink() or target.read_bytes() not in [original.read_bytes(),previous]:
        raise RuntimeError(label+' 已有配置不同，不能自动应用 NPU 适配')
    baseline=local/(component+'-compose.original.yml')
    if not baseline.exists():shutil.copyfile(original,baseline)
    text=render(original.read_text(),runtime)
    generated=local/(component+'-compose-'+hashlib.sha256(text.encode()).hexdigest()+'.yml')
    if generated.exists() and trusted(generated).read_text()!=text:raise RuntimeError('已有 NPU 本机配置摘要冲突')
    if not generated.exists():atomic_text(generated,text,0o644)
    record={'schema':1,'kind':'bundled-offline-userspace','runtime_release_id':manifest['release_id'],
            'runtime_dir':str(runtime),'host_noe_version':'3.1.2','base_sha256':sha256(original),
            'compose_sha256':sha256(generated),'compose_path':str(generated),'image':image,
            'files':{str(runtime/r['path']):r['sha256'] for r in manifest['files']},
            'actual_npu_inference':'passed' if model else 'deferred-until-model-installation'}
    if previous is not None:record['previous_compose']={'path':str(backup),'sha256':sha256(backup)}
    atomic_json(record_file(root,component),record)
    apply_record(installer.resources,root,component,target,installer.home)
    installer.event(label+' 已自动安装并启用离线 NPU 3.1.2 运行库，'+verified+'通过')
