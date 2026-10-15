"""Load every new offline image into an isolated Docker daemon, then remove it.

Does not start containers or modify the host Docker installation, network or settings.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
SHA='803d433f226db4776e1768fd319fc6c6e4935a456acf84fcc0080818b854bc8f'


def main():
    if os.name!='posix' or os.geteuid()!=0:raise SystemExit('Run in WSL as root')
    parser=argparse.ArgumentParser();parser.add_argument('--platform',action='store_true');args=parser.parse_args()
    archive=ROOT/'build/minio-mirror-check/docker-29.7.2.tgz'
    if not archive.exists():
        archive.parent.mkdir(parents=True,exist_ok=True)
        part=archive.with_name(archive.name+'.part')
        with urllib.request.urlopen('https://download.docker.com/linux/static/stable/x86_64/docker-29.7.2.tgz',timeout=120) as source,part.open('wb') as output:
            shutil.copyfileobj(source,output)
        with part.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=SHA:raise RuntimeError('Downloaded Docker runtime digest differs')
        part.replace(archive)
    with archive.open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=SHA:raise RuntimeError('Docker runtime digest differs')
    lock=json.loads((ROOT/'installer-bundle/catalog/workflow.json').read_text('utf-8'))
    rows=lock['images'];report_name='component-archives.json'
    if args.platform:
        manifest=json.loads((ROOT/'installer-bundle/catalog/platform-manifest.json').read_text('utf-8'))
        spec=json.loads((ROOT/'release-config.json').read_text('utf-8'))
        resources=ROOT/spec['resources_root']
        row=next(r for r in json.loads((ROOT/'installer-bundle/catalog/release-catalog.json').read_text('utf-8'))['resources'] if r['path']=='images/all-arm64-images.tar.gz')
        platform_archive=resources/row['path']
        with platform_archive.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=row['sha256']:raise RuntimeError('Platform archive digest differs')
        rows=[dict(image,id=image['tag'],archive={'sha256':row['sha256']}) for image in manifest['images']]
        report_name='platform-images.json'
    git=['git','-c','safe.directory='+str(ROOT),'-C',str(ROOT)]
    report={'source_commit':subprocess.check_output([*git,'rev-parse','HEAD'],text=True).strip(),
            'source_dirty':bool(subprocess.check_output([*git,'status','--porcelain'],text=True).strip()),
            'environment':'isolated WSL Docker 29.7.2, overlay2, no network bridge, no containers started','checks':[]}
    stage=Path(tempfile.mkdtemp(prefix='ai-platform-component-load-',dir='/var/tmp'))
    daemon=None;log=None
    try:
        with tarfile.open(archive) as tar:tar.extractall(stage,filter='data')
        binary=stage/'docker';environment=dict(os.environ,PATH=str(binary)+os.pathsep+os.environ.get('PATH',''))
        (stage/'client').mkdir();(stage/'client/config.json').write_text('{}\n');(stage/'daemon.json').write_text('{}\n')
        cli=[str(binary/'docker'),'--host','unix://'+str(stage/'docker.sock'),'--config',str(stage/'client')]
        folder=ROOT/'build/validation';folder.mkdir(parents=True,exist_ok=True)
        log=(folder/'component-archives-daemon.log').open('wb')
        daemon=subprocess.Popen([str(binary/'dockerd'),'--config-file',str(stage/'daemon.json'),
                                 '--host','unix://'+str(stage/'docker.sock'),'--data-root',str(stage/'data'),
                                 '--exec-root',str(stage/'exec'),'--pidfile',str(stage/'docker.pid'),
                                 '--storage-driver=overlay2','--bridge=none','--iptables=false','--ip6tables=false',
                                 '--ip-forward=false','--ip-masq=false','--userland-proxy=false'],
                                env=environment,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        def run(args,timeout=120):
            result=subprocess.run(cli+[str(x) for x in args],env=environment,capture_output=True,text=True,timeout=timeout)
            if result.returncode:raise RuntimeError('Docker archive validation failed: '+result.stderr[-700:])
            return result.stdout
        limit=time.monotonic()+90
        while True:
            try:run(['info'],timeout=5);break
            except (RuntimeError,subprocess.TimeoutExpired):
                if daemon.poll() is not None or time.monotonic()>limit:raise
                time.sleep(1)
        if args.platform:run(['load','-i',platform_archive],timeout=1200)
        for row in rows:
            print('Loading actual offline image: '+row['id'],flush=True)
            if not args.platform:
                source=ROOT/'support-files/files'/row['archive']['path']
                with source.open('rb') as stream:
                    if hashlib.file_digest(stream,'sha256').hexdigest()!=row['archive']['sha256']:
                        raise RuntimeError('Offline archive digest differs: '+row['id'])
                run(['load','-i',source],timeout=900)
            image=json.loads(run(['image','inspect',row['config_id']]))[0]
            if image['Id']!=row['config_id'] or image['Architecture']!='arm64' or image['Os']!='linux' or image['RootFS']['Layers']!=row['rootfs_diff_ids']:
                raise RuntimeError('Offline image config or filesystem differs: '+row['id'])
            report['checks'].append({'component':row['id'],'archive_sha256':row['archive']['sha256'],
                                     'image_config_id':image['Id'],'architecture':image['Architecture'],'passed':True})
            run(['image','rm','--force',row['config_id']],timeout=300)
            print(row['id']+' offline import and complete layer identity verified.',flush=True)
        report['passed']=len(report['checks'])==len(rows) and all(x['passed'] for x in report['checks'])
    finally:
        if daemon is not None and daemon.poll() is None:
            os.killpg(daemon.pid,signal.SIGTERM)
            try:daemon.wait(timeout=30)
            except subprocess.TimeoutExpired:os.killpg(daemon.pid,signal.SIGKILL);daemon.wait(timeout=10)
        if log is not None:log.close()
        report['temporary_daemon_stopped']=daemon is None or daemon.poll() is not None
        report['source_dirty']=report['source_dirty'] or bool(subprocess.check_output([*git,'status','--porcelain'],text=True).strip()) or subprocess.check_output([*git,'rev-parse','HEAD'],text=True).strip()!=report['source_commit']
        if stage.resolve().parent!=Path('/var/tmp') or not stage.name.startswith('ai-platform-component-load-'):
            raise RuntimeError('Unexpected cleanup path')
        shutil.rmtree(stage)
        (ROOT/'build/validation'/report_name).write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
