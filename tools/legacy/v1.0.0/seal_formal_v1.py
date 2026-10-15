"""Regenerate complete checksums and sign the prepared v1 distribution."""
from pathlib import Path
from datetime import datetime,timezone
import hashlib,json,subprocess,tarfile
R=Path(r'F:\duoliuer\AI-Platform-1.0.0');B=R/'AI-Platform-Installer-1.0.0';S=R/'support-files/files'
RID='ai-platform-installer-1.0.0-20260929';MID='qwen3.6-35b-a3b'
def read(p):return json.loads(p.read_text('utf-8'))
def write(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

cat=read(B/'catalog/release-catalog.json');rows={r['path']:r for r in cat['resources']}
modelrows=[r for r in rows.values() if r['category']=='models']
manifest=read(B/'catalog/platform-manifest.json')
base=read(Path(r'F:\duoliuer\AI-Platform-Qwen38-MM-20260928\AI-Platform-Installer-0.2.3\catalog\platform-manifest.json'))
with tarfile.open(S/'runtime/qwen36-vllm-runtime.tar.gz','r:gz') as tar:
    expanded=sum(m.size for m in tar if m.isfile())
manifest.update(release_id=RID,models=cat['models'],all_model_file_bytes=sum(r['size'] for r in modelrows),
    installed_payload_bytes=base['installed_payload_bytes']-base['all_model_file_bytes']+sum(r['size'] for r in modelrows)+expanded+24*1024**2,
    built_at=datetime.now(timezone.utc).isoformat(),source=base['source']+'; installer 1.0.0: hardware-routed Harbor GGUF and Qwen3.6 dual runtime')
write(S/'manifests/manifest.json',manifest);write(B/'catalog/platform-manifest.json',manifest)
for p in sorted(S.rglob('*')):
    if not p.is_file() or '__pycache__' in p.parts:continue
    rel=p.relative_to(S).as_posix()
    if rel=='manifests/SHA256SUMS':continue
    prior=rows.get(rel)
    category=prior['category'] if prior else ('runtime' if rel.startswith('runtime/') else 'platform')
    rows[rel]={'path':rel,'size':p.stat().st_size,'sha256':sha(p),'category':category,'models':prior['models'] if prior else []}
missing=[r['path'] for r in rows.values() if r['category']!='models' and not (S/r['path']).is_file()]
if missing:raise RuntimeError('Missing distribution resources '+str(missing))
sums=S/'manifests/SHA256SUMS';sums.write_text(''.join(r['sha256']+'  '+r['path']+'\n' for r in sorted(rows.values(),key=lambda r:r['path']) if r['path']!='manifests/SHA256SUMS'),encoding='utf-8',newline='\n')
rows['manifests/SHA256SUMS'].update(size=sums.stat().st_size,sha256=sha(sums))
cat.update(release_id=RID,installer_version='1.0.0',created_at=datetime.now(timezone.utc).isoformat(),resources=sorted(rows.values(),key=lambda r:r['path']),platform_manifest_sha256=sha(B/'catalog/platform-manifest.json'))
for comp in cat['components']:
    if comp['kind']!='model':comp['files']=[r['path'] for r in cat['resources'] if r['category']==comp['kind']]
write(B/'catalog/release-catalog.json',cat)
release=read(B/'release.json');release.update(release_id=RID,installer_version='1.0.0',models=27,status='release-build',signature='Ed25519; dedicated installer 1.x release key',resources_checksums_sha256=sha(sums),resources_manifest_sha256=sha(S/'manifests/manifest.json'))
release['validation']={'fresh_install':'not_run','reboot':'not_run','harbor_new_model_uploads':'pending-publisher-upload','hardware_selection':'automated-tests-required','qwen36_container':'see validation report','source_note':'GGUF SHA-256 is pinned; uploading the expected files to v1 does not require rebuilding'}
write(B/'release.json',release)
key=Path.home()/'.local/share/ai-platform-release-keys/installer-v1-ed25519.pem'
if not key.exists():
    key.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run(['openssl','genpkey','-algorithm','ED25519','-out',str(key)],check=True)
subprocess.run(['python',str(B/'tools/sign_release.py'),'--bundle',str(B),'--key',str(key)],check=True)
subprocess.run(['python',str(B/'tools/seal_installer.py'),'--bundle',str(B)],check=True)
print(json.dumps({'resource_count':len(rows),'runtime_expanded_bytes':expanded,'support_bytes':sum(r['size'] for r in rows.values() if r['category']!='models')}))
