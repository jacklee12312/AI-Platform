"""Download hash-locked Linux Qt test wheels using the author's pip."""
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]


def main():
    lock = json.loads((ROOT / 'tools/test-dependencies.json').read_text('utf-8'))
    wheels = ROOT / 'build/test-wheels'
    wheels.mkdir(parents=True, exist_ok=True)
    requirements = wheels / 'requirements.txt'
    requirements.write_text('\n'.join([
        'PySide6-Essentials==' + lock['version'] + ' --hash=sha256:' + next(r['sha256'] for r in lock['wheels'] if r['filename'].startswith('pyside6_')),
        'shiboken6==' + lock['version'] + ' --hash=sha256:' + next(r['sha256'] for r in lock['wheels'] if r['filename'].startswith('shiboken6-')),
    ]) + '\n', encoding='utf-8')
    subprocess.run([sys.executable, '-m', 'pip', 'download', '--dest', str(wheels), '--only-binary=:all:',
                    '--platform', lock['platform'], '--python-version', '3.14', '--implementation', 'cp', '--abi', 'abi3',
                    '--require-hashes', '-r', str(requirements)], check=True)
    destination = ROOT / 'build/test-python'
    for row in lock['wheels']:
        path = wheels / row['filename']
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != row['sha256']:
                raise RuntimeError('Test wheel checksum mismatch: ' + path.name)
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                member = PurePosixPath(name)
                if member.is_absolute() or '..' in member.parts or '\\' in name:
                    raise RuntimeError('Invalid test wheel path')
            archive.extractall(destination)
    print('Qt test dependencies ready under build/test-python; ARM64 delivery runtime is unchanged.')


if __name__ == '__main__':
    main()
