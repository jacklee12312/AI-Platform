"""Prepare 1.1.2 resources without changing any prior signed resource bytes."""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'support-files/files'
OVERLAY = ROOT / 'platform-overlays/1.1.2'
OUTPUT = ROOT / 'build/resources-1.1.2'
BUNDLE = ROOT / 'installer-bundle'
VERSION = '1.1.2'
RELEASE_ID = 'ai-platform-installer-1.1.2-20261015'
REMOVED_MODELS = {'qwen3guard-stream-0.6b', 'democratic-eval-v1'}
REMOVED_IMAGES = {'qwen3guard-stream-cpu:0.1.0', 'ballot-reader:0.1.0'}


def read(path): return json.loads(Path(path).read_text('utf-8'))


def write(path, value):
    path = Path(path);path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')


def sha(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()


def baseline(relative):
    return json.loads(subprocess.check_output(['git', '-C', str(ROOT), 'show', 'v1.1.1:' + relative]))


def excluded(path):
    return any(x in path for x in ('/ballot-reader/', '/qwen3guard-stream-cpu/', '/qwen3guard-stream-0.6b/')) or path.startswith('tests/synthetic-ballot')


def without_guard_service(text):
    return re.sub(r'^  qwen3guard:\s*\n.*?(?=^  [a-zA-Z0-9_-]+:\s*$|\Z)', '', text, flags=re.M | re.S)


def cleaned(value):
    if isinstance(value, list):
        return [cleaned(x) for x in value if not isinstance(x, dict) or x.get('id') not in REMOVED_MODELS]
    if isinstance(value, dict):
        return {k: cleaned(v) for k, v in value.items() if k not in REMOVED_MODELS | {'qwen3guard-stream-cpu', 'ballot-reader', 'guard', 'guarded', 'guard_mode', 'guard_mode_default'}}
    return value


def image_archive(source, target, expected):
    if sha(source) != expected: raise RuntimeError('Original image archive changed')
    record = ROOT / 'build/size-audit/slim-image-archive.json'
    if target.is_file() and record.is_file() and sha(target) == read(record)['sha256']: return
    docs = {}
    with source.open('rb') as raw, gzip.GzipFile(fileobj=raw) as zipped, tarfile.open(fileobj=zipped, mode='r|') as tar:
        for member in tar:
            if member.isfile() and member.size < 524288:
                try: docs[member.name] = json.loads(tar.extractfile(member).read())
                except (ValueError, UnicodeDecodeError): pass
    manifest = [dict(row, RepoTags=[t for t in row['RepoTags'] if t not in REMOVED_IMAGES]) for row in docs['manifest.json']]
    manifest = [row for row in manifest if row['RepoTags']]
    index = copy.deepcopy(docs['index.json'])
    index['manifests'] = [d for d in index['manifests'] if not any(
        d.get('annotations', {}).get('io.containerd.image.name', '').endswith('/' + tag) for tag in REMOVED_IMAGES)]
    keep = {'manifest.json', 'index.json', 'oci-layout'}
    def visit(path):
        if path in keep: return
        keep.add(path)
        doc = docs.get(path)
        def walk(value):
            if isinstance(value, dict):
                if isinstance(value.get('digest'), str): visit('blobs/sha256/' + value['digest'].split(':')[1])
                for v in value.values(): walk(v)
            elif isinstance(value, list):
                for v in value: walk(v)
        if doc is not None: walk(doc)
    for descriptor in index['manifests']: visit('blobs/sha256/' + descriptor['digest'].split(':')[1])
    for row in manifest:
        visit(row['Config'])
        for path in row['Layers']: visit(path)
    replacements = {'manifest.json': json.dumps(manifest, separators=(',', ':')).encode(),
                    'index.json': json.dumps(index, separators=(',', ':')).encode()}
    target.parent.mkdir(parents=True, exist_ok=True);part = target.with_suffix('.part')
    print('Repacking 10 platform image tags; retaining shared layers required by Kokoro...', flush=True)
    with source.open('rb') as raw, gzip.GzipFile(fileobj=raw) as src, tarfile.open(fileobj=src, mode='r|') as original:
        with part.open('wb') as stream, gzip.GzipFile(fileobj=stream, mode='wb', filename='', mtime=0, compresslevel=6) as zipped, tarfile.open(fileobj=zipped, mode='w|') as packed:
            for member in original:
                if not member.isfile() or member.name not in keep: continue
                data = replacements.get(member.name)
                item = tarfile.TarInfo(member.name);item.mode = 0o644;item.size = len(data) if data is not None else member.size
                packed.addfile(item, io.BytesIO(data) if data is not None else original.extractfile(member))
    part.replace(target)
    write(record, {'sha256': sha(target), 'bytes': target.stat().st_size,
                   'retained_tags': sorted(t for row in manifest for t in row['RepoTags']),
                   'removed_tags': sorted(REMOVED_IMAGES), 'shared_kokoro_layers_preserved': True})


def prepare():
    catalog = baseline('installer-bundle/catalog/release-catalog.json')
    rows = [dict(r, models=[m for m in r.get('models', []) if m not in REMOVED_MODELS]) for r in catalog['resources']
            if not excluded(r['path']) and (r['category'] != 'models' or set(r.get('models', [])) - REMOVED_MODELS)]
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for row in rows:
        relative = row['path']
        if row['category'] == 'models' or relative in {'images/all-arm64-images.tar.gz', 'runtime/ui-static.tar.gz'}: continue
        source = OVERLAY / 'files' / relative
        if not source.is_file(): source = BASE / relative
        target = OUTPUT / relative;target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or sha(target) != sha(source): shutil.copyfile(source, target)
        if target.suffix in {'.yaml', '.yml'} and 'compose' in target.name:
            target.write_text(without_guard_service(target.read_text('utf-8')), encoding='utf-8', newline='\n')
        if relative.endswith('.json') and any(x in relative for x in (
            'config/models.json', '/model_catalog.json', '/runtime_baseline.json', '/capability_catalog.json', 'manifests/model-catalog.json', 'manifests/versions.json')):
            write(target, cleaned(read(target)))
    image_row = next(r for r in rows if r['path'] == 'images/all-arm64-images.tar.gz')
    image_archive(BASE / image_row['path'], OUTPUT / image_row['path'], image_row['sha256'])
    workflow = baseline('installer-bundle/catalog/workflow.json')
    workflow['image_acquisition'] = 'on-demand-pinned-registry-or-local-import'
    for row in [workflow['probe']['archive'], *workflow['host_packages']]:
        target = OUTPUT / row['path'];target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists(): shutil.copyfile(BASE / row['path'], target)
    write(ROOT / 'build/slim-preparation.json', {'version': VERSION, 'resources': str(OUTPUT), 'rows': len(rows)})
    return catalog, rows, workflow


def finalize(catalog, rows, workflow, key):
    dist = OVERLAY / 'modeldeck-frontend/dist'
    if not (dist / 'index.html').is_file(): raise RuntimeError('Build the ModelDeck frontend first')
    ui = OUTPUT / 'runtime/ui-static.tar.gz'
    with ui.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as zipped, tarfile.open(fileobj=zipped, mode='w|') as tar:
        for path in sorted(p for p in dist.rglob('*') if p.is_file()):
            data=path.read_bytes();item=tarfile.TarInfo(path.relative_to(dist).as_posix());item.mode=0o644;item.size=len(data);tar.addfile(item,io.BytesIO(data))
    manifest = read(BASE / 'manifests/manifest.json')
    manifest.update(release_id=RELEASE_ID, models=[x for x in manifest['models'] if x not in REMOVED_MODELS],
                    images=[x for x in manifest['images'] if x['tag'] not in REMOVED_IMAGES])
    removed_bytes = sum(r['size'] for r in catalog['resources'] if r['category']=='models' and not set(r.get('models', []))-REMOVED_MODELS)
    for field in ('installed_payload_bytes', 'all_model_file_bytes'): manifest[field] -= removed_bytes
    manifest['source'] += '; installer 1.1.2: on-demand middleware; content moderation and ballot service removed'
    write(OUTPUT / 'manifests/manifest.json', manifest);write(OUTPUT / 'manifests/image-lock.json', manifest['images'])
    # New overlays may add bind-mounted modules that previously lived only inside a base image.
    known={r['path'] for r in rows}
    for path in sorted((OVERLAY/'files').rglob('*')):
        if path.is_file():
            relative=path.relative_to(OVERLAY/'files').as_posix()
            if relative not in known:
                target=OUTPUT/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
                rows.append({'path':relative,'category':'platform','models':[]});known.add(relative)
    for row in rows:
        if row['category']!='models' and row['path']!='manifests/SHA256SUMS':
            path=OUTPUT/row['path'];row.update(size=path.stat().st_size,sha256=sha(path))
    sums=OUTPUT/'manifests/SHA256SUMS'
    sums.write_text(''.join(r['sha256']+'  '+r['path']+'\n' for r in sorted(rows,key=lambda r:r['path']) if r['path']!='manifests/SHA256SUMS'),encoding='utf-8',newline='\n')
    next(r for r in rows if r['path']=='manifests/SHA256SUMS').update(size=sums.stat().st_size,sha256=sha(sums))
    write(BUNDLE/'catalog/workflow.json',workflow);write(BUNDLE/'catalog/platform-manifest.json',manifest)
    catalog.update(release_id=RELEASE_ID,installer_version=VERSION,models=manifest['models'],image_count=len(manifest['images']),resources=rows,
                   workflow_sha256=sha(BUNDLE/'catalog/workflow.json'),platform_manifest_sha256=sha(BUNDLE/'catalog/platform-manifest.json'))
    catalog['model_device_counts']={k:v for k,v in catalog.get('model_device_counts',{}).items() if k not in REMOVED_MODELS}
    catalog['components']=[c for c in catalog['components'] if c.get('id') not in REMOVED_MODELS]
    for component in catalog['components']:
        component['files']=[p for p in component.get('files',[]) if p in known]
        if component['id']=='platform':component['files']+=sorted(p for p in known if p not in {x for c in catalog['components'] for x in c.get('files',[])})
    write(BUNDLE/'catalog/release-catalog.json',catalog)
    openssl=shutil.which('openssl') or r'C:\Program Files\Git\usr\bin\openssl.exe'
    if key.resolve().is_relative_to(ROOT): raise RuntimeError('Signing key must remain outside the project')
    public=subprocess.check_output([openssl,'pkey','-in',str(key),'-pubout'])
    if public.replace(b'\r\n',b'\n')!=(BUNDLE/'catalog/release-public.pem').read_bytes().replace(b'\r\n',b'\n'):raise RuntimeError('Signing key does not match the trusted public key')
    subprocess.run([openssl,'pkeyutl','-sign','-inkey',str(key),'-rawin','-in',str(BUNDLE/'catalog/release-catalog.json'),'-out',str(BUNDLE/'catalog/release-catalog.sig')],check=True)
    release=baseline('installer-bundle/release.json')
    release.update(installer_version=VERSION,release_id=RELEASE_ID,resource_release_id=RELEASE_ID,models=len(manifest['models']),images=len(manifest['images'])+6,
                   catalog_sha256=sha(BUNDLE/'catalog/release-catalog.json'),resources_manifest_sha256=sha(OUTPUT/'manifests/manifest.json'),resources_checksums_sha256=sha(sums))
    write(BUNDLE/'release.json',release)
    spec=read(ROOT/'release-config.json');spec.update(installer_version=VERSION,resource_release_id=RELEASE_ID,resources_root=OUTPUT.relative_to(ROOT).as_posix());write(ROOT/'release-config.json',spec)
    sources=baseline('installer-bundle/sources.json')
    sources['file_urls']={p:v for p,v in sources['file_urls'].items() if not excluded(p)}
    sources['oci_artifacts']={k:v for k,v in sources['oci_artifacts'].items() if not any(excluded(p) for p in v['files'])}
    for name in ['sources.json','sources.example.json']:write(BUNDLE/name,sources)
    external=read(ROOT/'external-assets.json');assets={r['path']:r for r in external['files']}
    for path in OUTPUT.rglob('*'):
        if path.is_file():
            relative=path.relative_to(ROOT).as_posix();assets[relative]={'path':relative,'bytes':path.stat().st_size,'sha256':sha(path)}
    external['files']=[assets[k] for k in sorted(assets)];write(ROOT/'external-assets.json',external)
    print(json.dumps({'installer_version':VERSION,'resource_release_id':RELEASE_ID,'models':len(manifest['models']),'platform_images':len(manifest['images']),'old_resources_unchanged':True}))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--key',type=Path);args=parser.parse_args()
    if (ROOT/'releases'/f'{VERSION}.json').exists() or (ROOT/'delivery'/f'AI-Platform-{VERSION}-linux-arm64').exists():
        raise SystemExit('This version already has a delivery; restore locked assets or prepare a new resource version')
    catalog,rows,workflow=prepare()
    if args.key:finalize(catalog,rows,workflow,args.key)


if __name__=='__main__':main()
