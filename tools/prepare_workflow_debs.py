"""Resolve a complete Debian 12 ARM64 dependency closure in private APT state.

Publisher-only, download-only. No host packages or system APT settings are changed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'build/workflow-apt'
OUTPUT = ROOT / 'support-files/files/workflow/host-debs'
LOCK = ROOT / 'installer-bundle/catalog/workflow.json'


def main():
    global WORK
    parser=argparse.ArgumentParser();parser.add_argument('--refresh-lock',action='store_true');args=parser.parse_args()
    if os.name != 'posix':
        raise SystemExit('Run this download-only publisher utility through WSL')
    lock = json.loads(LOCK.read_text('utf-8'))
    if lock.get('host_packages') and not args.refresh_lock:
        WORK.mkdir(parents=True,exist_ok=True)
        for row in lock['host_packages']:
            target=ROOT/'support-files/files'/row['path']
            if not target.exists():
                if not row.get('urls'):raise RuntimeError('Restore the locked Packages index from Git')
                part=WORK/(target.name+'.restore-part')
                with urllib.request.urlopen(row['urls'][0],timeout=90) as source,part.open('wb') as output:shutil.copyfileobj(source,output)
                with part.open('rb') as stream:
                    if hashlib.file_digest(stream,'sha256').hexdigest()!=row['sha256']:raise RuntimeError('Locked dependency digest differs')
                target.parent.mkdir(parents=True,exist_ok=True);shutil.move(str(part),str(target))
            with target.open('rb') as stream:
                actual=hashlib.file_digest(stream,'sha256').hexdigest()
            if actual!=row['sha256'] or target.stat().st_size!=row['size']:raise RuntimeError('Changed locked dependency: '+target.name)
        print('Verified and reused '+str(len(lock['host_packages'])-1)+' locked Debian packages.');return
    if args.refresh_lock:
        WORK=Path(tempfile.mkdtemp(prefix='workflow-apt-refresh-',dir=ROOT/'build'))
    WORK.mkdir(parents=True, exist_ok=True);OUTPUT.mkdir(parents=True, exist_ok=True)
    for name in ['lists/partial', 'archives/partial', 'sourceparts', 'preferences']:
        (WORK / name).mkdir(parents=True, exist_ok=True)
    (WORK / 'status').touch()
    (WORK / 'sources.list').write_text(
        'deb [arch=arm64 signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://deb.debian.org/debian bookworm main\n'
        'deb [arch=arm64 signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://deb.debian.org/debian bookworm-updates main\n'
        'deb [arch=arm64 signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://security.debian.org/debian-security bookworm-security main\n')
    options = ['-o', 'APT::Architecture=arm64', '-o', 'APT::Architectures::=arm64',
               '-o', 'Dir::Etc::sourcelist=' + str(WORK / 'sources.list'),
               '-o', 'Dir::Etc::sourceparts=' + str(WORK / 'sourceparts'),
               '-o', 'Dir::Etc::preferencesparts=' + str(WORK / 'preferences'),
               '-o', 'Dir::State::status=' + str(WORK / 'status'),
               '-o', 'Dir::State::lists=' + str(WORK / 'lists'),
               '-o', 'Dir::Cache::archives=' + str(WORK / 'archives'),
               '-o', 'Acquire::Languages=none', '-o', 'APT::Sandbox::User=root']
    environment = os.environ.copy()
    for protocol in ['http', 'https']:
        if environment.get(protocol.upper() + '_PROXY'):
            environment[protocol + '_proxy'] = environment[protocol.upper() + '_PROXY']
    subprocess.run(['apt-get', *options, 'update'], env=environment, check=True)
    packages = lock['probe_packages'] + lock['component_packages']
    install = ['--yes', '--download-only', '--no-install-recommends', 'install', *packages]
    uris = subprocess.check_output(['apt-get', *options, '--print-uris', *install], env=environment, text=True)
    sources = {}
    for line in uris.splitlines():
        if line.startswith("'"):
            fields = shlex.split(line)
            sources[fields[1]] = fields[0]
    subprocess.run(['apt-get', *options, *install], env=environment, check=True)
    rows = [];index = []
    for source in sorted((WORK / 'archives').glob('*.deb')):
        control = subprocess.check_output(['dpkg-deb', '-f', str(source)], text=True)
        fields = dict(line.split(': ', 1) for line in control.splitlines() if ': ' in line and not line.startswith(' '))
        if fields['Architecture'] not in ['arm64', 'all']:
            raise RuntimeError('Incorrect host dependency architecture')
        target = OUTPUT / source.name;shutil.copyfile(source, target)
        data = target.read_bytes();digest = hashlib.sha256(data).hexdigest()
        uri = sources.get(source.name)
        if not uri:
            # Previously downloaded cache entries may be absent from --print-uris.
            cached = WORK / 'source-uris.json'
            uri = json.loads(cached.read_text()).get(source.name) if cached.exists() else None
        if not uri:
            raise RuntimeError('Missing verified download origin: ' + source.name)
        rows.append(dict(path=target.relative_to(ROOT / 'support-files/files').as_posix(),
                         size=len(data), sha256=digest, package=fields['Package'], version=fields['Version'],
                         architecture=fields['Architecture'], urls=[uri]))
        index.append(control.rstrip() + '\nFilename: ./' + source.name + '\nSize: ' + str(len(data))
                     + '\nSHA256: ' + digest + '\n')
    (WORK / 'source-uris.json').write_text(json.dumps(sources, indent=2) + '\n')
    package_index = OUTPUT / 'Packages';package_index.write_text('\n'.join(index), encoding='utf-8')
    rows.append(dict(path=package_index.relative_to(ROOT / 'support-files/files').as_posix(),
                     size=package_index.stat().st_size, sha256=hashlib.sha256(package_index.read_bytes()).hexdigest()))
    lock['host_packages'] = rows
    for name, required in [('nginx', '1.22.1'), ('etcd-server', '3.4.23')]:
        row = next(x for x in rows if x.get('package') == name)
        if not row['version'].startswith(required):
            raise RuntimeError('Unexpected component version: ' + name)
    LOCK.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'host_debs': len(rows) - 1, 'bytes': sum(x['size'] for x in rows)}, indent=2), flush=True)


if __name__ == '__main__':
    main()
