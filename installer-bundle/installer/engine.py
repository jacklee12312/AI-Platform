"""Root worker: prepare verified resources first, then install the locked full platform."""
from __future__ import annotations
import argparse,base64,fcntl,hashlib,importlib.util,json,os,pwd,re,secrets,shutil,stat,subprocess,sys,tarfile,tempfile,time
from pathlib import Path
from common import (STATE_ROOT,STEPS,CORE_DIRS,ACCELERATOR_MODELS,read_json,atomic_json,sha256,safe_relative,
                    no_symlink_parents,file_matches,copy_atomic,quick_checks,check_password,
                    validate_runtime_config,validate_acceptance,detect_device_count)
from selection import selected_catalog,validate_selected

class Paused(Exception):pass

def safe_extract(archive,destination):
    """Extract only regular files/dirs and validated links into a private empty tree."""
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    if destination.is_symlink():raise RuntimeError('运行时暂存目录不能是链接')
    pending=[]
    with tarfile.open(archive,'r:gz') as tar:
        seen=set()
        for item in tar:
            rel=safe_relative(item.name);target=destination/rel
            if rel.as_posix() in seen:raise RuntimeError('运行时归档包含重复路径')
            seen.add(rel.as_posix());no_symlink_parents(target,destination)
            if item.isdir():target.mkdir(parents=True,exist_ok=True,mode=0o755);continue
            target.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
            if item.issym():
                link=item.linkname
                if '\x00' in link or '\\' in link:raise RuntimeError('无效运行时链接')
                if link.startswith('/'):
                    if not (link.startswith('/home/cix/') or link in ['/usr/bin/python3','/usr/bin/python3.11']):raise RuntimeError('运行时绝对链接越界')
                else:
                    resolved=Path(os.path.normpath(str(target.parent/link)))
                    if not resolved.is_relative_to(destination):raise RuntimeError('运行时相对链接越界')
                pending.append((target,link,False));continue
            if item.islnk():
                link=safe_relative(item.linkname);pending.append((target,str(destination/link),True));continue
            if not item.isfile():raise RuntimeError('运行时归档包含特殊设备或 FIFO')
            with tar.extractfile(item) as source,target.open('xb') as out:shutil.copyfileobj(source,out,8*1024**2)
            target.chmod(item.mode & 0o777)
    # Deferred links cannot redirect any preceding file write outside the staging area.
    for target,link,hard in pending:
        no_symlink_parents(target,destination)
        if hard:
            src=Path(link);no_symlink_parents(src,destination)
            if src.is_symlink() or not src.is_file():raise RuntimeError('运行时硬链接目标无效')
            os.link(src,target)
        else:target.symlink_to(link)

class Installer:
    def __init__(self,bundle,root=STATE_ROOT,home=Path('/home/cix'),storage_root=None):
        self.bundle=Path(bundle).resolve();self.resources=self.bundle/'resources';self.root=Path(root);self.home=Path(home)
        self.release=read_json(self.bundle/'release.json');self.catalog=None
        if (self.bundle/'catalog/release-catalog.json').is_file():
            from artifacts import load_catalog
            self.catalog=load_catalog(self.bundle)
            request=read_json(self.root/'private/request.json')
            self.selected=validate_selected(self.catalog,request.get('selected_models'))
            self.device_count=request.get('device_count')
            if self.device_count!=detect_device_count():raise RuntimeError('设备卡数与安装任务记录不一致，不能下载模型')
            self.install_catalog=selected_catalog(self.catalog,self.selected,self.device_count)
            safe_relative(self.release['release_id'])
            self.resources=self.root/'private/releases'/(self.release['release_id']+'-installer-'+self.release.get('installer_version','legacy'))/'resources'
            self.manifest=read_json(self.bundle/'catalog/platform-manifest.json')
        else:
            self.manifest=read_json(self.resources/'manifests/manifest.json')
            self.selected=self.manifest.get('models',[]);self.install_catalog=None
            self.device_count=detect_device_count()
        self.state_file=self.root/'private/journal.json';self.public=self.root/'public/status.json'
        self.state=read_json(self.state_file,{'release_id':self.release['release_id'],'completed':[],'events':[]})
        if self.state['release_id']!=self.release['release_id']:raise RuntimeError('已有其他版本安装记录；本版不是升级工具')
        if self.state_file.exists() and self.state.get('installer_version')!=self.release.get('installer_version','legacy'):
            raise RuntimeError('已有不同安装器版本的任务记录；不能跨版本继续')
        if self.state.get('status')=='installed':raise RuntimeError('本版本已安装；请打开管理界面')
        self.last_update=0;self.plan=[];self.base=None;self.current='acquire'
        self.storage=None
        if self.catalog is not None:
            from storage import Storage
            self.storage=Storage(self.root,self.release,storage_root)
            self.storage.ensure();self.resources=self.storage.resources
        self.workflow=None
        if self.catalog and self.catalog.get('workflow_sha256'):
            from workflow import Workflow
            self.workflow=Workflow(self);self.current='probe'

    def resource_plan(self):
        from artifacts import source_config,plan_sources
        settings=read_json(self.root/'private/request.json')
        config=source_config(self.bundle,settings.get('sources'))
        plan=plan_sources(self.bundle,self.install_catalog,config,self.storage.cache)
        if not plan['ready']:
            missing=[r for r in plan['rows'] if r['source']=='missing']
            raise RuntimeError('缺少 '+str(len(missing))+' 个资源的本地文件或远端地址；请在资源来源页补齐后继续')
        from storage import existing,budgets,require_space,WORK_RESERVE,METADATA_RESERVE
        docker=Path('/var/lib/docker')
        if shutil.which('docker'):
            info=subprocess.run(['docker','info','--format','{{.DockerRootDir}}'],capture_output=True,text=True)
            if info.returncode==0 and info.stdout.strip():docker=Path(info.stdout.strip())
        selected_bytes=sum(r['size'] for r in self.install_catalog['resources'] if r['category']=='models')
        base_bytes=int(self.manifest['installed_payload_bytes'])-int(self.manifest['all_model_file_bytes'])
        entries=[(self.home,base_bytes+selected_bytes,'平台与所选模型'),
                 (docker,self.docker_space_budget(),'Docker 镜像'),
                 (self.root,METADATA_RESERVE,'安装记录与日志'),
                 (self.storage.work,WORK_RESERVE,'运行时解压与工作空间'),
                 (self.storage.cache,plan['cache_bytes_needed'],'资源缓存')]
        if self.release.get('yiqiban_resource_id'):
            from yiqiban_payload import load
            apps=load(self.bundle)
            image_paths={r['archive'] for r in apps['images']}
            app_bytes=sum(r['size'] for r in apps['files'] if r['path'] in image_paths)
            entries += [(docker,app_bytes*3+2*1024**3,'翼起办应用镜像'),
                        (self.storage.data_root,8*1024**3,'翼起办业务数据')]
        if existing(self.storage.cache).stat().st_dev!=existing(self.resources).stat().st_dev:
            entries.append((self.resources,plan['total_bytes'],'跨分区资源暂存'))
        rows=budgets(entries);require_space(rows)
        self.state['storage']={'data_root':str(self.storage.data_root),'cache':str(self.storage.cache),
                               'work':str(self.storage.work),'budgets':rows}
        return config,plan

    def docker_space_budget(self):
        """Already imported pinned images need working space, not a second copy."""
        full=int(self.manifest['docker_disk_budget_bytes'])
        if not shutil.which('docker'):return full
        images=self.manifest['images']
        result=subprocess.run(['docker','image','inspect',*[x['tag'] for x in images]],capture_output=True,text=True)
        if result.returncode:return full
        try:
            actual=json.loads(result.stdout)
            if len(actual)!=len(images):return full
            if all(row['Id'] in {pin['id'],pin['config_id']} for row,pin in zip(actual,images)):
                return min(full,2*1024**3)
        except (ValueError,KeyError,TypeError):pass
        return full

    def acquire(self):
        if self.catalog is None:return
        from artifacts import materialize
        config,plan=self.resource_plan()
        completed={};total=max(1,plan['total_bytes']+plan.get('archive_bytes',0))
        def progress(row,n,phase):
            completed[row['path']]=max(completed.get(row['path'],0),min(n,row['size']))
            self.update(phase+'：'+row['path'],sum(completed.values())/total,transfer={'file':row['path'],'file_bytes':n,'file_total':row['size'],'ready_bytes':sum(completed.values()),'total_bytes':total,'phase':phase})
        self.event(f'准备 {len(self.selected)} 个所选模型与平台组件；仅在完整校验后进入部署')
        materialize(self.bundle,self.install_catalog,config,self.resources,self.storage.cache,progress,self.pause_point)
        self.state.pop('transfer',None)
        self.event('全部资源已准备，进入平台安装；下载缓存可用于失败恢复')

    def probe(self):
        if self.workflow:self.workflow.probe()

    def components(self):
        if self.workflow:
            if self.release.get('component_runtime_release_id'):
                from runtime_overlay import load
                load(self.bundle)
            self.workflow.components()

    def integration(self):
        if self.workflow:self.workflow.finish()

    def yiqiban(self):
        if self.release.get('yiqiban_resource_id'):
            from yiqiban import YiQiBan
            YiQiBan(self).deploy()

    def yiqiban_check(self):
        if self.release.get('yiqiban_resource_id'):
            from yiqiban import YiQiBan
            YiQiBan(self).check()

    def update(self,message=None,fraction=None,force=False,**fields):
        self.state.update(fields);self.state['current']=self.current;self.state['updated_at']=time.time()
        if message:self.state['message']=message
        if fraction is not None:self.state['fraction']=max(0,min(1,fraction))
        index=[x[0] for x in STEPS].index(self.current)
        self.state['percent']=round((index+self.state.get('fraction',0))*100/len(STEPS),1)
        if force or time.monotonic()-self.last_update>=0.7:
            atomic_json(self.state_file,self.state)
            public={k:v for k,v in self.state.items() if k not in ['private','owned_paths']}
            atomic_json(self.public,public,0o644);self.last_update=time.monotonic()

    def event(self,message):
        self.state.setdefault('events',[]).append({'time':time.strftime('%H:%M:%S'),'message':message})
        self.state['events']=self.state['events'][-100:];self.update(message,force=True)

    def pause_point(self):
        if (self.root/'private/pause.request').exists():raise Paused('已在安全检查点暂停，重新打开向导后可继续。')

    def verify(self):
        expected=self.release['resources_checksums_sha256']
        sums=self.resources/'manifests/SHA256SUMS'
        if sha256(sums)!=expected:raise RuntimeError('资源清单不属于本次交付版本')
        all_rows=dict(s.split('  ',1)[::-1] for s in sums.read_text().splitlines())
        selected_paths={r['path'] for r in self.install_catalog['resources']} - {'manifests/SHA256SUMS'}
        if not selected_paths.issubset(all_rows):raise RuntimeError('所选资源不在锁定校验清单中')
        rows=[(all_rows[path],path) for path in sorted(selected_paths)]
        total=sum((self.resources/safe_relative(r)).stat().st_size for _,r in rows);done=0;listed=set()
        for digest,relative in rows:
            self.pause_point();path=self.resources/safe_relative(relative);listed.add(relative)
            no_symlink_parents(path,self.resources)
            if path.is_symlink() or not path.is_file():raise RuntimeError('缺少有效资源文件：'+relative)
            got=sha256(path,lambda n,s:self.update('正在校验：'+relative,(done+n)/max(1,total)))
            if got!=digest:raise RuntimeError('资源文件损坏：'+relative)
            done+=path.stat().st_size
        for p in self.resources.rglob('*'):
            if p.is_symlink():raise RuntimeError('资源目录含意外链接：'+str(p))
            if p.is_file() and p.relative_to(self.resources).as_posix() not in listed|{'manifests/SHA256SUMS'}:raise RuntimeError('资源目录含未验证文件：'+str(p))
        if self.catalog is not None and (set(self.manifest['models'])!=set(self.catalog['models']) or len(self.manifest['images'])!=self.catalog['image_count']):raise RuntimeError('平台范围与签名发行清单不一致')
        validate_runtime_config((self.resources/'payload/home/cix/houmo-docker-deployment/compose.yaml').read_text())
        if self.catalog is not None:
            validate_runtime_config((self.resources/'variants/single/houmo-docker-deployment/compose.yaml').read_text())
        self.event('所选资源 SHA-256 校验通过')
        spec=importlib.util.spec_from_file_location('offline_release',self.resources/'scripts/offline_install.py')
        self.base=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.base)

    def prepare(self):
        stage=self.runtime_directory()
        no_symlink_parents(stage,self.storage.work if self.storage else self.root)
        if stage.is_symlink():raise RuntimeError('暂存路径不能是链接')
        # Only our private, fixed staging directory may be regenerated.
        if stage.exists():shutil.rmtree(stage)
        for archive,destination in [('qwen36-vllm-runtime.tar.gz',''),('cosyvoice-runtime.tar.gz',''),('host-libs.tar.gz','modeldeck-runtime/lib'),('ui-static.tar.gz','gateway-admin/frontend/dist'),('qwen38-hliellama-2.2.0.tar.gz','modeldeck-runtime/qwen38-mm')]:
            self.update('正在展开：'+archive,force=True)
            safe_extract(self.resources/'runtime'/archive,stage/destination)
        from npu_runtime import prepare
        prepare(self)
        self.make_plan()

    def make_plan(self):
        by_path={}
        variant_files={
            'houmo-docker-deployment/compose.yaml',
            'ai-model-gateway/config/models.json',
            'gateway-admin/agent/runtime_baseline.json',
            'gateway-admin/agent/model_catalog.json',
            'gateway-admin/admin_app/capability_catalog.json',
        }
        for origin,runtime in [(self.resources/'payload/home/cix',False),(self.runtime_directory(),True)]:
            for p in origin.rglob('*'):
                if not (p.is_file() or p.is_symlink()):continue
                relative=p.relative_to(origin);target=self.home/relative
                if not runtime and self.catalog is not None and self.device_count==1 and relative.as_posix() in variant_files:
                    p=self.resources/'variants/single'/relative
                    if not p.is_file() or p.is_symlink():raise RuntimeError('单卡运行配置缺失：'+relative.as_posix())
                if not runtime and relative.as_posix() in {'gateway-admin/host_ops/core/components.py','gateway-admin/host_ops/runtime.py'}:
                    from runtime_overlay import effective_source
                    p=effective_source(self.bundle,relative.as_posix(),p)
                raw_mode=p.lstat().st_mode & 0o777
                mode=(raw_mode | 0o444 | (0o111 if raw_mode & 0o111 else 0)) if runtime else (0o755 if p.name=='modelctl' or p.suffix=='.sh' else 0o644)
                if str(target) in by_path:
                    previous=by_path[str(target)][0]
                    if p.is_symlink() or previous.is_symlink() or sha256(p)!=sha256(previous):raise RuntimeError('包内存在冲突的安装目标：'+str(target))
                if relative.as_posix() in {'kokoro-cix/docker-compose.yml','cv_deploy/compose.yaml'} and not runtime:
                    from npu_runtime import effective_compose,apply_record
                    original=p;component='kokoro' if relative.parts[0]=='kokoro-cix' else 'cv'
                    # Libraries are repaired and probed before service startup.
                    p=effective_compose(self.resources,self.root,component,verify_files=False)
                    if p!=original and target.exists():apply_record(self.resources,self.root,component,target,self.home)
                by_path[str(target)]=(p,target,mode)
        self.plan=list(by_path.values())

    def runtime_directory(self):
        return self.storage.runtime if getattr(self,'storage',None) else self.root/'private/runtime'

    def command(self,*args,timeout=None):
        print('RUN',str(args[0]),flush=True)
        subprocess.run([str(x) for x in args],check=True,timeout=timeout)

    def runtime_is_pinned(self):
        if not shutil.which('docker'):return False
        for name in ['containerd.io','docker-ce-cli','docker-ce','docker-buildx-plugin','docker-compose-plugin']:
            paths=list((self.resources/'docker/arm64-bookworm').glob(name+'_*_arm64.deb'))
            if len(paths)!=1:raise RuntimeError('离线 Docker 包不完整：'+name)
            arch=subprocess.check_output(['dpkg-deb','-f',str(paths[0]),'Architecture'],text=True).strip()
            if arch!='arm64':raise RuntimeError('Docker 包架构错误')
            expected=subprocess.check_output(['dpkg-deb','-f',str(paths[0]),'Version'],text=True).strip()
            actual=subprocess.run(['dpkg-query','-W','-f=${Status}|${Version}',name],capture_output=True,text=True)
            if actual.returncode or actual.stdout.strip()!='install ok installed|'+expected:return False
        p=subprocess.run(['docker','info'],capture_output=True,text=True)
        return p.returncode==0

    def conflicts(self):
        if not self.state.get('initial_guard_passed'):
            if self.workflow:
                from platform_takeover import prepare_files
                prepare_files(self)
            for name in CORE_DIRS:
                p=self.home/name
                if os.path.lexists(p) and (p.is_symlink() or not p.is_dir() or any(p.iterdir())):
                    raise RuntimeError('检测到已有项目部署，不覆盖：'+str(p))
            for name in ['houmo-ai.service','ai-model-gateway.service','gateway-admin.service','modeldeck-startup-verify.service']:
                if os.path.lexists('/etc/systemd/system/'+name):raise RuntimeError('检测到已有同名系统服务：'+name)
            self.state['initial_guard_passed']=True;self.update(force=True)
        self.guard_units()
        if not self.plan:self.make_plan()
        reuse=0
        for i,(source,target,mode) in enumerate(self.plan):
            self.pause_point();self.update('核对已有文件：'+str(target),(i+1)/len(self.plan))
            if file_matches(source,target,self.home):
                if not source.is_symlink() and mode & 0o111 and target.stat().st_mode & 0o111 != 0o111:
                    raise RuntimeError('已有运行文件缺少执行权限，请管理员单独处理：'+str(target))
                reuse+=1
        self.state['reusable_files']=reuse
        # The workflow replaces mismatched Docker packages before model deployment.
        expected_names=set()
        for p in (self.resources/'payload/home/cix').rglob('*'):
            if p.name in ['compose.yaml','docker-compose.yml']:
                expected_names.update(re.findall(r'^\s*container_name:\s*([\w.-]+)\s*$',p.read_text(),re.M))
        if shutil.which('docker'):
            result=subprocess.run(['docker','ps','-a','--format','{{.Names}}'],capture_output=True,text=True)
            if result.returncode:raise RuntimeError('Docker 已安装但服务不可访问，请管理员处理后继续')
            current=set(result.stdout.split())
            if current & expected_names and 'start' not in self.state['completed'] and not self.state.get('start_attempted'):
                raise RuntimeError('存在同名项目容器，不会覆盖：'+', '.join(sorted(current & expected_names)))
            for name in current & expected_names:
                info=json.loads(subprocess.check_output(['docker','inspect',name],text=True))[0]
                labels=info.get('Config',{}).get('Labels') or {}
                directory=labels.get('com.docker.compose.project.working_dir','')
                tag=info.get('Config',{}).get('Image')
                pinned=next((x for x in self.manifest['images'] if x['tag']==tag),None)
                allowed_dirs={str(self.home/p.parent.relative_to(self.resources/'payload/home/cix')) for p in (self.resources/'payload/home/cix').rglob('*') if p.name in ['compose.yaml','docker-compose.yml']}
                if directory not in allowed_dirs or not pinned or info.get('Image') not in {pinned['id'],pinned['config_id']}:
                    raise RuntimeError('同名容器不属于本次部署，未接管：'+name)
            if current and not self.workflow:
                if not self.runtime_is_pinned():raise RuntimeError('已有容器正在使用非配套 Docker 版本；本安装器不会升级或停止它们')
        for image in self.manifest['images']:
            if not shutil.which('docker'):break
            p=subprocess.run(['docker','image','inspect',image['tag']],capture_output=True,text=True)
            if p.returncode==0:
                actual=json.loads(p.stdout)[0]
                if actual['Id'] not in {image['id'],image['config_id']}:raise RuntimeError('同名镜像内容冲突，不会覆盖：'+image['tag'])
        self.event(f'文件冲突检查通过；可复用 {reuse} 个内容一致的文件，清单外模型保留')

    def guard_units(self,system=Path('/etc/systemd/system')):
        for relative,name in [('payload/home/cix/houmo-docker-deployment/houmo-ai.service','houmo-ai.service'),('payload/home/cix/ai-model-gateway/systemd/ai-model-gateway.service','ai-model-gateway.service'),('systemd/gateway-admin.service','gateway-admin.service'),('systemd/modeldeck-startup-verify.service','modeldeck-startup-verify.service')]:
            target=system/name;source=self.resources/relative
            if os.path.lexists(target) and (target.is_symlink() or not target.is_file() or sha256(target)!=sha256(source)):
                raise RuntimeError('已有系统服务定义与本包不同，不覆盖：'+name)

    def systemd(self):
        self.guard_units();self.base.setup_units()
        source=self.resources/'systemd/modeldeck-ops.service'
        if source.is_file():
            target=Path('/etc/systemd/system/modeldeck-ops.service')
            no_symlink_parents(target,Path('/'))
            if target.exists() and (target.is_symlink() or sha256(target)!=sha256(source)):
                raise RuntimeError('已有 ModelDeck 运维服务定义不同，不覆盖')
            shutil.copyfile(source,target);target.chmod(0o644)
            self.command('systemctl','daemon-reload')
            self.command('systemctl','enable','--now','modeldeck-ops.service')

    def docker(self):
        if self.workflow:
            self.workflow.docker();return
        if self.runtime_is_pinned():
            self.command('usermod','-aG','docker','cix');self.event('复用已安装的全部 5 个配套 Docker 软件包，不重启 Docker');return
        self.base.install_docker()

    def payload(self):
        if not self.plan:self.make_plan()
        user=pwd.getpwnam('cix');total=sum(s.lstat().st_size for s,_,_ in self.plan);done=0;new=0;reused=0
        for source,target,mode in self.plan:
            self.pause_point()
            action=copy_atomic(source,target,self.home,mode,user.pw_uid,user.pw_gid,lambda n,s:self.update('正在安装：'+str(target),(done+n)/max(1,total)))
            done+=source.lstat().st_size
            new+=action=='created';reused+=action=='reused'
            self.update('已'+('复用：' if action=='reused' else '安装：')+str(target),done/max(1,total))
        for relative in ['modeldeck-models','modeldeck-runtime','model-imports','cix_telecom_projects/cosyvoice-vllm-staging/logs']:
            path=self.home/relative;no_symlink_parents(path/'placeholder',self.home)
            node=self.home
            for part in path.relative_to(self.home).parts:
                node=node/part
                if not node.exists():node.mkdir(mode=0o755);node.chmod(0o755);os.chown(node,user.pw_uid,user.pw_gid)
        self.event(f'全部项目文件已安装：新建 {new}，复用 {reused}；未递归修改共享模型目录权限')

    def credentials(self):
        request_path=self.root/'private/request.json';request=read_json(request_path)
        encoded=request.get('admin_hash')
        if not encoded:
            password=request.get('admin_password');check_password(password)
            salt=secrets.token_bytes(18);digest=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,260000)
            encoded='$'.join(['pbkdf2_sha256','260000',base64.urlsafe_b64encode(salt).decode(),base64.urlsafe_b64encode(digest).decode()])
            request.pop('admin_password',None);request['admin_hash']=encoded;atomic_json(request_path,request)
        for key in ['api_key','management_token','agent_token']:
            if key not in request:request[key]=secrets.token_hex(32)
        atomic_json(request_path,request)  # Persist all secrets before publishing either .env.
        user=pwd.getpwnam('cix')
        def create_or_match(relative,content,mode=0o600):
            target=self.home/relative;no_symlink_parents(target,self.home)
            if os.path.lexists(target):
                if target.is_symlink() or not target.is_file() or target.read_text()!=content:
                    raise RuntimeError('已有凭据或初始状态不同，不覆盖：'+str(target))
                if target.stat().st_mode & 0o777 != mode:raise RuntimeError('已有凭据或状态权限不符：'+str(target))
                return
            fd,temporary=tempfile.mkstemp(prefix='.gui-credential-',dir=target.parent)
            try:
                with os.fdopen(fd,'w') as out:out.write(content);out.flush();os.fsync(out.fileno())
                os.chmod(temporary,mode);os.chown(temporary,user.pw_uid,user.pw_gid);os.link(temporary,target)
            finally:os.unlink(temporary)
        create_or_match('houmo-docker-deployment/.env','HOUMO_API_KEY='+request['api_key']+'\nGATEWAY_MANAGEMENT_TOKEN='+request['management_token']+'\n')
        create_or_match('gateway-admin/.env',"ADMIN_PASSWORD_HASH='"+encoded+"'\nADMIN_AGENT_TOKEN="+request['agent_token']+'\n')
        for relative,uid,gid,mode in [('houmo-docker-deployment/state',user.pw_uid,user.pw_gid,0o755),('ai-model-gateway/data',65534,65534,0o750)]:
            target=self.home/relative;no_symlink_parents(target/'placeholder',self.home)
            if not target.exists():target.mkdir()
            target.chmod(mode);os.chown(target,uid,gid)
        selected_modes=[mode for mode,model in ACCELERATOR_MODELS.items() if model in self.selected]
        initial_mode=selected_modes[0] if selected_modes else 'copaw'
        create_or_match('houmo-docker-deployment/state/active-mode',initial_mode+'\n',0o644)
        install_state={'schema_version':1,'revision':0,'models':{
            model:{'installed':True,'enabled':True,'status':'ready'} for model in self.selected}}
        create_or_match('houmo-docker-deployment/state/model-installations.json',json.dumps(install_state,ensure_ascii=False,indent=2)+'\n',0o644)
        self.event('管理员密码已以哈希保存；独立 API / Agent 密钥已生成，日志不包含密码')

    def start(self):
        transaction=None
        if getattr(self,'workflow',None):
            from platform_takeover import claim_ports
            transaction=claim_ports(self)
        try:
            self.start_services()
            if transaction:transaction.commit()
        except Exception:
            if transaction:
                from platform_takeover import rollback_start
                rollback_start(self,transaction)
            raise

    def start_services(self):
        self.state['start_attempted']=True;self.update(force=True)
        from npu_runtime import configure
        configure(self)
        mode_map={'copaw-flash-9b':'copaw','spark-x1-4b':'spark-x1','bge-m3':'bge-m3',
                  'bge-reranker-v2-m3':'reranker','cosyvoice3-0.5b':'cosyvoice',
                  'qwen3-vl-8b':'qwen-vl','glm-ocr-0.9b':'glm-ocr','qwen3.8-27b':'qwen38','qwen3.6-35b-a3b':'qwen36'}
        services=[mode_map[m] for m in self.selected if m in mode_map]
        if services:self.base.compose('houmo-docker-deployment','compose.yaml','up','--no-start','--no-build','--pull','never',*services)
        cv={'mobilenet_v2','inception_v3','yolov11_l','yolov11_n','yolov11n_obb','yolov12_s','yolov12_m','bytetrack_s','retinaface','yolov5s_face','scrfd','arcface','yolov8l_seg'}
        cpu=[]
        if cv.intersection(self.selected):cpu.append(('cv_deploy','compose.yaml'))
        for model,directory,filename in [('sensevoice','sensevoice-cpu','compose.yaml'),('kokoro-82m','kokoro-cix','docker-compose.yml'),('matcha-zh','matcha-tts-cpu','docker-compose.yml')]:
            if model in self.selected:cpu.append((directory,filename))
        for directory,filename in cpu:self.base.compose(directory,filename,'up','-d','--no-build','--pull','never','--wait','--wait-timeout','240')
        has_accelerator=any(m in mode_map for m in self.selected)
        if has_accelerator:
            self.command('systemctl','enable','--now','houmo-ai.service')
            self.command('/usr/local/bin/modelctl','boot',timeout=720)
        self.command('systemctl','enable','--now','ai-model-gateway.service','gateway-admin.service')
        if has_accelerator:self.command('systemctl','enable','modeldeck-startup-verify.service')
        # Desktop entry is created for the actual cix user, after successful startup.
        desktop=self.home/'.local/share/applications/ai-platform.desktop'
        no_symlink_parents(desktop,self.home)
        user=pwd.getpwnam('cix');node=self.home
        for part in desktop.parent.relative_to(self.home).parts:
            node=node/part
            if not node.exists():node.mkdir();node.chmod(0o755);os.chown(node,user.pw_uid,user.pw_gid)
        if desktop.is_symlink():raise RuntimeError('已有桌面入口是链接，不覆盖')
        if desktop.exists() and 'X-AI-Platform-Managed=true' not in desktop.read_text():raise RuntimeError('已有同名桌面入口，不覆盖')
        desktop.write_text('[Desktop Entry]\nType=Application\nName=AI 平台管理中心\nExec=xdg-open http://127.0.0.1:17780/\nIcon=applications-science\nTerminal=false\nCategories=Development;\nX-AI-Platform-Managed=true\n')
        desktop.chmod(0o644);user=pwd.getpwnam('cix');os.chown(desktop,user.pw_uid,user.pw_gid)

    def acceptance(self):
        run_id=secrets.token_hex(16)
        report=self.home/'houmo-docker-deployment/state/offline-acceptance.json'
        self.state['acceptance_run_id']=run_id;self.update(force=True)
        try:
            self.command('python3',Path(__file__).with_name('verify_selected.py'),'--run-id',run_id,
                         '--selected-json',json.dumps(self.selected),'--resources',self.resources,
                         '--device-count',str(self.device_count),timeout=14400)
        except (subprocess.CalledProcessError,subprocess.TimeoutExpired) as exc:
            result=read_json(report,{})
            detail=result.get('error') if result.get('run_id')==run_id else None
            raise RuntimeError('所选模型验收失败：'+(detail or '验收进程异常退出或超时；请查看诊断摘要')) from exc
        result=read_json(report)
        self.validate_selected_acceptance(result,run_id)
        self.state['acceptance']=result;self.event(f'{len(self.selected)} 个所选模型的真实推理验收通过')

    def validate_selected_acceptance(self,result,run_id):
        if result.get('run_id')!=run_id or result.get('passed') is not True or result.get('scope')!='selected-models':
            raise RuntimeError('验收报告不是本次所选模型的成功记录')
        if set(result.get('selected_models',[]))!=set(self.selected):raise RuntimeError('验收模型与安装选择不一致')
        if result.get('device_count')!=self.device_count:raise RuntimeError('验收设备卡数与安装计划不一致')
        checks=result.get('checks',[]);names=[x.get('name') for x in checks]
        if len(names)!=len(set(names)) or any(x.get('passed') is not True for x in checks):raise RuntimeError('验收项目重复或失败')
        expected={'selected-containers-and-autostart','selected-gateway-registry'}
        modes={mode for mode,model in ACCELERATOR_MODELS.items() if model in self.selected}
        expected.update('accelerator-mode:'+mode for mode in modes)
        if modes:
            initial=(self.home/'houmo-docker-deployment/state/active-mode').read_text().strip()
            expected.add('restore-mode:'+initial)
        expected.update('inference:'+m for m in self.selected if m not in ACCELERATOR_MODELS.values())
        for model in ('qwen3.8-27b','qwen3.6-35b-a3b'):
            if model in self.selected:expected.add('multimodal:'+model)
        if set(names)!=expected:raise RuntimeError('所选模型的真实推理验收项目不完整')

    def run(self):
        self.root.joinpath('private').mkdir(parents=True,exist_ok=True,mode=0o700)
        lock=(self.root/'private/install.lock').open('a')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.state.pop('error',None)
        self.state['status']='running';self.state['bundle']=str(self.bundle);self.state['selected_models']=self.selected;self.state['device_count']=self.device_count
        self.state['installer_version']=self.release.get('installer_version','legacy');self.update(force=True)
        try:
            selected_components=None
            if self.workflow:
                from workflow import requested_components,resolved_components
                component_settings=self.workflow.settings['components']
                selected_components=requested_components(component_settings)
                self.state['requested_components']=selected_components
                self.state['selected_components']=resolved_components(component_settings)
                self.update(force=True)
            checks=quick_checks(self.bundle,self.selected,selected_components)
            failed=[r['name']+': '+r['detail'] for r in checks if not r['ok']]
            if failed:raise RuntimeError('\n'.join(failed))
            # Full legacy OS package/device checks, without its directory blanket-rejection,
            # are implemented explicitly before any project service is installed.
            for package in ['init-system-helpers','iptables','nftables','libc6','libsystemd0','libseccomp2']:
                if subprocess.check_output(['dpkg-query','-W','-f=${Status}',package],text=True).strip()!='install ok installed':raise RuntimeError('缺少基础系统包：'+package)
            conflict=subprocess.run(['dpkg-query','-W','-f=${Status}','docker.io'],capture_output=True,text=True)
            if not self.workflow and conflict.returncode==0 and conflict.stdout.strip()=='install ok installed':raise RuntimeError('docker.io 与离线 Docker CE 冲突，不自动移除')
            docker_root=Path('/var/lib/docker')
            if shutil.which('docker'):
                p=subprocess.run(['docker','info','--format','{{.DockerRootDir}}'],capture_output=True,text=True)
                if p.returncode==0 and p.stdout.strip():docker_root=Path(p.stdout.strip())
            while not docker_root.exists():docker_root=docker_root.parent
            if docker_root.stat().st_dev!=self.home.stat().st_dev and shutil.disk_usage(docker_root).free<self.docker_space_budget():raise RuntimeError('Docker 数据分区空间不足')
            if self.storage:
                self.resource_plan();self.update(force=True)
            elif shutil.disk_usage(self.root).free<8*1024**3:raise RuntimeError('安装状态分区至少需要 8 GiB 暂存空间')
            actions={'acquire':self.acquire,'verify':self.verify,'prepare':self.prepare,'conflicts':self.conflicts,'docker':self.docker,'payload':self.payload,'credentials':self.credentials,'images':lambda:self.base.images(self.manifest),'systemd':self.systemd,'start':self.start,'acceptance':self.acceptance}
            actions.update(probe=self.probe,components=self.components,yiqiban=self.yiqiban,
                           **{'yiqiban-check':self.yiqiban_check},integration=self.integration)
            for name,title in STEPS:
                if name=='probe' and self.workflow and getattr(self.workflow,'skip_existing_probe',lambda:False)():
                    if name not in self.state['completed']:self.state['completed'].append(name)
                    continue
                if name=='integration' and self.state.get('probe_skipped'):title='联检组件库和模型平台'
                if name=='components' and self.state.get('probe_skipped'):title='安装所选组件库并检查健康状态'
                self.current=name;self.pause_point();self.update(title,0,force=True)
                # Always revalidate the resource source and file conflicts on resume.
                if name=='credentials' and name in self.state['completed']:
                    for path in [self.home/'gateway-admin/.env',self.home/'houmo-docker-deployment/.env']:
                        if path.is_symlink() or not path.is_file():raise RuntimeError('已完成的凭据文件缺失；不自动生成新密钥：'+str(path))
                    self.event('继续任务，已完成：'+title);continue
                self.event(title);actions[name]()
                if name not in self.state['completed']:self.state['completed'].append(name)
                self.update(fraction=1,force=True)
            self.update('平台安装与翼起办模型联通检查完成；业务授权待完成' if self.release.get('yiqiban_resource_id') else '整套平台安装完成，全部验收通过',1,force=True,status='installed',finished_at=time.time())
        except Paused as exc:self.update(str(exc),force=True,status='paused')
        except Exception as exc:
            self.update(str(exc)[:1500],force=True,status='failed',error=str(exc)[:1500])
            raise
        finally:lock.close()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--bundle',required=True);args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit('安装后台必须由系统授权启动')
    os.umask(0o077)
    for p,mode in [(STATE_ROOT,0o755),(STATE_ROOT/'public',0o755),(STATE_ROOT/'private',0o700)]:
        if p.is_symlink():raise SystemExit('安装器状态目录不能是符号链接')
        p.mkdir(parents=True,exist_ok=True);p.chmod(mode)
    with (STATE_ROOT/'private/worker.log').open('a') as log:
        os.dup2(log.fileno(),1);os.dup2(log.fileno(),2)
        Installer(args.bundle).run()

if __name__=='__main__':main()
