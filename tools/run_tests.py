"""Run the installer and release-contract tests; preserve exact-commit evidence."""
import io
import json
import os
import platform
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if os.name != 'posix':
        raise SystemExit('Use python tools/release.py test to run POSIX tests via WSL')
    spec = json.loads((ROOT / 'release-config.json').read_text('utf-8'))
    def git(*args):
        return subprocess.check_output(['git','-C',str(ROOT),*args],text=True).strip()
    starting_commit=git('rev-parse','HEAD')
    starting_dirty=bool(git('status','--porcelain'))
    bundle = ROOT / spec['source_bundle']
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.discover(str(bundle / 'installer/tests'), pattern='test_*.py'))
    suite.addTests(unittest.TestLoader().discover(str(ROOT / 'tools/tests'), pattern='test_*.py'))
    output = io.StringIO()
    print('Running installer, Qt interaction and release workflow tests...', flush=True)
    with redirect_stdout(output), redirect_stderr(output):
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    log = output.getvalue()
    folder = ROOT / 'build/validation'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'tests.log').write_text(log, encoding='utf-8')
    report = {'schema': 1, 'source_commit': starting_commit,
              'source_dirty': starting_dirty or bool(git('status','--porcelain')) or git('rev-parse','HEAD')!=starting_commit, 'checked_at': datetime.now(timezone.utc).isoformat(),
              'python': sys.version, 'platform': platform.platform(), 'target_hardware': 'not_used',
              'qt_platform': os.environ.get('QT_QPA_PLATFORM'), 'tests_run': result.testsRun,
              'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped),
              'passed': result.wasSuccessful() and not result.skipped}
    (folder / 'tests.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if not result.wasSuccessful():
        print(log)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
