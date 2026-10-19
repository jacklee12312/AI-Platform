"""Launch native Qt from bundled wheels, with no pip/network/system-package install."""
import hashlib,io,json,os,platform,subprocess,sys,tarfile,zipfile
from pathlib import Path
from browser_open import ENV_MARKER,capture_desktop_environment

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(4*1024**2),b''):h.update(b)
    return h.hexdigest()

def unpack_private_libs(deb,destination):
    """Read .deb data archive, flatten only ARM64 shared libraries; do not run maintainer scripts."""
    with deb.open('rb') as f:
        if f.read(8)!=b'!<arch>\n':raise RuntimeError('无效 GUI 依赖包')
        data=None
        while header:=f.read(60):
            if len(header)!=60:raise RuntimeError('依赖包头不完整')
            name=header[:16].decode().strip().rstrip('/');size=int(header[48:58]);body=f.read(size)
            if size%2:f.read(1)
            if name.startswith('data.tar'):data=body
    if data is None:raise RuntimeError('GUI 依赖包缺少文件归档')
    destination.mkdir(parents=True,exist_ok=True);links=[]
    with tarfile.open(fileobj=io.BytesIO(data),mode='r:*') as archive:
        for item in archive:
            p=Path(item.name)
            if p.parent.as_posix().lstrip('./') not in ['usr/lib/aarch64-linux-gnu','lib/aarch64-linux-gnu'] or not p.name.startswith('lib') or '.so' not in p.name:continue
            target=destination/p.name
            if item.isfile():
                content=archive.extractfile(item).read()
                if target.exists() and target.read_bytes()!=content:raise RuntimeError('GUI 依赖库同名冲突')
                target.write_bytes(content);target.chmod(0o755)
            elif item.issym():
                if Path(item.linkname).name!=item.linkname:raise RuntimeError('GUI 依赖链接越界')
                links.append((target,item.linkname))
    for target,link in links:
        if not os.path.lexists(target):target.symlink_to(link)
        elif not target.is_symlink() or os.readlink(target)!=link:raise RuntimeError('GUI 链接冲突')

def main():
    root=Path(__file__).resolve().parent.parent
    if platform.system()!='Linux' or platform.machine() not in ['aarch64','arm64']:
        raise RuntimeError('这是 Linux ARM64 盒子的安装程序，请在目标盒子上启动。')
    if sys.version_info[:2]!=(3,11):raise RuntimeError('需要厂家 Debian 12 自带的 Python 3.11。')
    if not os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY') and os.environ.get('QT_QPA_PLATFORM')!='offscreen':
        raise RuntimeError('未检测到图形桌面，请在盒子桌面打开；不要从普通 SSH 窗口启动图形向导。')
    lock_path=root/'gui-runtime/lock.json';rows=json.loads(lock_path.read_text())
    system_lock=root/'gui-runtime/system-libs-lock.json'
    key=hashlib.sha256((digest(lock_path)+digest(system_lock)).encode()).hexdigest()[:16];cache=Path.home()/'.cache/ai-platform-installer'/key
    marker=cache/'ready.json'
    if cache.is_symlink():raise RuntimeError('GUI 缓存目录不能是链接')
    if not marker.exists():
        cache.mkdir(parents=True,exist_ok=True)
        for item in rows:
            wheel=root/'gui-runtime/linux-arm64'/item['filename']
            if digest(wheel)!=item['sha256']:raise RuntimeError('图形运行时损坏：'+wheel.name)
            with zipfile.ZipFile(wheel) as archive:
                for name in archive.namelist():
                    p=Path(name)
                    if p.is_absolute() or '..' in p.parts:raise RuntimeError('GUI 运行时路径无效')
                archive.extractall(cache)
        for item in json.loads(system_lock.read_text())['packages']:
            deb=root/'gui-runtime/debs'/item['file']
            if digest(deb)!=item['sha256']:raise RuntimeError('GUI 依赖损坏：'+deb.name)
            unpack_private_libs(deb,cache/'system-libs')
        marker.write_text(json.dumps({'runtime':'6.7.3','source':key}))
    env=os.environ.copy();env[ENV_MARKER]=capture_desktop_environment(env)
    env['PYTHONDONTWRITEBYTECODE']='1';env['PYTHONPATH']=str(cache)
    env['QT_PLUGIN_PATH']=str(cache/'PySide6/Qt/plugins');env['QT_QPA_PLATFORM']=env.get('QT_QPA_PLATFORM') or ('xcb' if env.get('DISPLAY') else 'wayland')
    libs=cache/'system-libs'
    env['LD_LIBRARY_PATH']=str(libs)+':'+str(cache/'PySide6/Qt/lib')
    result=subprocess.run([sys.executable,str(root/'installer/wizard.py'),'--bundle',str(root),*sys.argv[1:]],env=env)
    return result.returncode

if __name__=='__main__':
    try:sys.exit(main())
    except Exception as exc:
        print('启动失败：'+str(exc),file=sys.stderr)
        if __import__('shutil').which('zenity'):
            subprocess.run(['zenity','--error','--title=AI 平台安装向导','--text='+str(exc)])
        sys.exit(1)
