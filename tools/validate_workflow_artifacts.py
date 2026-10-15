"""Exercise real ARM64 MinIO and Compose under QEMU; no system services installed.

The probe is smoke-tested in a private chroot against a deliberately unavailable
loopback registration endpoint. This never registers a development PC as a device.
"""
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'installer-bundle/installer'))
from common import read_json,sha256
from components import compose_spec,escape_compose,http,minio_authorization
from probe import make_config


def extract_binary(archive,target,relative):
    with tarfile.open(archive) as outer:
        manifest=json.load(outer.extractfile('manifest.json'))
        for layer in reversed(manifest[0]['Layers']):
            with tarfile.open(fileobj=outer.extractfile(layer),mode='r|*') as inner:
                for member in inner:
                    if member.name.lstrip('./')==relative and member.isfile():
                        with inner.extractfile(member) as source,target.open('wb') as destination:
                            shutil.copyfileobj(source,destination)
                        target.chmod(0o755);return
    raise RuntimeError('Binary not found: '+relative)


def main():
    if os.name!='posix' or os.geteuid()!=0:raise SystemExit('Run in WSL as root; all writes remain in a private temporary directory')
    qemu=shutil.which('qemu-aarch64')
    if not qemu:raise SystemExit('QEMU ARM64 user emulator is required')
    lock=read_json(ROOT/'installer-bundle/catalog/workflow.json')
    git=['git','-c','safe.directory='+str(ROOT),'-C',str(ROOT)]
    report={'source_commit':subprocess.check_output([*git,'rev-parse','HEAD'],text=True).strip(),
            'source_dirty':bool(subprocess.check_output([*git,'status','--porcelain'],text=True).strip()),
            'target_hardware':'not_used','emulation':'QEMU ARM64 user mode','real_registration':'not_run',
            'mqtt_platform_connection':'not_run','milvus_integration':'not_run','checks':{}}
    with tempfile.TemporaryDirectory(prefix='ai-platform-11-artifacts-') as folder:
        work=Path(folder)
        minio=next(x for x in lock['images'] if x['id']=='minio')
        binary=work/'minio'
        extract_binary(ROOT/'support-files/files'/minio['archive']['path'],binary,'usr/bin/minio')
        version=subprocess.check_output([qemu,str(binary),'--version'],text=True,timeout=30)
        if 'RELEASE.2025-09-07T16-13-09Z' not in version or '07c3a429bfed433e49018cb0f78a52145d4bedeb' not in version:
            raise RuntimeError('Actual ARM64 MinIO version differs')
        report['checks']['minio_arm64_version']=True;report['minio_version']=version.strip()
        print('ARM64 MinIO version and upstream commit verified.',flush=True)
        with socket.socket() as available:available.bind(('127.0.0.1',9000))
        username='installer-check';password='fixture-$-secret-'+secrets.token_hex(10)
        environment=dict(os.environ,MINIO_ROOT_USER=username,MINIO_ROOT_PASSWORD=password,MINIO_BROWSER='off')
        data=work/'minio-data';data.mkdir()
        with (work/'minio.log').open('wb') as log:
            server=subprocess.Popen([qemu,str(binary),'server',str(data),'--address','127.0.0.1:9000'],
                                    env=environment,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            try:
                limit=time.monotonic()+60
                while True:
                    try:
                        status,body=http('http://127.0.0.1:9000/',headers=minio_authorization(username,password))
                        if status==200 and b'ListAllMyBucketsResult' in body:break
                    except (OSError,ValueError):pass
                    if server.poll() is not None or time.monotonic()>limit:
                        raise RuntimeError('ARM64 MinIO S3 authenticated request failed')
                    time.sleep(1)
                report['checks']['minio_arm64_s3_authentication']=True
                try:
                    http('http://127.0.0.1:9000/',headers=minio_authorization(username,'incorrect-password'))
                    raise RuntimeError('MinIO accepted incorrect S3 credentials')
                except urllib.error.HTTPError as error:
                    if error.code!=403:raise
                report['checks']['minio_rejects_wrong_password']=True
                print('ARM64 MinIO started; signed S3 request succeeds and wrong password is rejected.',flush=True)
            finally:
                if server.poll() is None:os.killpg(server.pid,signal.SIGTERM)
                try:server.wait(timeout=15)
                except subprocess.TimeoutExpired:os.killpg(server.pid,signal.SIGKILL);server.wait(timeout=5)
        report['temporary_minio_stopped']=True
        compose_root=work/'compose';compose_root.mkdir()
        package=next((ROOT/'support-files/files/docker/arm64-bookworm').glob('docker-compose-plugin_*.deb'))
        subprocess.run(['dpkg-deb','-x',str(package),str(compose_root)],check=True)
        plugin=next(compose_root.rglob('docker-compose'))
        credentials={key:'fixture-$VAR-#-password-123' for key in ['mysql_password','redis_password','paradedb_password','neo4j_password','minio_password']}
        credentials['minio_user']='fixture-minio'
        spec=compose_spec(lock['images'],credentials)
        configuration=work/'compose.json';configuration.write_text(json.dumps(escape_compose(spec)))
        result=subprocess.run([qemu,str(plugin),'-f',str(configuration),'config','--format','json'],text=True,capture_output=True,timeout=90)
        if result.returncode:raise RuntimeError('ARM64 Compose rejected the generated configuration: '+result.stderr[:500])
        # Compose 5.5 config.go:180-182 re-escapes every dollar for round-trip output.
        # Undo that rendering step before comparing the resolved service values.
        normalized=json.loads(result.stdout.replace('$$','$'))
        for name,expected in spec['services'].items():
            if normalized['services'][name].get('environment',{})!=expected['environment']:
                raise RuntimeError('Compose changed environment credentials for '+name)
        if normalized['services']['redis']['command']!=spec['services']['redis']['command']:
            raise RuntimeError('Compose changed Redis authentication arguments')
        report['checks']['real_arm64_compose_config']=True
        report['checks']['compose_preserves_special_passwords']=True
        print('Real ARM64 Compose accepts all services and preserves dollar signs and special passwords.',flush=True)
        # Smoke-test the exact C++ binary with the complete Debian runtime closure.
        chroot=work/'probe-root';chroot.mkdir()
        for row in lock['host_packages']:
            if row.get('package'):
                subprocess.run(['dpkg-deb','-x',str(ROOT/'support-files/files'/row['path']),str(chroot)],check=True,stdout=subprocess.DEVNULL)
        with tarfile.open(ROOT/'support-files/files'/lock['probe']['archive']['path']) as tar:
            for src,dst in [('build/data-probe','usr/local/bin/data-probe'),('config/certs/ca.pem','etc/data-probe/certs/ca.pem'),('config/certs/sm2_pub.pem','etc/data-probe/certs/sm2_pub.pem')]:
                target=chroot/dst;target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(tar.extractfile('data-probe-offline/'+src).read());target.chmod(0o755 if src.startswith('build/') else 0o600)
        shutil.copyfile(qemu,chroot/'qemu');(chroot/'qemu').chmod(0o755)
        for directory in ['var/log/data-probe','proc','sys','tmp','dev']:(chroot/directory).mkdir(parents=True,exist_ok=True)
        (chroot/'etc/machine-id').write_text('1234567890abcdef1234567890abcdef\n')
        probe_settings={'platform_url':'http://127.0.0.1:1',**{key:'fixture' for key in ['owner','project','city','location','deploymentPurpose','accessPolicy','hardwareModel']},'bindActivateToken':'fixture-do-not-register'}
        config=make_config(probe_settings);config['register']['max_retries']=1
        (chroot/'etc/data-probe/probe.yaml').write_text(json.dumps(config))
        with (work/'probe.log').open('wb') as log:
            process=subprocess.Popen(['chroot',str(chroot),'/qemu','/usr/local/bin/data-probe','/etc/data-probe/probe.yaml'],
                                     stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            try:
                try:process.wait(timeout=15)
                except subprocess.TimeoutExpired:pass
            finally:
                if process.poll() is None:os.killpg(process.pid,signal.SIGTERM)
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5)
        text=(work/'probe.log').read_text(errors='replace')
        (ROOT/'build/validation').mkdir(parents=True,exist_ok=True)
        (ROOT/'build/validation/probe-smoke.log').write_text(text)
        if any(x in text for x in ['error while loading shared libraries','Could not open','yaml-cpp: error','bad conversion','YAML::ParserException']):
            raise RuntimeError('ARM64 probe runtime dependency or configuration error; inspect private smoke log')
        if not any(x in text.lower() for x in ['register','registration']):
            raise RuntimeError('Probe did not reach its local fixture registration path')
        report['checks']['real_arm64_probe_loads']=True
        report['checks']['probe_accepts_json_yaml_config']=True
        report['checks']['probe_dependency_closure']=True
        report['temporary_probe_stopped']=True
        print('Real ARM64 probe loads its dependencies and reaches the isolated registration fixture.',flush=True)
    report['source_dirty']=report['source_dirty'] or bool(subprocess.check_output([*git,'status','--porcelain'],text=True).strip()) or subprocess.check_output([*git,'rev-parse','HEAD'],text=True).strip()!=report['source_commit']
    report['passed']=all(report['checks'].values())
    (ROOT/'build/validation/workflow-artifacts.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
