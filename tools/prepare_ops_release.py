"""Build explicit v1.2.0 resources from the immutable v1.1.2 baseline."""
from __future__ import annotations
import argparse
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT/'platform-overlays/1.2.0'
OUTPUT = ROOT/'build/resources-1.2.0'
BUNDLE = ROOT/'installer-bundle'
VERSION = '1.2.0'
RELEASE_ID = 'ai-platform-installer-1.2.0-20261015'


def read(p): return json.loads(Path(p).read_text('utf-8'))
def baseline(name): return json.loads(subprocess.check_output(['git', '-C', str(ROOT), 'show', 'v1.1.2:'+name]))
def sha(p):
    with Path(p).open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()
def write(p, value):
    p = Path(p); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8', newline='\n')
def put(src, dst):
    dst = Path(dst); dst.parent.mkdir(parents=True, exist_ok=True)
    # Never mutate a hard-linked baseline inode.
    if dst.exists(): dst.unlink()
    shutil.copyfile(src, dst)


def prepare():
    catalog = baseline('installer-bundle/catalog/release-catalog.json')
    workflow = baseline('installer-bundle/catalog/workflow.json')
    sources = baseline('installer-bundle/sources.json')
    source_root = ROOT/'build/resources-1.1.2'
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for p in source_root.rglob('*'):
        if not p.is_file() or '__pycache__' in p.parts or p.suffix=='.pyc': continue
        q = OUTPUT/p.relative_to(source_root)
        if q.exists(): continue
        q.parent.mkdir(parents=True, exist_ok=True)
        if p.stat().st_size > 1024*1024: os.link(p, q)
        else: shutil.copyfile(p, q)
    for p in (OVERLAY/'files').rglob('*'):
        if p.is_file(): put(p, OUTPUT/p.relative_to(OVERLAY/'files'))
    home = OUTPUT/'payload/home/cix'
    for base, count in [(home, 2), (OUTPUT/'variants/single', 1)]:
        path = base/'gateway-admin/agent/model_catalog.json'
        doc = read(source_root/path.relative_to(OUTPUT))
        doc.update(resource_release_id=RELEASE_ID, device_count=count)
        for model in doc['models']:
            for item in model['files']:
                target = 'payload/home/cix/'+item['target']
                matches = [s for s in sources['oci_artifacts'].values() if target in s['files'] and s.get('device_count', count)==count]
                if len(matches) == 1:
                    item.pop('url', None); item['oci_source'] = matches[0]
                elif not matches and model['id']=='sensevoice' and item['target'].endswith('/zh.wav'):
                    sample = home/'gateway-admin/agent/fixtures/sensevoice-zh.wav'
                    if not sample.is_file():
                        sample.parent.mkdir(parents=True, exist_ok=True)
                        with urlopen(item['url'], timeout=90) as response: sample.write_bytes(response.read(item['size']+1))
                    if sample.stat().st_size != item['size'] or sha(sample) != item['sha256']:
                        raise RuntimeError('SenseVoice test fixture differs from pinned release')
                    put(sample, home/item['target'])
                    item.pop('url', None); item['bundled_file'] = 'fixtures/sensevoice-zh.wav'
                else: raise RuntimeError('Missing or ambiguous Harbor source: '+target)
        write(path, doc)
    host = home/'gateway-admin/host_ops'
    write(host/'workflow.json', workflow)
    for row in workflow['host_packages']:
        put(source_root/row['path'], host/'resources'/row['path'])
    manifest={**read(source_root/'manifests/manifest.json'), 'release_id': RELEASE_ID}
    manifest['source'] += '; ModelDeck 1.2.0: unified Harbor supply, local imports and host operations'
    write(OUTPUT/'manifests/manifest.json', manifest)
    return catalog, workflow


def finalize(catalog, workflow, key):
    openssl = shutil.which('openssl') or r'C:\Program Files\Git\usr\bin\openssl.exe'
    public = subprocess.check_output([openssl, 'pkey', '-in', str(key), '-pubout'])
    if public.replace(b'\r\n', b'\n') != (BUNDLE/'catalog/release-public.pem').read_bytes().replace(b'\r\n', b'\n'):
        raise RuntimeError('Signing key does not match existing trusted release key')
    ui = OUTPUT/'runtime/ui-static.tar.gz'
    if ui.exists(): ui.unlink()
    dist = OVERLAY/'modeldeck-frontend/dist'
    if not (dist/'index.html').is_file(): raise RuntimeError('Build frontend before signing')
    with ui.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as zipped, tarfile.open(fileobj=zipped, mode='w|') as tar:
        for p in sorted(p for p in dist.rglob('*') if p.is_file()):
            data=p.read_bytes(); entry=tarfile.TarInfo(p.relative_to(dist).as_posix()); entry.size=len(data); entry.mode=0o644
            tar.addfile(entry, io.BytesIO(data))
    rows = {r['path']:r for r in catalog['resources']}
    # This small fixed test audio is an always-available platform fixture, so
    # fresh installation and later speech-model activation need no third-party download.
    fixture='payload/home/cix/cix_telecom_projects/sensevoice/model/test_wavs/zh.wav'
    rows[fixture].update(category='platform',models=[])
    write(BUNDLE/'catalog/upgrade-baseline.json', {'version':'1.1.2','resources':[
        r for r in catalog['resources'] if '/gateway-admin/' in r['path']]})
    for p in OUTPUT.rglob('*'):
        if not p.is_file() or '__pycache__' in p.parts or p.suffix=='.pyc' or p.relative_to(OUTPUT).as_posix().startswith('workflow/'): continue
        relative=p.relative_to(OUTPUT).as_posix()
        rows.setdefault(relative, {'path': relative, 'category': 'platform', 'models': []})
        if rows[relative]['category'] != 'models' and relative != 'manifests/SHA256SUMS':
            rows[relative].update(size=p.stat().st_size, sha256=sha(p))
    sums=OUTPUT/'manifests/SHA256SUMS'
    if sums.exists(): sums.unlink()
    sums.write_text(''.join(r['sha256']+'  '+r['path']+'\n' for r in sorted(rows.values(), key=lambda r:r['path']) if r['path']!='manifests/SHA256SUMS'), encoding='utf-8', newline='\n')
    rows['manifests/SHA256SUMS'].update(size=sums.stat().st_size, sha256=sha(sums))
    manifest=read(OUTPUT/'manifests/manifest.json')
    write(BUNDLE/'catalog/platform-manifest.json', manifest)
    catalog.update(release_id=RELEASE_ID, installer_version=VERSION, resources=list(rows.values()),
                   platform_manifest_sha256=sha(BUNDLE/'catalog/platform-manifest.json'), created_at='2026-10-15T00:00:00+00:00')
    known={p for c in catalog['components'] for p in c.get('files', [])}
    next(c for c in catalog['components'] if c['id']=='platform')['files'] += sorted(set(rows)-known)
    write(BUNDLE/'catalog/release-catalog.json', catalog)
    subprocess.run([openssl, 'pkeyutl', '-sign', '-inkey', str(key), '-rawin', '-in', str(BUNDLE/'catalog/release-catalog.json'), '-out', str(BUNDLE/'catalog/release-catalog.sig')], check=True)
    release=baseline('installer-bundle/release.json')
    release.update(installer_version=VERSION, release_id=RELEASE_ID, resource_release_id=RELEASE_ID,
                   catalog_sha256=sha(BUNDLE/'catalog/release-catalog.json'), resources_manifest_sha256=sha(OUTPUT/'manifests/manifest.json'), resources_checksums_sha256=sha(sums))
    write(BUNDLE/'release.json', release)
    spec=read(ROOT/'release-config.json'); spec.update(installer_version=VERSION, resource_release_id=RELEASE_ID, resources_root=OUTPUT.relative_to(ROOT).as_posix()); write(ROOT/'release-config.json', spec)
    external=read(ROOT/'external-assets.json'); assets={r['path']:r for r in external['files'] if not r['path'].startswith('build/resources-1.2.0/')}
    for p in OUTPUT.rglob('*'):
        if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc':
            relative=p.relative_to(ROOT).as_posix(); assets[relative]={'path':relative,'bytes':p.stat().st_size,'sha256':sha(p)}
    external['files']=[assets[k] for k in sorted(assets)]; write(ROOT/'external-assets.json', external)
    print(json.dumps({'version':VERSION,'resource_release_id':RELEASE_ID,'resources':len(rows),'trusted_key_preserved':True}))


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--key', type=Path); args=parser.parse_args()
    if (ROOT/'releases/1.2.0.json').exists() or (ROOT/'delivery/AI-Platform-1.2.0-linux-arm64').exists():
        raise SystemExit('Version already published; do not replace its resources')
    catalog, workflow=prepare()
    if args.key: finalize(catalog, workflow, args.key)


if __name__ == '__main__': main()
