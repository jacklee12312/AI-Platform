"""Publisher-only Ed25519 signing. Private keys stay outside all delivery folders."""
import argparse,hashlib,json,os,subprocess
from pathlib import Path

def main():
    p=argparse.ArgumentParser();p.add_argument('--bundle',type=Path,default=Path(__file__).resolve().parents[1]);p.add_argument('--key',type=Path);p.add_argument('--generate-development-key',action='store_true');a=p.parse_args()
    bundle=a.bundle.resolve();key=a.key or Path.home()/'.local/share/ai-platform-release-keys/development-ed25519.pem'
    key=key.expanduser().absolute()
    if key.is_relative_to(bundle):raise SystemExit('私钥不能存放在安装器交付目录')
    if not key.exists():
        if not a.generate_development_key:raise SystemExit('签名私钥不存在；请指定正式发布私钥或显式创建开发密钥')
        key.parent.mkdir(parents=True,exist_ok=True,mode=0o700);key.parent.chmod(0o700)
        previous=os.umask(0o077)
        try:subprocess.run(['openssl','genpkey','-algorithm','ED25519','-out',str(key)],check=True)
        finally:os.umask(previous)
        key.chmod(0o600)
    public=bundle/'catalog/release-public.pem';catalog=bundle/'catalog/release-catalog.json'
    subprocess.run(['openssl','pkey','-in',str(key),'-pubout','-out',str(public)],check=True)
    subprocess.run(['openssl','pkeyutl','-sign','-inkey',str(key),'-rawin','-in',str(catalog),'-out',str(bundle/'catalog/release-catalog.sig')],check=True)
    release=json.loads((bundle/'release.json').read_text(encoding='utf-8'))
    release['catalog_sha256']=hashlib.sha256(catalog.read_bytes()).hexdigest();release['catalog_public_key_sha256']=hashlib.sha256(public.read_bytes()).hexdigest()
    (bundle/'release.json').write_text(json.dumps(release,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'signed':str(catalog),'private_key_location':str(key),'private_key_in_delivery':False},ensure_ascii=False))

if __name__=='__main__':main()
