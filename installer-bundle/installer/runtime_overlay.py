"""Authenticate a separately versioned compatibility resource before payload planning."""
from pathlib import Path
import subprocess
from common import read_json,sha256,no_symlink_parents

RELEASE_ID='modeldeck-component-runtime-1.2.0-r9-20261016'
DIRECTORY='component-runtime-r9'
MEMBERS={'gateway-admin/host_ops/core/components.py','gateway-admin/host_ops/runtime.py'}


def load(bundle):
    bundle=Path(bundle);base=bundle/DIRECTORY;key=bundle/'catalog/release-public.pem'
    metadata=read_json(bundle/'release.json')
    if metadata.get('component_runtime_release_id')!=RELEASE_ID:raise RuntimeError('组件运行资源发行 ID 不一致')
    for path in [base/'manifest.json',base/'manifest.sig',key]:
        no_symlink_parents(path,bundle)
        if path.is_symlink() or not path.is_file():raise RuntimeError('组件运行资源缺失')
    if sha256(key)!=metadata['catalog_public_key_sha256']:raise RuntimeError('组件运行资源公钥不受信任')
    check=subprocess.run(['openssl','pkeyutl','-verify','-pubin','-inkey',str(key),'-rawin','-in',str(base/'manifest.json'),'-sigfile',str(base/'manifest.sig')],capture_output=True,timeout=20)
    if check.returncode:raise RuntimeError('组件运行资源签名无效')
    manifest=read_json(base/'manifest.json')
    if manifest.get('schema')!=1 or manifest.get('release_id')!=RELEASE_ID or len(manifest['files'])!=2 or {r['path'] for r in manifest['files']}!=MEMBERS:
        raise RuntimeError('组件运行资源文件清单错误')
    from artifacts import load_catalog
    originals={r['path']:r['sha256'] for r in load_catalog(bundle)['resources']}
    for row in manifest['files']:
        path=base/'files'/row['path'];no_symlink_parents(path,base)
        if path.is_symlink() or not path.is_file() or path.stat().st_size!=row['bytes'] or sha256(path)!=row['sha256']:
            raise RuntimeError('组件运行资源内容校验失败')
        if originals['payload/home/cix/'+row['path']]!=row['base_sha256']:raise RuntimeError('组件运行资源基线不一致')
    return manifest


def effective_source(bundle,relative,original):
    if relative not in MEMBERS or not read_json(Path(bundle)/'release.json').get('component_runtime_release_id'):return original
    row=next(r for r in load(bundle)['files'] if r['path']==relative)
    if sha256(original)!=row['base_sha256']:raise RuntimeError('组件兼容资源基础文件不同')
    return Path(bundle)/DIRECTORY/'files'/relative
