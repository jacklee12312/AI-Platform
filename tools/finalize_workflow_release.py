"""Prepare explicit 1.1 resource identity and sign with the existing trusted key.

Run after all prerequisite archives and dependency packages have been prepared.
Historical deliveries and tags are never modified.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]
BUNDLE=ROOT/'installer-bundle'
FILES=ROOT/'support-files/files'
RELEASE_ID='ai-platform-installer-1.1.0-20261015'


def read(path):return json.loads(path.read_text('utf-8'))
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--key',type=Path,required=True);args=parser.parse_args()
    key=args.key.resolve()
    if key.is_relative_to(ROOT.resolve()):raise SystemExit('Signing key must be outside the project')
    openssl=shutil.which('openssl')
    if not openssl and os.name=='nt':
        candidate=Path(r'C:\Program Files\Git\usr\bin\openssl.exe')
        if candidate.is_file():openssl=str(candidate)
    if not openssl:raise SystemExit('OpenSSL is required')
    public=subprocess.check_output([openssl,'pkey','-in',str(key),'-pubout'])
    if public.replace(b'\r\n',b'\n')!=(BUNDLE/'catalog/release-public.pem').read_bytes().replace(b'\r\n',b'\n'):
        raise SystemExit('The signing key does not match the original trusted public key')
    workflow=read(BUNDLE/'catalog/workflow.json')
    if len(workflow['images'])!=6 or not all(x.get('archive') for x in workflow['images']) or not workflow['host_packages']:
        raise SystemExit('Complete offline prerequisite assets before finalizing')
    workflow['component_disk_budget_bytes']=20*1024**3
    write(BUNDLE/'catalog/workflow.json',workflow)
    changed=[]
    for relative in ['payload/home/cix/gateway-admin/agent/model_catalog.json','variants/single/gateway-admin/agent/model_catalog.json']:
        path=FILES/relative;data=read(path)
        model=next(x for x in data['models'] if x['id']=='qwen3.8-27b')
        projector=next(x for x in model['files'] if '/mmproj-' in x['target'])
        projector['oci_source']['repository']='aistore/qwen3-8-27b-mmproj'
        write(path,data);changed.append(relative)
    manifest=read(FILES/'manifests/manifest.json');manifest['release_id']=RELEASE_ID
    marker='; installer 1.1: registered probe, authenticated middleware, shared Qwen3.8 projector'
    if marker not in manifest['source']:manifest['source']+=marker
    write(FILES/'manifests/manifest.json',manifest);changed.append('manifests/manifest.json')
    shutil.copyfile(FILES/'manifests/manifest.json',BUNDLE/'catalog/platform-manifest.json')
    sums=FILES/'manifests/SHA256SUMS'
    rows=[line.split('  ',1) for line in sums.read_text('utf-8').splitlines()]
    sums.write_text(''.join((sha(FILES/name) if name in changed else digest)+'  '+name+'\n' for digest,name in rows),encoding='utf-8',newline='\n')
    changed.append('manifests/SHA256SUMS')
    catalog=read(BUNDLE/'catalog/release-catalog.json')
    catalog.update(release_id=RELEASE_ID,installer_version='1.1.0',workflow_sha256=sha(BUNDLE/'catalog/workflow.json'),
                   platform_manifest_sha256=sha(BUNDLE/'catalog/platform-manifest.json'),created_at='2026-10-15T00:00:00+00:00')
    for row in catalog['resources']:
        if row['path'] in changed:
            path=FILES/row['path'];row.update(size=path.stat().st_size,sha256=sha(path))
    write(BUNDLE/'catalog/release-catalog.json',catalog)
    subprocess.run([openssl,'pkeyutl','-sign','-inkey',str(key),'-rawin','-in',str(BUNDLE/'catalog/release-catalog.json'),'-out',str(BUNDLE/'catalog/release-catalog.sig')],check=True)
    release=read(BUNDLE/'release.json')
    release.update(installer_version='1.1.0',release_id=RELEASE_ID,resource_release_id=RELEASE_ID,
                   images=18,install_mode='probe-components-selected-models',catalog_sha256=sha(BUNDLE/'catalog/release-catalog.json'),
                   resources_manifest_sha256=sha(FILES/'manifests/manifest.json'),resources_checksums_sha256=sha(sums))
    write(BUNDLE/'release.json',release)
    spec=read(ROOT/'release-config.json');spec.update(installer_version='1.1.0',resource_release_id=RELEASE_ID)
    write(ROOT/'release-config.json',spec)
    external=read(ROOT/'external-assets.json');by_path={x['path']:x for x in external['files']}
    assets=[workflow['probe']['archive'],*workflow['host_packages'],*(x['archive'] for x in workflow['images'])]
    for row in assets:
        path=FILES/row['path']
        if sha(path)!=row['sha256'] or path.stat().st_size!=row['size']:raise RuntimeError('Changed workflow asset: '+row['path'])
        rel=path.relative_to(ROOT).as_posix();by_path[rel]={'path':rel,'bytes':row['size'],'sha256':row['sha256']}
    external['files']=[by_path[k] for k in sorted(by_path)];write(ROOT/'external-assets.json',external)
    print(json.dumps({'resource_release_id':RELEASE_ID,'installer_version':'1.1.0','workflow_assets':len(assets),
                      'trusted_public_key_unchanged':True},indent=2))


if __name__=='__main__':main()
