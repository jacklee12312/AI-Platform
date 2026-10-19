"""Entry point embedded in the executable Python ZIP installer."""
import hashlib,json,os,shutil,stat,subprocess,sys,tempfile,zipfile
from pathlib import Path,PurePosixPath

MAX_BUNDLE_BYTES=16*1024**3

def archive_size(entries):
    total=sum(item.file_size for item in entries if item.filename!='__main__.py' and not item.is_dir())
    if total>MAX_BUNDLE_BYTES:raise RuntimeError('安装器大小超出预期')
    return total

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(4*1024**2),b''):h.update(block)
    return h.hexdigest()

def verify(root,manifest):
    for r in manifest['files']:
        p=root/r['path']
        if not p.resolve().is_relative_to(root.resolve()) or p.is_symlink() or not p.is_file() or sha(p)!=r['sha256']:
            raise RuntimeError('安装器文件校验失败：'+r['path'])

def main():
    archive=Path(sys.argv[0]).resolve();args=sys.argv[1:]
    extraction_only=args[:1]==['--extract-only']
    if extraction_only:
        if len(args)!=2:raise RuntimeError('--extract-only 后需要一个尚不存在的目标目录')
        target=Path(args[1]).resolve()
        if target.exists():raise RuntimeError('解包目标已存在，未覆盖')
    else:
        target=Path.home()/'.cache/ai-platform/setup'/sha(archive)[:24]
    if target.is_symlink():raise RuntimeError('安装器缓存不能是链接')
    target.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        manifest=json.loads(z.read('bundle/installer-manifest.json'))
        expected_size=archive_size(z.infolist())
        if not target.exists():
            stage=Path(tempfile.mkdtemp(prefix='.ai-setup-',dir=target.parent))
            try:
                total=0
                for item in z.infolist():
                    if item.filename=='__main__.py':continue
                    p=PurePosixPath(item.filename)
                    if p.is_absolute() or '..' in p.parts or '\\' in item.filename or p.parts[0]!='bundle' or stat.S_ISLNK(item.external_attr>>16):raise RuntimeError('安装器压缩包路径无效')
                    if item.is_dir():continue
                    total+=item.file_size
                    if total>expected_size:raise RuntimeError('安装器大小超出预期')
                    out=stage/Path(*p.parts[1:]);out.parent.mkdir(parents=True,exist_ok=True)
                    with z.open(item) as src,out.open('xb') as dst:shutil.copyfileobj(src,dst)
                    out.chmod(0o644)
                verify(stage,manifest)
                try:stage.rename(target)
                except FileExistsError:verify(target,manifest)
            finally:
                if stage.exists():
                    assert stage.parent==target.parent and stage.name.startswith('.ai-setup-')
                    shutil.rmtree(stage)
        verify(target,manifest)
    if extraction_only:print(json.dumps({'extracted_to':str(target),'verified':True,'installation_started':False}));return 0
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',AI_PLATFORM_SOURCE_BASE=str(archive.parent/'installer-source-base'))
    sources=archive.with_suffix('.sources.json')
    if sources.is_file():env['AI_PLATFORM_SOURCES_FILE']=str(sources)
    return subprocess.call([sys.executable,str(target/'installer/bootstrap.py'),*args],env=env)

if __name__=='__main__':
    try:sys.exit(main())
    except Exception as exc:print('启动失败：'+str(exc),file=sys.stderr);sys.exit(1)
