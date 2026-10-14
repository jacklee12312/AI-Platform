"""Anonymous OCI Distribution pull with scoped bearer tokens kept only in memory."""
import hashlib,json,re
from urllib import error,parse,request

class RegistryError(RuntimeError):pass

def origin(url):
    p=parse.urlsplit(url)
    return p.scheme,p.hostname,p.port or (443 if p.scheme=='https' else 80)

class SameOriginRedirect(request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if origin(req.full_url)!=origin(newurl):raise RegistryError('仓库重定向到其他服务，未转发拉取令牌')
        return super().redirect_request(req,fp,code,msg,headers,newurl)

class RegistryClient:
    def __init__(self,source,timeout=45):
        self.source=source;self.base=source['registry'].rstrip('/');self.repository=source['repository'];self.timeout=timeout
        self.opener=request.build_opener(SameOriginRedirect());self.token=None;self.checked=False
    def _token(self,challenge):
        if not challenge.lower().startswith('bearer '):raise RegistryError('仓库不支持匿名 Bearer 拉取')
        values=request.parse_keqv_list(request.parse_http_list(challenge[7:]));realm=values.get('realm','')
        p=parse.urlsplit(realm)
        if origin(realm)!=origin(self.base) or p.username or p.password or p.fragment or p.query:
            raise RegistryError('仓库令牌服务地址不符合来源安全约束')
        url=realm+'?'+parse.urlencode({'service':values.get('service',''),'scope':'repository:'+self.repository+':pull'})
        with self.opener.open(request.Request(url),timeout=self.timeout) as response:
            raw=response.read(1024**2+1)
        if len(raw)>1024**2:raise RegistryError('仓库令牌响应过大')
        data=json.loads(raw);token=data.get('token') or data.get('access_token')
        if not isinstance(token,str) or not token or '\r' in token or '\n' in token:raise RegistryError('仓库未授予匿名拉取权限')
        self.token=token
    def open(self,req,timeout=None):
        if origin(req.full_url)!=origin(self.base):raise RegistryError('仓库请求超出已配置服务')
        for attempt in range(2):
            headers=dict(req.header_items())
            if self.token:headers['Authorization']='Bearer '+self.token
            outgoing=request.Request(req.full_url,headers=headers,method=req.get_method())
            try:return self.opener.open(outgoing,timeout=timeout or self.timeout)
            except error.HTTPError as exc:
                code=exc.code;challenge=exc.headers.get('WWW-Authenticate','');exc.close()
                if code==404:raise RegistryError('仓库 '+self.repository+':'+self.source.get('reference','')+' 尚未上传完成或标签不存在（HTTP 404）') from None
                if code!=401 or attempt:raise RegistryError('仓库资源不可访问（HTTP '+str(code)+'）') from None
                self.token=None
                try:self._token(challenge)
                except error.HTTPError as denied:
                    denied.close();raise RegistryError('仓库未授予匿名拉取权限') from None
        raise RegistryError('仓库鉴权失败')
    def verify_manifest(self):
        if self.checked:return
        pinned=self.source.get('manifest_digest')
        reference=pinned or self.source['reference']
        url=self.base+'/v2/'+self.repository+'/manifests/'+parse.quote(reference,safe=':')
        with self.open(request.Request(url,headers={'Accept':'application/vnd.oci.image.manifest.v1+json'})) as response:raw=response.read(4*1024**2+1)
        if len(raw)>4*1024**2 or (pinned and 'sha256:'+hashlib.sha256(raw).hexdigest()!=pinned):
            raise RegistryError('仓库清单摘要变化，拒绝自动更换模型版本')
        manifest=json.loads(raw)
        if not any(layer.get('digest')==self.source['blob_digest'] and layer.get('size')==self.source['blob_size'] for layer in manifest.get('layers',[])):
            raise RegistryError('仓库清单不包含已锁定资源层')
        self.checked=True
    def blob_url(self):return self.base+'/v2/'+self.repository+'/blobs/'+self.source['blob_digest']

def validate_sources(config,safe_relative,validate_url,exception):
    sources=config.get('oci_artifacts',{})
    if not isinstance(sources,dict):raise exception('OCI 来源必须是对象')
    owners={}
    for model,s in sources.items():
        if not isinstance(model,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,80}',model):raise exception('OCI 模型标识无效')
        if not isinstance(s,dict):raise exception('OCI 来源格式错误')
        validate_url(s.get('registry',''),config.get('allow_http',False));p=parse.urlsplit(s['registry'])
        if p.path not in ('','/') or p.query:raise exception('OCI registry 只能填写协议、主机和端口')
        if not re.fullmatch(r'[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*',s.get('repository','')):raise exception('OCI 仓库名称无效')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',s.get('blob_digest','')):raise exception('OCI 摘要无效')
        pinned=s.get('manifest_digest')
        if pinned is None:
            if s.get('resolve_tag') is not True or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}',s.get('reference','')):
                raise exception('未锁定清单仅可使用显式标签解析，并继续校验固定模型摘要')
        elif not re.fullmatch(r'sha256:[0-9a-f]{64}',pinned):raise exception('OCI 清单摘要无效')
        if type(s.get('blob_size')) is not int or not 0<s['blob_size']<=1024**4:raise exception('OCI 资源大小无效')
        if s.get('format') not in ['blob','tar+gzip'] or not isinstance(s.get('files'),dict) or not s['files']:raise exception('OCI 文件映射格式错误')
        count=s.get('device_count')
        if count is not None and (type(count) is not int or count not in (1,2)):raise exception('OCI 设备来源只允许单卡或双卡')
        names=set()
        for path,member in s['files'].items():
            prior=owners.setdefault(path,set())
            if safe_relative(path).as_posix()!=path or count in prior or (prior and (count is None or None in prior)):
                raise exception('OCI 目标路径重复或不规范')
            prior.add(count)
            if s['format']=='blob':
                if member is not None:raise exception('OCI 单文件来源不应设置归档成员')
            else:
                if not isinstance(member,str) or safe_relative(member).as_posix()!=member or member in names:raise exception('OCI 归档路径重复或不规范')
                names.add(member)
        if s['format']=='blob' and len(s['files'])!=1:raise exception('OCI 单文件来源只能对应一个文件')
    return sources
