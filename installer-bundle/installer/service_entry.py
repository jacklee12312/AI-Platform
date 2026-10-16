"""Minimal explicit-authorization bridge from the desktop to a root-owned systemd job."""
import argparse,fcntl,json,os,shutil,subprocess,sys
from pathlib import Path
from common import STATE_ROOT,PUBLIC_STATE,UNIT,read_json,atomic_json,sha256,safe_relative,no_symlink_parents,check_password,detect_device_count
from artifacts import load_catalog,source_config
from selection import validate_selected,supported_on

def verify_app(bundle):
    manifest=read_json(bundle/'installer-manifest.json')
    for row in manifest['files']:
        relative=safe_relative(row['path']);p=bundle/relative;no_symlink_parents(p,bundle)
        if p.is_symlink() or not p.is_file() or sha256(p)!=row['sha256']:raise RuntimeError('安装向导文件损坏：'+str(relative))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['install','resume','pause']);parser.add_argument('--bundle',required=True);args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit('需要系统管理员授权')
    os.environ['PATH']='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    os.umask(0o077);bundle=Path(args.bundle).resolve()
    for p,mode in [(STATE_ROOT,0o755),(STATE_ROOT/'public',0o755),(STATE_ROOT/'private',0o700)]:
        no_symlink_parents(p,Path('/'))
        if p.is_symlink():raise RuntimeError('安装状态目录不能是链接')
        p.mkdir(parents=True,exist_ok=True);p.chmod(mode)
    lock=(STATE_ROOT/'private/dispatch.lock').open('a')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        dispatch(args,bundle)
    finally:lock.close()

def dispatch(args,bundle):
    status=subprocess.run(['systemctl','is-active',UNIT],capture_output=True,text=True).stdout.strip()
    if args.action=='pause':
        if status!='active':raise RuntimeError('当前没有正在执行的安装任务')
        (STATE_ROOT/'private/pause.request').write_text('pause at next safe checkpoint\n')
        print('已请求在安全检查点暂停');return
    if status in ['active','activating','deactivating']:raise RuntimeError('已有安装任务正在运行，请回到进度页面')
    verify_app(bundle);catalog=load_catalog(bundle);release=read_json(bundle/'release.json')
    old=subprocess.run(['systemctl','is-active','ai-platform-offline-install.service'],capture_output=True,text=True).stdout.strip()
    if old in ['active','activating','deactivating']:raise RuntimeError('旧版安装任务仍在运行，请待其结束后操作')
    saved=read_json(STATE_ROOT/'private/journal.json',{})
    if saved and (saved.get('release_id')!=release['release_id'] or saved.get('installer_version')!=release['installer_version']):
        raise RuntimeError('已有不同安装器版本的任务记录，不自动覆盖或跨版本继续')
    if saved.get('status')=='installed':raise RuntimeError('本版本已经安装完成')
    request_path=STATE_ROOT/'private/request.json'
    if args.action=='install':
        if saved:raise RuntimeError('检测到已有任务，请使用继续安装')
        data=sys.stdin.buffer.read(262145)
        if len(data)>262144:raise RuntimeError('安装设置过大')
        request=json.loads(data.decode());check_password(request.get('admin_password'))
        device_count=detect_device_count()
        if request.get('device_count')!=device_count:raise RuntimeError('设备卡数在检查后发生变化，请返回检查页面重新检测')
        config=source_config(bundle,request.get('sources'))
        selected=validate_selected(catalog,request.get('selected_models'))
        if any(not supported_on(catalog,model,device_count) for model in selected):
            raise RuntimeError('所选模型不支持当前设备卡数，请返回模型选择页面')
        saved_request={'admin_password':request['admin_password'],'release_id':release['release_id'],'sources':config,'selected_models':selected,'device_count':device_count}
        if catalog.get('workflow_sha256'):
            from workflow import validate_settings
            from probe import detect_probe
            settings=request.get('workflow')
            if not isinstance(settings,dict):raise ValueError('缺少安装配置')
            settings=dict(settings,probe=dict(settings.get('probe') or {},mode='auto'))
            saved_request['workflow']=validate_settings(settings,probe_installed=detect_probe())
        atomic_json(request_path,saved_request)
    elif not saved or not request_path.exists():raise RuntimeError('没有可继续的完整任务记录')
    else:
        request=read_json(request_path)
        if request.get('device_count')!=detect_device_count():raise RuntimeError('设备卡数与原安装任务不同，不能继续')
        if any(not supported_on(catalog,model,request['device_count']) for model in request['selected_models']):
            raise RuntimeError('原任务包含不支持当前设备卡数的模型')
        data=sys.stdin.buffer.read(262145)
        if len(data)>262144:raise RuntimeError('安装设置过大')
        if data:
            update=json.loads(data.decode());config=source_config(bundle,update.get('sources'))
            if update.get('device_count')!=request['device_count']:raise RuntimeError('设备卡数与原安装任务不同，不能继续')
            request['sources']=config;atomic_json(request_path,request)
            if 'selected_models' in update and validate_selected(catalog,update['selected_models'])!=request['selected_models']:
                raise RuntimeError('继续安装时不能更改已选模型；请仅修改来源。')
    (STATE_ROOT/'private/pause.request').unlink(missing_ok=True)
    code=STATE_ROOT/'private/code';no_symlink_parents(code,STATE_ROOT)
    if code.is_symlink():raise RuntimeError('后台代码目录不能是链接')
    code.mkdir(exist_ok=True)
    for name in ['common.py','engine.py','artifacts.py','storage.py','npu_runtime.py','npu_payload.py','port_takeover.py','platform_takeover.py','runtime_overlay.py','registry.py','selection.py','verify_selected.py','version.py','workflow.py','probe.py','probe_replace.py','docker_runtime.py','components.py','probe_runner.py']:
        shutil.copyfile(bundle/'installer'/name,code/name);(code/name).chmod(0o600)
    shutil.copyfile(bundle/'release.json',code/'release.json');(code/'release.json').chmod(0o600)
    # Credentials are never command-line arguments, environment values or public status fields.
    command=['systemd-run','--unit='+UNIT,'--collect','--property=Type=exec','--property=Restart=no','--property=UMask=0077','--property=TimeoutStopSec=45','--setenv=PYTHONDONTWRITEBYTECODE=1','--setenv=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin','/usr/bin/python3','-u',str(code/'engine.py'),'--bundle',str(bundle)]
    subprocess.run(command,check=True)
    print('后台安装任务已启动；关闭窗口不会中断任务。')

if __name__=='__main__':
    try:main()
    except Exception as exc:print(str(exc),file=sys.stderr);sys.exit(1)
