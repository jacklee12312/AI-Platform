"""Exercise the selected-model wizard under the Debian 12 ARM64 runtime."""
import json
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

bundle = Path(__file__).resolve().parents[1]
root = Path(tempfile.mkdtemp(prefix='ai-platform-select-023-arm64-'))
for deb in Path('/mnt/f/AI-Platform-Offline-GUI-build-20260919/arm64-qa-debs').glob('*.deb'):
    subprocess.run(['dpkg-deb', '-x', str(deb), str(root)], check=True)
runtime = root / 'qt'
runtime.mkdir()
for wheel in (bundle / 'gui-runtime/linux-arm64').glob('*.whl'):
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(runtime)
for deb in (bundle / 'gui-runtime/debs').glob('*.deb'):
    subprocess.run(['dpkg-deb', '-x', str(deb), str(root)], check=True)
app = root / 'select-app-20260923'
app.mkdir()
for name in ('installer', 'catalog', 'assets', 'support-files'):
    shutil.copytree(bundle / name, app / name, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__'))
for name in ('release.json', 'sources.json'):
    shutil.copyfile(bundle / name, app / name)
qemu = ['qemu-aarch64', '-L', str(root)]
env = {
    'LD_LIBRARY_PATH': str(root / 'lib/aarch64-linux-gnu') + ':' +
                       str(root / 'usr/lib/aarch64-linux-gnu') + ':' +
                       str(runtime / 'PySide6/Qt/lib'),
    'PYTHONHOME': str(root / 'usr'),
    'PYTHONPATH': str(runtime),
    'QT_PLUGIN_PATH': str(runtime / 'PySide6/Qt/plugins'),
    'QT_QPA_PLATFORM': 'offscreen',
    'PYTHONDONTWRITEBYTECODE': '1',
}
for key, value in env.items():
    qemu += ['-E', key + '=' + value]
qemu += [str(root / 'usr/bin/python3.11')]
tests = subprocess.run(qemu + ['-m', 'unittest', 'discover', '-s',
                               str(app / 'installer/tests'), '-p', 'test_ui.py', '-v'],
                       capture_output=True, text=True, timeout=180)
match = re.search(r'Ran (\d+) tests?', tests.stdout + tests.stderr)
tests_count = int(match.group(1)) if match else 0
print('ARM64 UI tests', tests.returncode, tests.stderr[-700:], flush=True)
screens = []
for screen in (0, 1, 2, 3, 4, 5, 6):
    output = bundle / 'docs/evidence' / ('arm64-select-023-preview-' + str(screen) + '.png')
    result = subprocess.run(qemu + [str(app / 'installer/wizard.py'), '--bundle', str(app),
                                    '--preview', '--screen', str(screen),
                                    '--screenshot', str(output)],
                            capture_output=True, text=True, timeout=120)
    screens.append({'page': screen, 'exit_code': result.returncode,
                    'path': output.name, 'error': result.stderr[-500:]})
    print('ARM64 page', screen, result.returncode, flush=True)
report = {
    'date': time.strftime('%Y-%m-%d %H:%M:%S'),
    'environment': 'QEMU Debian 12 ARM64 Python 3.11 / Qt 6.7.3; preview only',
    'tests': tests_count, 'test_exit_code': tests.returncode,
    'test_log': tests.stdout + tests.stderr, 'screens': screens,
    'physical_install': False,
    'passed': tests.returncode == 0 and all(s['exit_code'] == 0 for s in screens),
}
(bundle / 'docs/evidence/arm64-gui-tests-023.json').write_text(
    json.dumps(report, ensure_ascii=False, indent=2) + '\n'
)
raise SystemExit(0 if report['passed'] else 1)
