#!/usr/bin/env python3
"""Offline-only fresh installation of the pinned CIX/LQ50 ModelDeck release."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
from datetime import datetime, timezone

PACKAGE=Path(__file__).resolve().parent.parent
HOME=Path('/home/cix')
DEPLOY=HOME/'houmo-docker-deployment'
STATE=Path('/var/lib/modeldeck-offline-install/state.json')
LOGDIR=Path('/var/log/modeldeck-offline-install')
CPU=[('cv_deploy','compose.yaml'),('sensevoice-cpu','compose.yaml'),('kokoro-cix','docker-compose.yml'),('matcha-tts-cpu','docker-compose.yml')]
MODES=['copaw','spark-x1','qwen38','qwen36','qwen-vl','glm-ocr','bge-m3','reranker','cosyvoice']

def run(*command, capture=False, timeout=None):
    return subprocess.run([str(x) for x in command],check=True,text=True,
                          stdout=subprocess.PIPE if capture else None,timeout=timeout).stdout

def load(path):
    return json.loads(path.read_text(encoding='utf-8'))

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(8*1024*1024),b''): h.update(block)
    return h.hexdigest()

def safe_relative(value):
    path=Path(value)
    if path.is_absolute() or '..' in path.parts or '\\' in value:
        raise RuntimeError('Unsafe package path: '+value)
    return path

def verify_package():
    manifest=load(PACKAGE/'manifests/manifest.json')
    if manifest['status']!='complete-artifacts':
        raise RuntimeError('The release is not a complete artifact set')
    rows=(PACKAGE/'manifests/SHA256SUMS').read_text().splitlines()
    listed=set()
    for index,row in enumerate(rows,1):
        digest,relative=row.split('  ',1)
        path=PACKAGE/safe_relative(relative)
        if path.is_symlink() or not path.is_file() or sha(path)!=digest:
            raise RuntimeError('Missing or corrupt package file: '+relative)
        listed.add(relative)
        if index%30==0 or path.stat().st_size>100*1024**2:
            print(f'Checksums {index}/{len(rows)}: {relative}',flush=True)
    for path in PACKAGE.rglob('*'):
        if path.is_symlink(): raise RuntimeError('Unexpected package symlink: '+str(path))
        if path.is_file() and path.relative_to(PACKAGE).as_posix() not in listed|{'manifests/SHA256SUMS'}:
            raise RuntimeError('Unexpected unverified file in package: '+str(path))
    catalog=load(PACKAGE/'manifests/model-catalog.json')
    for model in catalog['models']:
        for item in model['files']:
            p=PACKAGE/'payload/home/cix'/safe_relative(item['target'])
            if not p.is_file() or p.stat().st_size!=item['size']:
                raise RuntimeError('Missing model payload: '+item['target'])
    print('Package verification passed.',flush=True)
    return manifest

def preflight(manifest,resume):
    if os.geteuid()!=0: raise RuntimeError('Run with sudo.')
    if platform.machine() not in ['aarch64','arm64']: raise RuntimeError('ARM64 CIX/LQ50 hardware is required.')
    osinfo=dict(line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if osinfo.get('ID','').strip('"')!='debian' or osinfo.get('VERSION_ID','').strip('"')!='12':
        raise RuntimeError('The validated vendor Debian 12 ARM64 system is required.')
    user=pwd.getpwnam('cix')
    if user.pw_dir!='/home/cix': raise RuntimeError('The cix home directory must be /home/cix.')
    for device in manifest['required_devices']:
        if not Path(device).is_char_device(): raise RuntimeError('Missing device: '+device)
    if not Path('/usr/local/houmo-drv-xh2_v1.4.0').is_dir(): raise RuntimeError('Install the matching vendor XH2 v1.4.0 driver/SDK first.')
    for command in ['tar','gzip','dpkg','dpkg-deb','dpkg-query','systemctl','curl','cp','chown','chmod','usermod','runuser','sha256sum','ip','timeout','flock','tee']:
        if not shutil.which(command): raise RuntimeError('Missing base-OS command: '+command)
    for name in ['init-system-helpers','iptables','nftables','libc6','libsystemd0','libseccomp2']:
        if 'install ok installed' not in run('dpkg-query','-W','-f=${Status}',name,capture=True):
            raise RuntimeError('Missing base-OS package: '+name)
    for deb in (PACKAGE/'docker/arm64-bookworm').glob('*.deb'):
        if run('dpkg-deb','-f',deb,'Architecture',capture=True).strip()!='arm64': raise RuntimeError('Wrong DEB architecture: '+str(deb))
    if STATE.exists():
        state=load(STATE)
        if state['release_id']!=manifest['release_id']: raise RuntimeError('Another release owns this installation. This package is not an upgrade tool.')
        if not resume: raise RuntimeError('An installation record exists. Use --resume to explicitly continue this release.')
    else:
        for directory in ['houmo-docker-deployment','ai-model-gateway','gateway-admin','cv_deploy','sensevoice-cpu','kokoro-cix','matcha-tts-cpu','cix_telecom_projects','vendor-hliellama-2.1.1-6043','modeldeck-runtime','modeldeck-models']:
            path=HOME/directory
            if path.exists() and (not path.is_dir() or any(path.iterdir())):
                raise RuntimeError('Existing project detected; fresh-install only, will not overwrite: '+str(path))
        if shutil.which('docker'):
            existing=run('docker','ps','-aq',capture=True).strip()
            if existing: raise RuntimeError('Existing Docker containers detected. Use an empty deployment box; nothing was stopped.')
        for port in [17700,17780]:
            with socket.socket() as sock:
                try: sock.bind(('0.0.0.0',port))
                except OSError: raise RuntimeError(f'Port {port} is already in use.')
    if subprocess.run(['dpkg-query','-W','-f=${Status}','docker.io'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True).stdout.strip()=='install ok installed':
        raise RuntimeError('docker.io conflicts with bundled Docker CE. Ask the system administrator to resolve it; no packages were removed.')
    payload_need=manifest['installed_payload_bytes']+5*1024**3
    docker_need=manifest['docker_disk_budget_bytes']
    docker_parent=Path('/var/lib/docker')
    if shutil.which('docker'):
        query=subprocess.run(['docker','info','--format','{{.DockerRootDir}}'],capture_output=True,text=True)
        if query.returncode==0 and query.stdout.strip(): docker_parent=Path(query.stdout.strip())
    while not docker_parent.exists(): docker_parent=docker_parent.parent
    payload_parent=HOME
    same=payload_parent.stat().st_dev==docker_parent.stat().st_dev
    if shutil.disk_usage(payload_parent).free<(payload_need+docker_need if same else payload_need):
        raise RuntimeError('Not enough free space for payload and Docker; keep at least 250 GB free before copying/extracting the delivery package.')
    if not same and shutil.disk_usage(docker_parent).free<docker_need:
        raise RuntimeError('Not enough space on Docker data-root filesystem.')
    print('Hardware/base-system and space preflight passed.',flush=True)

def compose(directory,filename,*args):
    run('docker','compose','--project-directory',HOME/directory,'-f',HOME/directory/filename,*args)

def extract(archive,destination):
    destination.mkdir(parents=True,exist_ok=True)
    # Archives are allowlisted and cryptographically checked before installation.
    with tarfile.open(archive,'r:gz') as tar:
        for item in tar:
            safe_relative(item.name)
            if item.isdev() or item.isfifo(): raise RuntimeError('Unexpected special file in runtime archive')
            if item.issym() and item.linkname.startswith('/') and not item.linkname.startswith(('/home/cix/','/usr/bin/')):
                raise RuntimeError('Unexpected absolute runtime link: '+item.linkname)
    run('tar','--no-same-owner','-xzf',archive,'-C',destination)

def install_docker():
    for name in ['containerd.io','docker-ce-cli','docker-ce','docker-buildx-plugin','docker-compose-plugin']:
        packages=list((PACKAGE/'docker/arm64-bookworm').glob(name+'_*_arm64.deb'))
        if len(packages)!=1: raise RuntimeError('Expected exactly one package: '+name)
        run('dpkg','-i',packages[0])
    run('systemctl','enable','--now','docker')
    run('usermod','-aG','docker','cix')

def install_payload():
    # This stage is repeatable only inside the fresh-install journal, before runtime credentials are created.
    run('cp','-a','--reflink=auto',str(PACKAGE/'payload/home/cix')+'/.',str(HOME)+'/')
    extract(PACKAGE/'runtime/cosyvoice-runtime.tar.gz',HOME)
    extract(PACKAGE/'runtime/host-libs.tar.gz',HOME/'modeldeck-runtime/lib')
    extract(PACKAGE/'runtime/ui-static.tar.gz',HOME/'gateway-admin/frontend/dist')
    (HOME/'cix_telecom_projects/cosyvoice-vllm-staging/logs').mkdir(parents=True,exist_ok=True)
    dirs=[p.name for p in (PACKAGE/'payload/home/cix').iterdir() if p.is_dir()]
    dirs+=['vendor-hliellama-2.1.1-6043','modeldeck-runtime','modeldeck-models']
    for name in set(dirs):
        (HOME/name).mkdir(exist_ok=True)
        run('chown','-R','cix:cix',HOME/name)
        run('chmod','-R','a+rX',HOME/name)
    run('chmod','+x',DEPLOY/'modelctl')
    for script in DEPLOY.glob('*.sh'): run('chmod','+x',script)

def credentials():
    state=DEPLOY/'state'
    state.mkdir(exist_ok=True)
    (state/'active-mode').write_text('copaw\n')
    state.chmod(0o755)
    (state/'active-mode').chmod(0o644)
    run('chown','-R','cix:cix',state)
    env=DEPLOY/'.env'
    if not env.exists(): env.write_text('HOUMO_API_KEY='+secrets.token_hex(32)+'\nGATEWAY_MANAGEMENT_TOKEN='+secrets.token_hex(32)+'\n')
    admin=HOME/'gateway-admin'
    if not (admin/'.env').exists():
        sys.path.insert(0,str(admin))
        from hash_password import encode
        password=secrets.token_urlsafe(18)
        (admin/'.env').write_text("ADMIN_PASSWORD_HASH='"+encode(password)+"'\nADMIN_AGENT_TOKEN="+secrets.token_hex(32)+'\n')
        (admin/'INITIAL_ADMIN_PASSWORD').write_text(password+'\n')
    for path in [env,admin/'.env',admin/'INITIAL_ADMIN_PASSWORD']:
        run('chown','cix:cix',path)
        path.chmod(0o600)
    for path in [HOME/'ai-model-gateway/data']:
        path.mkdir(parents=True,exist_ok=True)
        run('chown','-R','65534:65534',path)
        path.chmod(0o750)

def images(manifest):
    run('docker','load','-i',PACKAGE/'images/all-arm64-images.tar.gz')
    for expected in manifest['images']:
        actual=json.loads(run('docker','image','inspect',expected['tag'],capture=True))[0]
        if (actual['Id'] not in {expected['id'],expected['config_id']} or actual['Architecture']!='arm64' or actual['Os']!='linux'
                or actual['RootFS']['Layers']!=expected['rootfs_diff_ids']):
            raise RuntimeError('Loaded image does not match locked release: '+expected['tag'])

def setup_units():
    link=Path('/usr/local/bin/modelctl')
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or link.resolve()!=DEPLOY/'modelctl': raise RuntimeError('Existing unrelated /usr/local/bin/modelctl; not overwritten.')
    else: link.symlink_to(DEPLOY/'modelctl')
    for source,name in [(DEPLOY/'houmo-ai.service','houmo-ai.service'),(HOME/'ai-model-gateway/systemd/ai-model-gateway.service','ai-model-gateway.service'),(PACKAGE/'systemd/gateway-admin.service','gateway-admin.service'),(PACKAGE/'systemd/modeldeck-startup-verify.service','modeldeck-startup-verify.service')]:
        shutil.copyfile(source,Path('/etc/systemd/system')/name)
    run('systemctl','daemon-reload')

def start():
    args=[]
    for mode in MODES: args+=['--profile',mode]
    compose('houmo-docker-deployment','compose.yaml',*args,'up','--no-start','--no-build','--pull','never')
    for directory,filename in CPU:
        compose(directory,filename,'up','-d','--no-build','--pull','never','--wait','--wait-timeout','240')
    run('systemctl','enable','--now','houmo-ai.service')
    run('systemctl','enable','--now','ai-model-gateway.service','gateway-admin.service')
    run('systemctl','enable','modeldeck-startup-verify.service')

def save_state(value):
    temporary=STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,indent=2))
    temporary.replace(STATE)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-package',action='store_true')
    parser.add_argument('--preflight-only',action='store_true')
    parser.add_argument('--resume',action='store_true')
    args=parser.parse_args()
    manifest=verify_package()
    if args.verify_package:return
    preflight(manifest,args.resume)
    if args.preflight_only:
        print('PRECHECK PASSED. No project files, containers or system services changed.')
        return
    STATE.parent.mkdir(parents=True,exist_ok=True)
    lock=open(STATE.parent/'install.lock','w')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    LOGDIR.mkdir(parents=True,exist_ok=True)
    logpath=LOGDIR/(datetime.now().strftime('%Y%m%d-%H%M%S')+'.log')
    # tee runs locally; every subprocess inherits this output, never prints generated credentials.
    tee=subprocess.Popen(['tee','-a',str(logpath)],stdin=subprocess.PIPE)
    os.dup2(tee.stdin.fileno(),1);os.dup2(tee.stdin.fileno(),2)
    os.umask(0o077)
    state=load(STATE) if STATE.exists() else {'release_id':manifest['release_id'],'completed':[]}
    state['status']='installing';save_state(state)
    steps=[('docker',install_docker),('payload',install_payload),('credentials',credentials),('images',lambda:images(manifest)),('systemd',setup_units),('start',start),('acceptance',lambda:run('python3',PACKAGE/'scripts/verify_runtime.py'))]
    try:
        for name,action in steps:
            if name in state['completed']:
                print('SKIP completed stage: '+name,flush=True);continue
            print('START stage: '+name,flush=True)
            state['current']=name;save_state(state)
            action()
            state['completed'].append(name);save_state(state)
        state['status']='installed';save_state(state)
        print('INSTALLATION PASSED. UI: http://<box-ip>:17780/ API: http://<box-ip>:17700/v1')
        print('Initial admin password: sudo cat /home/cix/gateway-admin/INITIAL_ADMIN_PASSWORD')
        print('API key is in /home/cix/houmo-docker-deployment/.env; do not share that file.')
    except Exception:
        state['status']='failed';save_state(state)
        raise

if __name__=='__main__':
    try: main()
    except Exception as exc:
        print('ERROR: '+str(exc),file=sys.stderr,flush=True)
        sys.exit(1)
