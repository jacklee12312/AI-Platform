"""Minimal explicit-authorization bridge from the desktop to a root-owned systemd job."""
import argparse,ast,fcntl,json,os,shutil,subprocess,sys,tempfile
from pathlib import Path
from common import STATE_ROOT,PUBLIC_STATE,UNIT,read_json,atomic_json,sha256,safe_relative,no_symlink_parents,check_password,detect_device_count
from artifacts import load_catalog,source_config
from selection import validate_selected,supported_on

def verify_app(bundle):
    manifest=read_json(bundle/'installer-manifest.json')
    for row in manifest['files']:
        relative=safe_relative(row['path']);p=bundle/relative;no_symlink_parents(p,bundle)
        if p.is_symlink() or not p.is_file() or sha256(p)!=row['sha256']:raise RuntimeError('安装向导文件损坏：'+str(relative))


def worker_imports(directory, available):
    """Include imports inside functions: engine dependencies are mostly lazy."""
    pending=['engine','verify_selected'];seen=set()
    while pending:
        name=pending.pop()
        if name in seen:continue
        source=directory/(name+'.py')
        if not source.is_file():raise RuntimeError('后台入口缺失：'+name)
        seen.add(name)
        for node in ast.walk(ast.parse(source.read_text('utf-8'),filename=str(source))):
            names=[]
            if isinstance(node,ast.Import):names=[alias.name.split('.')[0] for alias in node.names]
            elif isinstance(node,ast.ImportFrom) and node.module:names=[node.module.split('.')[0]]
            for dependency in names:
                if dependency in available:
                    if not (directory/(dependency+'.py')).is_file():
                        raise RuntimeError('后台依赖未列入安装器完整性清单：'+dependency)
                    pending.append(dependency)
    return sorted(seen)


def stage_worker(bundle,code):
    """Publish a fresh private copy of every sealed flat installer module.

    A hand-maintained list missed container_storage.py in revision 5. Use the
    integrity manifest and test imports without the source tree or PYTHONPATH.
    """
    no_symlink_parents(code,STATE_ROOT)
    if code.is_symlink() or (code.exists() and not code.is_dir()):raise RuntimeError('后台代码目录类型错误')
    manifest=read_json(bundle/'installer-manifest.json')
    rows={}
    for row in manifest['files']:
        relative=safe_relative(row['path'])
        if (len(relative.parts)==2 and relative.parts[0]=='installer' and relative.suffix=='.py') or relative.as_posix()=='release.json':
            name=relative.name
            if name in rows:raise RuntimeError('后台代码清单包含重复文件')
            rows[name]=row
    if not {'engine.py','common.py','verify_selected.py','release.json'}.issubset(rows):
        raise RuntimeError('安装器完整性清单缺少后台入口')
    available={p.stem for p in (bundle/'installer').glob('*.py')}
    temporary=Path(tempfile.mkdtemp(prefix='.code-stage-',dir=code.parent));previous=None
    try:
        for name,row in rows.items():
            source=bundle/safe_relative(row['path']);no_symlink_parents(source,bundle)
            if source.is_symlink():raise RuntimeError('后台代码不能是符号链接')
            target=temporary/name;shutil.copyfile(source,target);target.chmod(0o600)
            if sha256(target)!=row['sha256']:raise RuntimeError('后台代码复制校验失败：'+name)
        imports=worker_imports(temporary,available)
        script='import importlib,sys;sys.path.insert(0,sys.argv[1]);[importlib.import_module(name) for name in sys.argv[2:]]'
        checked=subprocess.run(['/usr/bin/python3','-I','-B','-c',script,str(temporary),*imports],
                               cwd=temporary,capture_output=True,text=True,timeout=45)
        if checked.returncode:raise RuntimeError('后台模块依赖自检失败，未启动安装任务：'+checked.stderr[-1200:])
        atomic_json(temporary/'worker-manifest.json',{'schema':1,'source_commit':read_json(temporary/'release.json').get('source_commit'),
                                                     'files':rows,'checked_imports':imports})
        if code.exists():
            previous=Path(tempfile.mkdtemp(prefix='.code-previous-',dir=code.parent));os.replace(code,previous)
        try:os.replace(temporary,code)
        except BaseException:
            if previous is not None:os.replace(previous,code);previous=None
            raise
        if previous is not None:shutil.rmtree(previous)
    finally:
        if temporary.exists():shutil.rmtree(temporary)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['install','resume','pause']);parser.add_argument('--bundle',required=True)
    parser.add_argument('--preflight-only',action='store_true',help='Validate the real worker entry and storage, then pause before installation stages')
    args=parser.parse_args()
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
        if release.get('yiqiban_resource_id'):
            from yiqiban_config import REQUIRED_COMPONENTS,model_plan,validate_host
            model_plan(selected)
            if not REQUIRED_COMPONENTS.issubset(saved_request.get('workflow',{}).get('components',{}).get('selected',[])):
                raise ValueError('翼起办需要 MySQL、Redis、ParadeDB')
            yiqiban=request.get('yiqiban',{})
            if not isinstance(yiqiban,dict):raise ValueError('翼起办配置格式错误')
            host=yiqiban.get('public_host','')
            saved_request['yiqiban']={'public_host':validate_host(host) if host else ''}
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
    code=STATE_ROOT/'private/code';stage_worker(bundle,code)
    # Credentials are never command-line arguments, environment values or public status fields.
    command=['systemd-run','--unit='+UNIT,'--collect','--property=Type=exec','--property=Restart=no','--property=UMask=0077','--property=TimeoutStopSec=45','--setenv=PYTHONDONTWRITEBYTECODE=1','--setenv=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin','/usr/bin/python3','-u',str(code/'engine.py'),'--bundle',str(bundle)]
    if getattr(args,'preflight_only',False):command.append('--preflight-only')
    subprocess.run(command,check=True)
    print('后台安装任务已启动；关闭窗口不会中断任务。')

if __name__=='__main__':
    try:main()
    except Exception as exc:print(str(exc),file=sys.stderr);sys.exit(1)
