"""Verify and assemble the self-contained online installer distribution."""
from pathlib import Path
import hashlib,json,shutil,subprocess,sys,tarfile
R=Path(r'F:\duoliuer\AI-Platform-1.0.0');B=R/'AI-Platform-Installer-1.0.0'
D=R/'delivery/AI-Platform-1.0.0-linux-arm64'
sys.path.insert(0,str(B/'installer'))
from artifacts import load_catalog
from common import sha256
cat=load_catalog(B)
if D.exists():raise RuntimeError('Distribution already exists; refusing overwrite')
D.mkdir(parents=True)
for row in cat['resources']:
    if row['category']=='models':continue
    src=R/'support-files/files'/row['path']
    if src.stat().st_size!=row['size'] or sha256(src)!=row['sha256']:raise RuntimeError('Resource changed after signing: '+row['path'])
    dst=D/'support-files/files'/row['path'];dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dst)
subprocess.run(['python',str(B/'tools/pack_launcher_023.py'),'--bundle',str(B),'--output',str(D/'安装AI平台-1.0.0.run')],check=True)
for src,name in [('使用说明.md','README.md'),('模型上传清单.md','模型上传清单.md'),('验证报告.md','验证报告.md')]:shutil.copyfile(R/src,D/name)
shutil.copytree(R/'validation',D/'validation')
(D/'启动安装.sh').write_text('#!/bin/sh\nset -eu\ncd -- "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\nexec python3 "./安装AI平台-1.0.0.run" "$@"\n',encoding='utf-8',newline='\n')
rows=[]
for p in sorted(D.rglob('*')):
    if p.is_file():rows.append({'path':p.relative_to(D).as_posix(),'bytes':p.stat().st_size,'sha256':sha256(p)})
(D/'SHA256SUMS').write_text(''.join(r['sha256']+'  '+r['path']+'\n' for r in rows),encoding='utf-8',newline='\n')
archive=D.parent/'AI-Platform-1.0.0-linux-arm64.tar.gz'
if archive.exists():raise RuntimeError('Archive already exists')
with tarfile.open(archive,'w:gz',compresslevel=1) as tar:
    for p in sorted(D.rglob('*')):
        if p.is_file():
            info=tar.gettarinfo(str(p),arcname=D.name+'/'+p.relative_to(D).as_posix());info.uid=info.gid=0;info.uname=info.gname='root';info.mode=0o755 if p.suffix in ('.run','.sh') else 0o644
            with p.open('rb') as src:tar.addfile(info,src)
digest=sha256(archive);archive.with_suffix('.gz.sha256').write_text(digest+'  '+archive.name+'\n',encoding='utf-8')
summary={'directory':str(D),'archive':str(archive),'archive_bytes':archive.stat().st_size,'archive_sha256':digest,'distribution_file_count':len(rows)+1,'distribution_bytes':sum(r['bytes'] for r in rows),'model_weights_in_distribution':False}
(R/'delivery-result.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False),flush=True)
