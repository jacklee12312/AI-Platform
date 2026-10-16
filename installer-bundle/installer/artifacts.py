"""Signed resource catalog, interchangeable sources and resumable content-addressed cache."""
from __future__ import annotations
import contextlib,functools,hashlib,http.client,json,os,re,shutil,socket,subprocess,tarfile,tempfile,time
from pathlib import Path
from urllib import error,parse,request
from common import read_json,atomic_json,sha256,safe_relative,no_symlink_parents
from version import VERSION

from storage import default_cache
CACHE=default_cache()
LEGACY_CACHE=Path('/var/cache/ai-platform')
CATEGORIES={'platform','docker','runtime','images','models'}

class SourceError(RuntimeError):pass

def verify_signature(bundle):
    bundle=Path(bundle);release=read_json(bundle/'release.json')
    catalog=bundle/'catalog/release-catalog.json';key=bundle/'catalog/release-public.pem'
    if sha256(catalog)!=release['catalog_sha256'] or sha256(key)!=release['catalog_public_key_sha256']:
        raise SourceError('发行清单或公钥不匹配，请使用完整的同版本安装器')
    executable=shutil.which('openssl')
    if not executable:raise SourceError('缺少系统 openssl，无法验证发行清单签名')
    p=subprocess.run([executable,'pkeyutl','-verify','-pubin','-inkey',str(key),'-rawin','-in',str(catalog),'-sigfile',str(bundle/'catalog/release-catalog.sig')],capture_output=True,timeout=20)
    if p.returncode:raise SourceError('发行清单签名验证失败')

def load_catalog(bundle,verify=True):
    bundle=Path(bundle)
    if verify:verify_signature(bundle)
    catalog=read_json(bundle/'catalog/release-catalog.json');release=read_json(bundle/'release.json')
    if catalog.get('schema')!=1 or catalog.get('release_id')!=release['release_id']:raise SourceError('不支持的资源清单版本')
    if sha256(bundle/'catalog/platform-manifest.json')!=catalog['platform_manifest_sha256']:raise SourceError('平台清单与发行签名不符')
    paths=set();digests={}
    for row in catalog['resources']:
        rel=row['path'];normalized=safe_relative(rel).as_posix()
        if normalized!=rel or rel in paths:raise SourceError('资源路径重复或不规范')
        paths.add(rel)
        if not re.fullmatch(r'[0-9a-f]{64}',row['sha256']) or type(row['size']) is not int or row['size']<0 or row['category'] not in CATEGORIES:raise SourceError('资源摘要、大小或类别无效')
        if row.get('device_count') is not None and (row['category']!='models' or row['device_count'] not in (1,2)):
            raise SourceError('设备专用模型资源缺少有效的单卡或双卡标记')
        if row['sha256'] in digests and digests[row['sha256']]!=row['size']:raise SourceError('同一摘要的大小冲突')
        digests[row['sha256']]=row['size']
    if catalog.get('workflow_sha256'):
        from workflow import load_workflow
        load_workflow(bundle,catalog)
    return catalog

def validate_url(url,allow_http=False):
    if not isinstance(url,str) or not url.strip():raise SourceError('下载地址为空')
    p=parse.urlsplit(url)
    if p.scheme not in ({'https','http'} if allow_http else {'https'}) or not p.hostname or p.username or p.password or p.fragment:
        raise SourceError('下载地址应为 HTTPS；内网 HTTP 需在来源设置中启用，不支持 URL 内嵌账号')
    return url

def source_config(bundle,data=None):
    config=read_json(Path(os.environ.get('AI_PLATFORM_SOURCES_FILE') or Path(bundle)/'sources.json')) if data is None else data
    if config.get('schema')!=1 or config.get('mode') not in ['prefer-local','local-only','remote-only']:raise SourceError('资源来源配置格式错误')
    if config.get('model_source','local' if config.get('mode')=='local-only' else 'online') not in ['online','local']:raise SourceError('模型来源只能选择联网下载或本地导入')
    if not isinstance(config.get('local_roots'),list) or any(not isinstance(r,str) or not r.strip() for r in config['local_roots']):raise SourceError('本地源应为目录列表')
    if type(config.get('allow_http',False)) is not bool:raise SourceError('allow_http 应为布尔值')
    for key in ['base_urls','file_urls']:
        if not isinstance(config.get(key,{}),dict):raise SourceError('来源地址表格式错误')
        for name,urls in config.get(key,{}).items():
            if key=='base_urls' and name not in CATEGORIES:raise SourceError('未知资源类别')
            if key=='file_urls':safe_relative(name)
            if not isinstance(urls,list):raise SourceError('地址应使用数组；待配置项保留为空数组')
            for url in urls:validate_url(url,config.get('allow_http',False))
    for key,default,maximum in [('timeout_seconds',45,300),('retries',3,10)]:
        v=config.get(key,default)
        if type(v) is not int or not 1<=v<=maximum:raise SourceError('下载超时或重试次数超出范围')
    from registry import validate_sources
    validate_sources(config,safe_relative,validate_url,SourceError)
    return config

def source_roots(bundle,config):
    base=os.environ.get('AI_PLATFORM_SOURCE_BASE') or str(Path(bundle).absolute())
    roots=tuple(str(Path(bundle).absolute()/safe_relative(r[8:])) if r.startswith('@bundle/') else r for r in config['local_roots'])
    return _resolved_roots(base,roots)

def worker_source_roots(bundle,config):
    """Pin external roots while retaining trusted bundle markers for online models."""
    resolved=source_roots(bundle,config)
    return [reference if reference.startswith('@bundle/') else str(root)
            for reference,root in zip(config['local_roots'],resolved)]

@functools.lru_cache(maxsize=32)
def _resolved_roots(bundle,roots):
    return tuple((Path(bundle)/r).resolve() if not Path(r).is_absolute() else Path(r).resolve() for r in roots)

def local_candidates(bundle,config,row):
    if config['mode']=='remote-only':return []
    result=[]
    roots=source_roots(bundle,config)
    if row['category']=='models' and config.get('model_source','local' if config['mode']=='local-only' else 'online')=='online':
        roots=[root for reference,root in zip(config['local_roots'],roots) if reference.startswith('@bundle/')]
    for root in roots:
        for relative in ['files/'+row['path'],row['path'],'blobs/sha256/'+row['sha256']]:
            p=root/safe_relative(relative)
            try:
                if p.is_file() and not p.is_symlink() and p.stat().st_size==row['size']:
                    no_symlink_parents(p,root);result.append(p)
            except (OSError,RuntimeError):continue
    return result

def remote_candidates(config,row):
    if config['mode']=='local-only':return []
    if row['category']=='models' and config.get('model_source','local' if config['mode']=='local-only' else 'online')=='local':return []
    urls=list(config.get('file_urls',{}).get(row['path'],[]))
    for base in config.get('base_urls',{}).get(row['category'],[]):
        # Base URLs represent a repository root; per-file signed URLs belong in file_urls.
        if parse.urlsplit(base).query:raise SourceError('仓库根地址不能带查询参数；临时签名链接请填入 file_urls')
        urls.append(base.rstrip('/')+'/files/'+parse.quote(row['path'],safe='/'))
    return list(dict.fromkeys(urls))

def blob_path(cache,row):return Path(cache)/'blobs/sha256'/row['sha256']

def legacy_candidates(cache,row):
    if Path(cache)==LEGACY_CACHE:return []
    p=blob_path(LEGACY_CACHE,row)
    try:
        no_symlink_parents(p,LEGACY_CACHE)
        return [p] if p.is_file() and not p.is_symlink() and p.stat().st_size==row['size'] else []
    except (OSError,RuntimeError):return []

def oci_source(config,row):
    if config['mode']=='local-only':return None
    if row['category']=='models' and config.get('model_source','local' if config['mode']=='local-only' else 'online')=='local':return None
    candidates=[(model,s) for model,s in config.get('oci_artifacts',{}).items()
                if row['path'] in s['files'] and
                (s.get('device_count') is None or s['device_count']==row.get('selected_device_count',row.get('device_count')))]
    if len(candidates)>1:raise SourceError('模型存在多个匹配的设备来源：'+row['path'])
    return candidates[0] if candidates else None

def plan_sources(bundle,catalog,config,cache=CACHE):
    config=source_config(bundle,config);rows=[];archives={};transfer_bytes=0
    for r in catalog['resources']:
        p=blob_path(cache,r)
        valid_cache=False
        try:
            no_symlink_parents(p,Path(cache));valid_cache=not p.is_symlink() and p.is_file() and p.stat().st_size==r['size']
        except (OSError,RuntimeError):pass
        local=legacy_candidates(cache,r)+local_candidates(bundle,config,r);remote=remote_candidates(config,r)
        oci=oci_source(config,r)
        state='cache' if valid_cache else 'local' if local else 'remote' if remote or oci else 'missing'
        rows.append({'path':r['path'],'size':r['size'],'category':r['category'],'models':r['models'],'source':state,
                     'selected_device_count':r.get('selected_device_count',r.get('device_count'))})
        if state=='remote':
            if oci and oci[1]['format']=='tar+gzip':archives[oci[1]['blob_digest']]=oci[1]['blob_size']
            else:transfer_bytes+=r['size']
    totals={s:sum(r['size'] for r in rows if r['source']==s) for s in ['cache','local','remote','missing']}
    archive_bytes=sum(archives.values())
    return {'ready':not any(r['source']=='missing' for r in rows),'rows':rows,'bytes':totals,'total_bytes':sum(r['size'] for r in rows),'cache_bytes_needed':sum(r['size'] for r in rows if r['source']!='cache')+archive_bytes,'archive_bytes':archive_bytes,'remote_transfer_bytes':transfer_bytes+archive_bytes,'note':'来源存在不代表远端实测可用；准备阶段验证仓库清单、完整资源 SHA-256 和归档内文件'}

class RestrictedRedirect(request.HTTPRedirectHandler):
    def __init__(self,allow_http):self.allow_http=allow_http
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        validate_url(newurl,self.allow_http)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

@contextlib.contextmanager
def file_lock(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_symlink():raise SourceError('缓存锁不能是链接')
    with path.open('a+b') as f:
        if os.name=='nt':
            import msvcrt
            f.seek(0);f.write(b'0');f.flush();f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:yield
        finally:
            if os.name=='nt':f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)

class BlobStore:
    def __init__(self,bundle,config,cache=CACHE,progress=None,pause=None,rows=None):
        self.bundle=Path(bundle);self.config=source_config(bundle,config);self.cache=Path(cache)
        self.progress=progress or (lambda row,n,phase:None);self.pause=pause or (lambda:None)
        self.opener=request.build_opener(RestrictedRedirect(self.config.get('allow_http',False)))
        self.clients={};self.rows={r['path']:r for r in (rows or [])}
        if self.config.get('oci_artifacts') and rows is None:self.rows={r['path']:r for r in load_catalog(self.bundle)['resources']}
    def check(self,p,row):
        return p.is_file() and not p.is_symlink() and p.stat().st_size==row['size'] and sha256(p,lambda n,s:(self.pause(),self.progress(row,n,'校验')))==row['sha256']
    def _copy_local(self,source,part,row):
        with source.open('rb') as src,part.open('wb') as dst:
            self._stream(src,dst,row,0)
            dst.flush();os.fsync(dst.fileno())
    def _stream(self,src,dst,row,start):
        done=start
        while True:
            self.pause()
            block=src.read(min(1024**2,row['size']-done+1))
            if not block:break
            done+=len(block)
            if done>row['size']:raise SourceError('下载内容大于发行清单规定的大小')
            dst.write(block);self.progress(row,done,'读取' if not hasattr(src,'status') else '下载')
        if done!=row['size']:raise SourceError('传输未完成，保留断点以便继续')
    def _download(self,url,part,row,open_request=None):
        validate_url(url,self.config.get('allow_http',False))
        offset=part.stat().st_size if part.exists() else 0
        if offset>row['size']:
            part.unlink();offset=0
        if offset==row['size'] and self.check(part,row):return
        if offset==row['size']:part.unlink();offset=0
        headers={'Accept-Encoding':'identity','User-Agent':'AI-Platform-Installer/'+VERSION}
        if offset:headers['Range']='bytes='+str(offset)+'-'
        req=request.Request(url,headers=headers)
        with (open_request or self.opener.open)(req,timeout=self.config.get('timeout_seconds',45)) as response:
            status=response.status
            if response.headers.get('Content-Encoding','identity').lower()!='identity':raise SourceError('下载源返回了不支持的内容编码')
            if status==206:
                value=response.headers.get('Content-Range','')
                m=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',value)
                if not m or tuple(map(int,m.groups()))!=(offset,row['size']-1,row['size']):raise SourceError('下载源返回的断点范围不匹配')
            elif status==200:offset=0
            else:raise SourceError('下载源未返回可用文件')
            length=response.headers.get('Content-Length')
            if length is not None and int(length)!=row['size']-offset:raise SourceError('下载源返回的文件长度不匹配')
            with part.open('ab' if offset else 'wb') as dst:
                self._stream(response,dst,row,offset);dst.flush();os.fsync(dst.fileno())
    def ensure(self,row):
        target=blob_path(self.cache,row);no_symlink_parents(target,self.cache)
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.is_symlink():raise SourceError('缓存内容不能是链接')
        part=target.with_suffix('.part')
        if part.is_symlink():raise SourceError('下载断点不能是链接')
        lock=self.cache/'locks'/row['sha256'];no_symlink_parents(lock,self.cache)
        with file_lock(lock):
            if self.check(target,row):return target
            if target.exists():target.unlink()  # Only this exact digest-owned corrupt cache entry.
            for source in legacy_candidates(self.cache,row)+local_candidates(self.bundle,self.config,row):
                try:
                    self._copy_local(source,part,row)
                    if self.check(part,row):return self._publish(part,target)
                    part.unlink(missing_ok=True)
                except (OSError,SourceError):continue
            urls=remote_candidates(self.config,row)
            for url in urls:
                for attempt in range(self.config.get('retries',3)):
                    self.pause()
                    try:
                        self._download(url,part,row)
                        if not self.check(part,row):
                            part.unlink(missing_ok=True);raise SourceError('下载内容 SHA-256 不匹配')
                        return self._publish(part,target)
                    except (OSError,error.URLError,http.client.HTTPException,SourceError,ValueError) as exc:
                        if isinstance(exc,error.HTTPError):exc.close()
                        self.progress(row,part.stat().st_size if part.exists() else 0,'等待重试')
                        if attempt+1<self.config.get('retries',3):time.sleep(min(2,0.25*2**attempt))
        # OCI acquisition happens outside the per-file lock to avoid archive/file lock inversion.
        source=oci_source(self.config,row)
        if source:return self._ensure_oci(row,*source)
        # Deliberately omit URLs, response bodies and tokens from public diagnostic messages.
        raise SourceError('资源不可用：'+row['path']+'；请补齐本地文件或配置有效下载地址，已有断点已保留')

    def _ensure_oci(self,row,model,source):
        from registry import RegistryClient,RegistryError
        client=self.clients.setdefault(model,RegistryClient(source,self.config.get('timeout_seconds',45)))
        blob={'path':'OCI/'+model,'sha256':source['blob_digest'][7:],'size':source['blob_size'],'category':'models','models':[model]}
        if source['format']=='blob' and (blob['sha256']!=row['sha256'] or blob['size']!=row['size']):raise SourceError('OCI 文件不匹配发行清单：'+row['path'])
        target=blob_path(self.cache,blob);no_symlink_parents(target,self.cache);target.parent.mkdir(parents=True,exist_ok=True)
        part=target.with_suffix('.part');lock=self.cache/'locks'/blob['sha256'];no_symlink_parents(lock,self.cache)
        if target.is_symlink() or part.is_symlink():raise SourceError('OCI 缓存或断点不能是链接')
        with file_lock(lock):
            if not self.check(target,blob):
                success=False
                for attempt in range(self.config.get('retries',3)):
                    self.pause()
                    try:
                        client.verify_manifest();self._download(client.blob_url(),part,blob,client.open)
                        if not self.check(part,blob):part.unlink(missing_ok=True);raise SourceError('OCI 资源层摘要不匹配')
                        self._publish(part,target);success=True;break
                    except (OSError,RegistryError,SourceError,http.client.HTTPException,ValueError):
                        if attempt+1<self.config.get('retries',3):time.sleep(min(2,0.25*2**attempt))
                if not success:raise SourceError('OCI 资源不可用：'+model+'；请检查网络、匿名拉取权限或锁定版本；断点已保留')
        if source['format']=='blob':return target
        archive_lock=self.cache/'locks'/('archive-'+blob['sha256']);no_symlink_parents(archive_lock,self.cache)
        with file_lock(archive_lock):
            output=blob_path(self.cache,row);no_symlink_parents(output,self.cache)
            if not self.check(output,row):self._unpack_oci(target,model,source)
        if not self.check(output,row):raise SourceError('归档中缺少匹配的模型文件：'+row['path'])
        return output

    def _unpack_oci(self,archive,model,source):
        members={}
        for path,name in source['files'].items():
            row=self.rows.get(path)
            if row is None:continue
            if row['category']!='models':raise SourceError('OCI 模型归档不能部署平台文件')
            members[name]=row
        found=set();seen=set();expanded=0;limit=sum(r['size'] for r in members.values())*2+64*1024**2
        with tarfile.open(archive,'r|gz') as tar:
            for item in tar:
                self.pause();name=safe_relative(item.name).as_posix()
                if name in seen or len(seen)>50000:raise SourceError('模型归档包含重复或过多路径')
                seen.add(name)
                if item.isdir():continue
                if not item.isfile():raise SourceError('模型归档包含链接或特殊文件，拒绝解包')
                expanded+=item.size
                if expanded>limit:raise SourceError('模型归档展开大小超过安全预算')
                if name not in members:continue
                row=members[name];found.add(name)
                if item.size!=row['size']:raise SourceError('归档内模型文件大小与发行清单不符：'+row['path'])
                target=blob_path(self.cache,row);no_symlink_parents(target,self.cache);target.parent.mkdir(parents=True,exist_ok=True)
                part=target.with_suffix('.unpack.part');lock=self.cache/'locks'/row['sha256'];no_symlink_parents(lock,self.cache)
                if target.is_symlink() or part.is_symlink():raise SourceError('模型解包目标不能是链接')
                with file_lock(lock):
                    if self.check(target,row):continue
                    try:
                        with tar.extractfile(item) as src,part.open('wb') as dst:
                            self._stream(src,dst,row,0);dst.flush();os.fsync(dst.fileno())
                        if not self.check(part,row):raise SourceError('归档内模型 SHA-256 与发行清单不符：'+row['path'])
                        self._publish(part,target)
                    finally:part.unlink(missing_ok=True)
        if found!=set(members):raise SourceError('模型归档缺少发行清单所需文件：'+model)
    @staticmethod
    def _publish(part,target):
        part.chmod(0o644);os.replace(part,target);return target

def materialize(bundle,catalog,config,destination,cache=CACHE,progress=None,pause=None):
    """Fetch everything before preparing the deployable tree; no application changes here."""
    plan=plan_sources(bundle,catalog,config,cache)
    if not plan['ready']:
        missing=[r for r in plan['rows'] if r['source']=='missing']
        names=sorted({m for r in missing for m in r['models']})
        raise SourceError('资源源尚未配置完整；缺少 '+str(len(missing))+' 个文件'+('，模型：'+', '.join(names) if names else '')+'。请配置来源后继续。')
    store=BlobStore(bundle,config,cache,progress,pause,rows=catalog['resources']);paths=[]
    for row in catalog['resources']:paths.append((row,store.ensure(row)))
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    for row,source in paths:
        store.pause();target=destination/safe_relative(row['path']);no_symlink_parents(target,destination)
        target.parent.mkdir(parents=True,exist_ok=True)
        if os.path.lexists(target):
            if target.is_symlink():raise SourceError('资源暂存目标不能是链接')
            if store.check(target,row):continue
            target.unlink()
        try:os.link(source,target)
        except OSError:
            fd,name=tempfile.mkstemp(prefix='.acquire-',dir=destination.parent);os.close(fd)
            try:
                shutil.copyfile(source,name)
                if not store.check(Path(name),row):raise SourceError('暂存资源写入后校验失败')
                os.chmod(name,0o644);os.replace(name,target)
            finally:
                if os.path.exists(name):os.unlink(name)
    return plan
