"""Separate signed ModelDeck compatibility resource; immutable platform bytes remain intact."""
import json
from pathlib import Path
import subprocess

RELEASE_ID='modeldeck-component-runtime-1.2.0-r8-20261016'
PATHS=['gateway-admin/host_ops/core/components.py','gateway-admin/host_ops/runtime.py']


def prepare(root,key):
    import release
    bundle=root/'installer-bundle';destination=bundle/'component-runtime';key=Path(key).resolve()
    if key.is_relative_to(root.resolve()) or destination.exists():raise RuntimeError('Keep keys outside the repository; never overwrite a resource release')
    public=subprocess.check_output(['openssl','pkey','-in',str(key),'-pubout'])
    if public.replace(b'\r\n',b'\n')!=(bundle/'catalog/release-public.pem').read_bytes().replace(b'\r\n',b'\n'):raise RuntimeError('Wrong trusted key')
    rows=[]
    for relative in PATHS:
        base=root/'build/resources-1.2.0/payload/home/cix'/relative
        text=base.read_text('utf-8')
        if relative.endswith('core/components.py'):
            text=text.replace("if info.get('Image') != self.images[name]['config_id']:","if info.get('Image') not in {self.images[name]['config_id'], self.images[name]['manifest_digest']}:")
        else:
            old="result = run(['docker', 'image', 'inspect', row['config_id']], check=False)"
            text=text.replace(old,old+"\n        if result.returncode:\n            result = run(['docker', 'image', 'inspect', row['pinned']], check=False)",1)
            text=text.replace("result = run(['docker', 'image', 'inspect', row['config_id']])",
              "result = run(['docker', 'image', 'inspect', row['pinned']], check=False)\n            if result.returncode: result = run(['docker', 'image', 'inspect', row['config_id']])")
            text=text.replace("info['Id'] != row['config_id']", "info['Id'] not in {row['config_id'], row['manifest_digest']}")
            text=text.replace("            raise ValueError('组件镜像与固定 ARM64 摘要不符：'+name)",
              "            raise ValueError('组件镜像与固定 ARM64 摘要不符：'+name)\n        self.spec['services'][name]['image'] = info['Id']")
        if text==base.read_text('utf-8'):raise RuntimeError('Compatibility transformation did not apply')
        compile(text,relative,'exec')
        path=destination/'files'/relative;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(text,encoding='utf-8',newline='\n')
        rows.append({'path':relative,'bytes':path.stat().st_size,'sha256':release.digest(path),'base_sha256':release.digest(base)})
    release.write(destination/'manifest.json',{'schema':1,'release_id':RELEASE_ID,'files':rows})
    subprocess.run(['openssl','pkeyutl','-sign','-inkey',str(key),'-rawin','-in',str(destination/'manifest.json'),'-out',str(destination/'manifest.sig')],check=True)
    for path in [root/'release-config.json',bundle/'release.json']:
        value=release.read(path);value['component_runtime_release_id']=RELEASE_ID;release.write(path,value)
    print(json.dumps({'component_runtime_release_id':RELEASE_ID,'signed_files':len(rows),'original_platform_resources_unchanged':True}))
