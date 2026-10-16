"""Prepare a separately signed, offline NPU runtime resource release."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

RELEASE_ID = 'cix-noe-runtime-3.1.2-arm64-20261016'
ARCHIVE = 'cix-noe-3.1.2-arm64.tar.gz'
MEMBERS = {'lib/libnoe.so','lib/libnoe.so.3','lib/libaipu_driver.so',
           'libnoe/__init__.py','libnoe/libnoe.cpython-311-aarch64-linux-gnu.so'}


def prepare(root, source, key):
    import release
    root,source,key=Path(root),Path(source),Path(key).resolve()
    if key.is_relative_to(root.resolve()):raise RuntimeError('Signing key must remain outside the repository')
    bundle=root/'installer-bundle';destination=bundle/'npu-runtime'
    if destination.exists():raise RuntimeError('NPU resource release already exists; never overwrite signed resources')
    public=subprocess.check_output(['openssl','pkey','-in',str(key),'-pubout'])
    if public.replace(b'\r\n',b'\n')!=(bundle/'catalog/release-public.pem').read_bytes().replace(b'\r\n',b'\n'):
        raise RuntimeError('Signing key does not match the existing trusted key')
    proof=release.read(source/'provenance.json');archive=source/ARCHIVE
    if archive.stat().st_size!=proof['bytes'] or release.digest(archive)!=proof['sha256']:
        raise RuntimeError('Vendor runtime archive checksum mismatch')
    expected={row['path']:row for row in proof['files']}
    if set(expected)!=MEMBERS:raise RuntimeError('Unexpected NPU runtime member set')
    with tarfile.open(archive,'r:gz') as tar:
        seen=set()
        for item in tar:
            if item.name in seen or item.name not in expected or not item.isfile():raise RuntimeError('Unsafe NPU runtime archive')
            seen.add(item.name);row=expected[item.name]
            if item.size!=row['size'] or hashlib.file_digest(tar.extractfile(item),'sha256').hexdigest()!=row['sha256']:
                raise RuntimeError('NPU runtime member checksum mismatch')
        if seen!=MEMBERS:raise RuntimeError('Incomplete NPU runtime archive')
    manifest={'schema':1,'release_id':RELEASE_ID,'architecture':'arm64','container_python':'3.11',
              'selection_policy':'probe-image-then-offline-bundle-with-real-cix-inference',
              'installation_scope':'platform-private-userspace; system kernel and drivers unchanged',
              'vendor_packages':proof['vendor_packages'],'vendor_inputs':proof['inputs'],
              'archive':{'path':ARCHIVE,'size':proof['bytes'],'sha256':proof['sha256']},
              'files':proof['files'],
              'validated_hardware':{'soc':'CIX P1','aipu_kmd':'6.0.1','kernel':'6.6.89-cix-build-generic'},
              'source_verification':'exported only after dpkg --verify cix-noe-umd cix-npu-onnxruntime passed'}
    destination.mkdir()
    shutil.copyfile(archive,destination/ARCHIVE)
    release.write(destination/'manifest.json',manifest)
    subprocess.run(['openssl','pkeyutl','-sign','-inkey',str(key),'-rawin','-in',str(destination/'manifest.json'),'-out',str(destination/'manifest.sig')],check=True)
    config=release.read(root/'release-config.json');config['npu_runtime_release_id']=RELEASE_ID;release.write(root/'release-config.json',config)
    metadata=release.read(bundle/'release.json');metadata['npu_runtime_release_id']=RELEASE_ID;release.write(bundle/'release.json',metadata)
    external=release.read(root/'external-assets.json')
    path=(destination/ARCHIVE).relative_to(root).as_posix()
    external['files'].append({'path':path,'bytes':proof['bytes'],'sha256':proof['sha256']})
    external['files'].sort(key=lambda row:row['path']);release.write(root/'external-assets.json',external)
    print(json.dumps({'npu_runtime_release_id':RELEASE_ID,'archive_bytes':proof['bytes'],'trusted_public_key_preserved':True}))
