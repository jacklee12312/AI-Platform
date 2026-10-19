"""Release gates protect signed content, exact-version workers and published files."""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('release_workflow', ROOT / 'tools/release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
BUNDLE = ROOT / 'installer-bundle'


class ReleaseWorkflowTests(unittest.TestCase):
    def test_launcher_accepts_the_multi_gigabyte_yiqiban_bundle(self):
        spec=importlib.util.spec_from_file_location('launcher_entry_test',BUNDLE/'tools/launcher_entry.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        row=zipfile.ZipInfo('bundle/yiqiban/image.tar');row.file_size=4_515_569_551
        self.assertEqual(module.archive_size([row]),row.file_size)

    def test_launcher_keeps_a_finite_limit_before_writing_large_archives(self):
        spec=importlib.util.spec_from_file_location('launcher_entry_limit_test',BUNDLE/'tools/launcher_entry.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        row=zipfile.ZipInfo('bundle/oversized.tar');row.file_size=16*1024**3+1
        with self.assertRaisesRegex(RuntimeError,'大小'):module.archive_size([row])

    def test_historical_1_0_1_preserves_the_1_0_0_trust_and_resource_baseline(self):
        for name in ('release-catalog.json', 'release-catalog.sig', 'release-public.pem', 'platform-manifest.json'):
            original = subprocess.check_output(['git', '-C', str(ROOT), 'show', 'v1.0.0:AI-Platform-Installer-1.0.0/catalog/' + name])
            preserved=subprocess.check_output(['git','-C',str(ROOT),'show','v1.0.1:installer-bundle/catalog/'+name])
            self.assertEqual(original,preserved,name)
            if name=='release-public.pem':self.assertEqual(original,(BUNDLE/'catalog'/name).read_bytes(),name)

    def test_shared_projector_changes_only_repository(self):
        old = json.loads(subprocess.check_output(['git', '-C', str(ROOT), 'show', 'v1.0.0:AI-Platform-Installer-1.0.0/sources.json']))
        new = release.read(BUNDLE / 'sources.json')
        for key in ('qwen38-1-mmproj', 'qwen38-2-mmproj'):
            old['oci_artifacts'][key]['repository'] = 'aistore/qwen3-8-27b-mmproj'
        old['file_urls']={p:v for p,v in old['file_urls'].items() if 'qwen3guard' not in p}
        old['oci_artifacts'].pop('qwen3guard-stream-0.6b')
        self.assertEqual(new, old)

    def test_copied_worker_reads_its_own_version_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('common.py', 'version.py'):
                shutil.copyfile(BUNDLE / 'installer' / name, root / name)
            release.write(root / 'release.json', {'installer_version': '9.8.7'})
            text = subprocess.check_output([sys.executable, '-B', '-c', 'import common; print(common.PRODUCT)'], cwd=root, text=True)
            self.assertIn('9.8.7', text)

    def test_delivery_checks_include_nested_checksum_files_and_reject_tampering(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            nested = root / 'support-files/manifests/SHA256SUMS'
            nested.parent.mkdir(parents=True)
            nested.write_bytes(b'locked content')
            (root / 'SHA256SUMS').write_text(release.digest(nested) + '  support-files/manifests/SHA256SUMS\n')
            self.assertEqual(release.verify_checksums(root), 1)
            nested.write_bytes(b'changed')
            with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                release.verify_checksums(root)

    def test_paths_cannot_escape_release_directory(self):
        for name in ('../outside', '/etc/passwd', 'C:/outside', 'a\\b'):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                release.inside(ROOT, name)

    def test_archive_requires_executable_launchers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            delivery = root / 'AI-Platform-1.0.1-linux-arm64'
            delivery.mkdir()
            launcher = delivery / 'setup.run'
            launcher.write_bytes(b'fixture')
            for mode, valid in ((0o644, False), (0o755, True)):
                archive = root / ('archive-' + str(mode) + '.tar.gz')
                with tarfile.open(archive, 'w:gz') as tar:
                    def permissions(info):
                        info.mode = mode
                        return info
                    tar.add(launcher, arcname=delivery.name + '/setup.run', filter=permissions)
                if valid:
                    self.assertEqual(release.verify_archive(archive, delivery), 1)
                else:
                    with self.assertRaisesRegex(RuntimeError, 'mode mismatch'):
                        release.verify_archive(archive, delivery)

    def test_build_refuses_existing_delivery_without_changing_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            delivery = root / 'delivery/AI-Platform-1.0.1-linux-arm64'
            delivery.mkdir(parents=True)
            sentinel = delivery / 'original'
            sentinel.write_text('keep')
            release.write(root / 'build/validation/tests.json', {'passed': True, 'source_dirty': False, 'source_commit': 'commit', 'skipped': 0})
            def git(_root, *args):
                return '' if args[0] == 'status' else 'commit'
            with mock.patch.object(release, 'git', side_effect=git), mock.patch.object(release, 'config', return_value={'installer_version': '1.0.1'}):
                with self.assertRaisesRegex(RuntimeError, 'never overwrite'):
                    release.build(root)
            self.assertEqual(sentinel.read_text(), 'keep')

    def test_launcher_archive_is_independent_of_source_file_mtimes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bundle = root / 'bundle'
            (bundle / 'tools').mkdir(parents=True)
            (bundle / 'tools/launcher_entry.py').write_text('print("fixture")\n')
            (bundle / 'sources.json').write_text('{}\n')
            (bundle / 'SHA256SUMS').write_text('fixture\n')
            release.write(bundle / 'installer-manifest.json', {'files': [{'path': 'tools/launcher_entry.py'}]})
            archives = []
            for index, mtime in enumerate((1600000000, 1700000000)):
                for path in bundle.rglob('*'):
                    os.utime(path, (mtime, mtime))
                output = root / f'setup-{index}.run'
                subprocess.run([sys.executable, '-B', str(BUNDLE / 'tools/pack_launcher_023.py'), '--bundle', str(bundle), '--output', str(output)],
                               env=dict(os.environ, SOURCE_DATE_EPOCH='1700000000'), check=True, capture_output=True)
                archives.append(release.digest(output))
            self.assertEqual(archives[0], archives[1])


if __name__ == '__main__':
    unittest.main()
