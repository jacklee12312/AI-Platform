"""Shared read-only checks and safe filesystem operations for the native installer."""
from __future__ import annotations
import hashlib,json,os,platform,re,shutil,socket,stat,subprocess,tempfile
from pathlib import Path, PurePosixPath

from version import VERSION

PRODUCT='AI 平台安装器 · '+VERSION
STATE_ROOT=Path('/var/lib/ai-platform-installer')
PUBLIC_STATE=STATE_ROOT/'public/status.json'
UNIT='ai-platform-install.service'
STEPS=[('verify','校验所选资源'),('prepare','展开配套运行时'),('conflicts','检查文件冲突与可复用模型'),('docker','配置容器基础环境'),('payload','安装所选模型与项目文件'),('credentials','配置账号与独立密钥'),('images','导入配套 12 个镜像'),('systemd','配置开机自启'),('start','启动平台与所选服务'),('acceptance','依次验收所选模型')]
STEPS.insert(0,('acquire','准备资源：缓存、本地源与远端下载'))
STEPS[0:0]=[('probe','第一阶段：安装探针，完成注册和 MQTT 连接'),('components','第二阶段：安装组件库并检查健康状态')]
STEPS.append(('integration','联检探针、组件库和模型平台'))
CORE_DIRS=['houmo-docker-deployment','ai-model-gateway','gateway-admin','cv_deploy','sensevoice-cpu','kokoro-cix','matcha-tts-cpu','qwen3guard-stream-cpu','ballot-reader','modeldeck-models']
ACCELERATOR_MODELS={'copaw':'copaw-flash-9b','spark-x1':'spark-x1-4b','qwen38':'qwen3.8-27b','qwen36':'qwen3.6-35b-a3b','qwen-vl':'qwen3-vl-8b','glm-ocr':'glm-ocr-0.9b','bge-m3':'bge-m3','reranker':'bge-reranker-v2-m3','cosyvoice':'cosyvoice3-0.5b'}
DEVICE1_NODES=tuple('/dev/xh2a_'+name for name in (
    'fast_memory_device1','i2c_device1','ipu1','memory_allocator_device1_ddr',
    'memory_allocator_device1_spm0','memory_allocator_device1_spm1',
    'memory_transfer_device1','qspi_device1','rpmsg_device1_0','system_device1'))

def detect_device_count():
    """Fail closed if the second XH2 device is only partly exposed."""
    extra=[str(p) for p in Path('/dev').glob('xh2a_ipu*')
           if re.fullmatch(r'xh2a_ipu(?:[2-9]|[1-9][0-9]+)',p.name) and p.is_char_device()]
    if extra:raise RuntimeError('本安装器仅支持单卡或双卡，检测到额外设备：'+', '.join(extra))
    first=('/dev/aipu','/dev/xh2a_ctl0','/dev/xh2a_ipu0',
           '/dev/xh2a_fast_memory_device0','/dev/xh2a_memory_allocator_device0_ddr',
           '/dev/xh2a_memory_transfer_device0')
    missing=[name for name in first if not Path(name).is_char_device()]
    if missing:raise RuntimeError('加速卡 0 设备节点不完整：'+', '.join(missing))
    present=[name for name in DEVICE1_NODES if Path(name).is_char_device()]
    if present and len(present)!=len(DEVICE1_NODES):
        raise RuntimeError('加速卡 1 设备节点不完整：'+', '.join(sorted(set(DEVICE1_NODES)-set(present))))
    return 2 if present else 1

def validate_runtime_config(text):
    """Release-specific guard: locked XH2 backend supports at most eight sequences."""
    for name in ['bge-m3','reranker']:
        block=re.search(r'^  '+re.escape(name)+r':\s*\n(.*?)(?=^  [\w-]+:\s*$|\Z)',text,re.M|re.S)
        values=re.findall(r'^    - -np\s*\n    - [\'\"]?(\d+)[\'\"]?\s*$',block.group(1),re.M) if block else []
        if values!=['8']:raise RuntimeError(name+' 配置与配套后端不匹配：本发行包要求 -np 8')

def validate_acceptance(result,run_id,models):
    if result.get('run_id')!=run_id:raise RuntimeError('验收报告不是本次运行生成，不能作为安装成功依据')
    if result.get('passed') is not True or result.get('scope')!='all-modes':raise RuntimeError('全模型验收未通过')
    if set(result.get('registry_models',[]))!=set(models):raise RuntimeError('验收报告的模型清单不完整')
    expected={'containers-and-autostart','active-accelerator-real-inference','authenticated-gateway-registry','ballot-gateway-36-selections','restore-mode:copaw','gateway-sse-and-usage'}
    expected.update('accelerator-mode:'+m for m in ACCELERATOR_MODELS)
    expected.update('inference:'+m for m in set(models)-set(ACCELERATOR_MODELS.values())-{'democratic-eval-v1'})
    checks=result.get('checks',[])
    names=[c.get('name') for c in checks]
    if len(names)!=len(set(names)) or set(names)!=expected or any(c.get('passed') is not True for c in checks):
        raise RuntimeError('验收项目缺失、重复或失败，不能标记安装完成')

def read_json(path,default=None):
    try:return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        if default is not None:return default
        raise

def sha256(path,callback=None):
    h=hashlib.sha256();done=0;size=Path(path).stat().st_size
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(8*1024**2),b''):
            h.update(chunk);done+=len(chunk)
            if callback:callback(done,size)
    return h.hexdigest()

def atomic_json(path,value,mode=0o600):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
        os.chmod(tmp,mode);os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def atomic_text(path,text,mode=0o600):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as out:
            out.write(text);out.flush();os.fsync(out.fileno())
        os.chmod(tmp,mode);os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def safe_relative(name):
    p=PurePosixPath(name)
    if not name or '\\' in name or '\x00' in name or p.is_absolute() or '..' in p.parts or ':' in name:
        raise ValueError('不安全的包内路径：'+repr(name))
    return Path(*p.parts)

def no_symlink_parents(path,root):
    root=Path(root);path=Path(path)
    try:relative=path.relative_to(root)
    except ValueError:raise RuntimeError('目标超出安装范围：'+str(path))
    if root.is_symlink():raise RuntimeError('安装根目录不能是符号链接：'+str(root))
    node=root
    for part in relative.parts[:-1]:
        node=node/part
        if node.is_symlink():raise RuntimeError('目标父目录存在符号链接：'+str(node))
        if node.exists() and not node.is_dir():raise RuntimeError('目标父路径不是目录：'+str(node))

def file_matches(source,target,root):
    source=Path(source);target=Path(target);no_symlink_parents(target,root)
    if not os.path.lexists(target):return False
    if source.is_symlink():
        if target.is_symlink() and os.readlink(source)==os.readlink(target):return True
        raise RuntimeError('已有链接与离线包冲突，未覆盖：'+str(target))
    if target.is_symlink() or not target.is_file():raise RuntimeError('已有目标类型冲突，未覆盖：'+str(target))
    if source.stat().st_size!=target.stat().st_size or sha256(source)!=sha256(target):
        raise RuntimeError('已有文件与离线包内容不同，未覆盖：'+str(target))
    if os.name!='nt' and not target.stat().st_mode & stat.S_IROTH:
        raise RuntimeError('已有文件缺少容器读取权限，请管理员单独处理：'+str(target))
    return True

def copy_atomic(source,target,root,mode=0o644,uid=None,gid=None,callback=None):
    source=Path(source);target=Path(target);root=Path(root)
    no_symlink_parents(target,root)
    node=root
    for part in target.relative_to(root).parts[:-1]:
        node=node/part
        if not node.exists():
            node.mkdir(mode=0o755)
            node.chmod(0o755)  # The privileged worker intentionally uses umask 0077.
            if uid is not None:os.chown(node,uid,gid)
    if os.path.lexists(target):
        if file_matches(source,target,root):return 'reused'
    if source.is_symlink():
        # Never follow a payload link while copying; the tar validator approved it.
        target.symlink_to(os.readlink(source))
        if uid is not None:os.lchown(target,uid,gid)
        return 'created'
    fd,tmp=tempfile.mkstemp(prefix='.'+target.name+'.ai-install-',dir=target.parent)
    try:
        h=hashlib.sha256();done=0;size=source.stat().st_size
        with source.open('rb') as reader,os.fdopen(fd,'wb') as writer:
            for chunk in iter(lambda:reader.read(8*1024**2),b''):
                writer.write(chunk);h.update(chunk);done+=len(chunk)
                if callback:callback(done,size)
            writer.flush();os.fsync(writer.fileno())
        os.chmod(tmp,mode & 0o777)
        if uid is not None:os.chown(tmp,uid,gid)
        # Link is a no-replace atomic publication, unlike os.replace on a shared target.
        try:os.link(tmp,target)
        except FileExistsError:
            if not file_matches(source,target,root):raise
            return 'reused'
        if sha256(target)!=h.hexdigest():raise RuntimeError('写入后校验失败：'+str(target))
        return 'created'
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def command(args,timeout=20):
    p=subprocess.run([str(x) for x in args],capture_output=True,text=True,timeout=timeout)
    if p.returncode:raise RuntimeError(f'{args[0]} 检查失败（退出码 {p.returncode}）')
    return p.stdout.strip()

def quick_checks(bundle,selected_models=None,selected_components=None):
    """No writes, no model loads and no network requests."""
    bundle=Path(bundle);out=[]
    def row(name,ok,detail,warning=False):out.append({'name':name,'ok':bool(ok),'detail':str(detail),'warning':warning})
    resources=bundle/'resources'
    try:
        modular=(bundle/'catalog/release-catalog.json').exists()
        m=read_json(bundle/'catalog/platform-manifest.json' if modular else resources/'manifests/manifest.json');release=read_json(bundle/'release.json')
        ok=len(m['models'])==27 and len(m['images'])==12 and m['status']=='complete-artifacts'
        row('发行范围',ok,'27 个可选模型及服务（Qwen3.6 仅双卡） · 12 个 ARM64 配套镜像')
        if modular:
            from artifacts import load_catalog
            catalog=load_catalog(bundle)
            row('发行签名',True,'Ed25519 签名通过；安装前仍逐文件校验内容')
            if catalog.get('workflow_sha256'):
                from workflow import COMPONENTS,load_workflow,resolved_components
                workflow=load_workflow(bundle,catalog)
                components=resolved_components({} if selected_components is None else {'selected':selected_components})
                scope='、'.join(COMPONENTS[name] for name in components) if components else '未选组件，跳过组件库'
                row('三阶段安装',True,'探针 '+workflow['probe']['version']+' 注册与 MQTT → '+scope+' → 所选模型；Docker / Compose 为必需环境')
            if catalog.get('limits')!={'bge-m3_parallel_sequences':8,'reranker_parallel_sequences':8}:raise RuntimeError('配套后端参数锁定信息错误')
        else:
            row('资源清单',sha256(resources/'manifests/SHA256SUMS')==release['resources_checksums_sha256'],'后续会逐文件校验全部资源；本页不加载模型')
            validate_runtime_config((resources/'payload/home/cix/houmo-docker-deployment/compose.yaml').read_text())
        row('配套模型参数',True,'BGE / 重排与配套后端匹配：并发序列上限 8')
    except Exception as e:
        row('离线包',False,e);return out
    linux=platform.system()=='Linux';arm=platform.machine() in ['aarch64','arm64']
    row('目标平台',linux and arm,platform.system()+' / '+platform.machine()+'；要求厂家 Debian 12 ARM64')
    if not linux:return out
    try:
        osinfo=dict(line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
        row('操作系统',osinfo.get('ID','').strip('"')=='debian' and osinfo.get('VERSION_ID','').strip('"')=='12',osinfo.get('PRETTY_NAME','unknown').strip('"'))
        import pwd
        user=pwd.getpwnam('cix');row('安装用户',user.pw_dir=='/home/cix','固定安装到 /home/cix；不改动系统驱动')
    except Exception as e:row('操作系统 / 用户',False,e)
    missing=[p for p in m['required_devices'] if not Path(p).is_char_device()]
    row('加速卡设备',not missing,'设备节点齐全' if not missing else '缺少：'+', '.join(missing))
    try:
        device_count=detect_device_count()
        row('设备拓扑',True,f'识别为 {device_count} 卡；Qwen3.8 将使用对应权重和启动配置')
    except RuntimeError as exc:
        device_count=None;row('设备拓扑',False,str(exc))
    row('厂家驱动 / SDK',Path('/usr/local/houmo-drv-xh2_v1.4.0').is_dir(),'要求 XH2 v1.4.0；本安装包不安装底层驱动')
    missing=[c for c in ['python3','tar','gzip','dpkg','dpkg-deb','dpkg-query','systemctl','systemd-run','curl','cp','chown','chmod','usermod','runuser','sha256sum','ip','timeout','flock','tee','pkexec','openssl'] if not shutil.which(c)]
    row('基础命令',not missing,'齐全' if not missing else '缺少：'+', '.join(missing))
    payload=int(m['installed_payload_bytes'])
    if selected_models is not None and modular:
        chosen=set(selected_models)
        payload-=int(m['all_model_file_bytes'])
        payload+=sum(r['size'] for r in catalog['resources'] if r['category']=='models' and chosen.intersection(r['models'])
                     and (not r.get('device_count') or r['device_count']==device_count))
    need=payload+int(m['docker_disk_budget_bytes'])+8*1024**3
    free=shutil.disk_usage('/home/cix').free
    row('安装空间',free>=need,f'用户分区可用 {free/1e9:.1f} GB；保守预算 {need/1e9:.1f} GB。后台另核对 Docker 分区。')
    if modular and catalog.get('workflow_sha256') and components:
        data=Path('/data')
        while not data.exists():data=data.parent
        budget=workflow['component_disk_budget_bytes'];available=shutil.disk_usage(data).free
        row('组件库数据空间',available>=budget,f'数据分区可用 {available/1e9:.1f} GB；组件库预留 {budget/1e9:.1f} GB')
    state=read_json(PUBLIC_STATE,{})
    resume=state.get('release_id')==release['release_id'] and state.get('status') in ['running','failed','paused','installed']
    conflicts=[] if resume else [str(Path('/home/cix')/n) for n in CORE_DIRS if (Path('/home/cix')/n).exists() and (not (Path('/home/cix')/n).is_dir() or any((Path('/home/cix')/n).iterdir()))]
    row('旧部署冲突',not conflicts,'可继续本版本任务' if resume else ('未发现旧部署冲突' if not conflicts else '请先处理：'+', '.join(conflicts)))
    for port in [17700,17780]:
        try:
            with socket.socket() as s:s.bind(('0.0.0.0',port))
            row(f'端口 {port}',True,'可用')
        except OSError:row(f'端口 {port}',resume,'本版本已有任务占用' if resume else '已被占用，不会停止其他程序')
    existing=Path('/home/cix/cix_telecom_projects')
    row('已有模型',True,'检测到共享模型目录；仅复用哈希一致文件，额外模型保留' if existing.exists() else '将安装勾选的模型',warning=existing.exists())
    return out

def check_password(password):
    if not isinstance(password,str) or not 12<=len(password)<=128 or '\n' in password or '\r' in password or '\x00' in password:
        raise ValueError('管理员密码需为 12–128 个字符，不含换行。')
