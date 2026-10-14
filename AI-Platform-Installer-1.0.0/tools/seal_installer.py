"""Seal immutable installer files after building; source routing remains editable."""
import argparse,hashlib,json
from pathlib import Path

def main():
    p=argparse.ArgumentParser();p.add_argument('--bundle',type=Path,default=Path(__file__).resolve().parents[1]);a=p.parse_args();root=a.bundle.resolve();rows=[]
    for path in sorted(root.rglob('*')):
        rel=path.relative_to(root)
        if path.is_symlink():raise SystemExit('安装器不允许额外符号链接')
        if not path.is_file() or '__pycache__' in rel.parts or rel.as_posix() in ['sources.json','installer-manifest.json','SHA256SUMS']:continue
        h=hashlib.sha256()
        with path.open('rb') as f:
            for b in iter(lambda:f.read(4*1024**2),b''):h.update(b)
        rows.append({'path':rel.as_posix(),'bytes':path.stat().st_size,'sha256':h.hexdigest()})
    manifest=root/'installer-manifest.json';manifest.write_text(json.dumps({'format':1,'mutable_files':['sources.json'],'files':rows},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (root/'SHA256SUMS').write_text(''.join(r['sha256']+'  '+r['path']+'\n' for r in rows)+hashlib.sha256(manifest.read_bytes()).hexdigest()+'  installer-manifest.json\n',encoding='utf-8')
    print(json.dumps({'sealed_files':len(rows),'installer_bytes':sum(r['bytes'] for r in rows),'model_weights_in_installer':False}))

if __name__=='__main__':main()
