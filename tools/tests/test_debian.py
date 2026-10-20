"""Test actual Debian extraction, executable modes, integrity and writable sources."""
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tools' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


debian, entry = load('debian'), load('debian_entry')


class DebianTests(unittest.TestCase):
    def test_packaging_base_can_live_in_build_cache_after_delivery_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            name = 'AI-Platform-1.3.0-linux-arm64'
            record = {'archive': 'delivery/' + name + '.tar.gz'}
            with self.assertRaisesRegex(RuntimeError, 'Missing immutable base'):
                debian.base_paths(root, '1.3.0', record)
            cache = root / 'build/release-bases/1.3.0'
            (cache / name).mkdir(parents=True)
            (cache / (name + '.tar.gz')).write_bytes(b'unchanged archive')
            self.assertEqual(debian.base_paths(root, '1.3.0', record),
                             (cache / name, cache / (name + '.tar.gz')))
            published = root / 'delivery'
            (published / name).mkdir(parents=True)
            (published / (name + '.tar.gz')).write_bytes(b'published archive')
            self.assertEqual(debian.base_paths(root, '1.3.0', record),
                             (published / name, published / (name + '.tar.gz')))

    def test_code_overlay_preserves_signed_resources_and_rejects_resource_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source';reference=root/'reference'
            for base in (source,reference):
                (base/'catalog').mkdir(parents=True)
                (base/'catalog/signed.json').write_bytes(b'exact signed bytes')
                (base/'sources.json').write_bytes(b'{}')
                (base/'installer').mkdir()
                (base/'installer/probe.py').write_bytes(b'old code')
            (source/'installer/probe.py').write_bytes(b'fixed code')
            debian.overlay_bundle(source,reference,root/'output')
            self.assertEqual((root/'output/installer/probe.py').read_bytes(),b'fixed code')
            self.assertEqual((root/'output/catalog/signed.json').read_bytes(),b'exact signed bytes')
            (source/'catalog/signed.json').write_bytes(b'changed bytes')
            with self.assertRaisesRegex(RuntimeError,'cannot change immutable'):
                debian.overlay_bundle(source,reference,root/'must-not-exist')
            self.assertFalse((root/'must-not-exist').exists())

    def test_slim_overlay_omits_payload_before_sealing_and_keeps_signed_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for base in (root/'source',root/'reference'):
                (base/'yiqiban/files').mkdir(parents=True)
                (base/'yiqiban/files/image.tar').write_bytes(b'large image omitted from distribution')
                (base/'yiqiban/manifest.json').write_bytes(b'exact signed manifest bytes')
                (base/'yiqiban/source.json').write_text('{"pinned":"remote source"}')
                (base/'sources.json').write_bytes(b'{}')
            debian.overlay_bundle(root/'source',root/'reference',root/'output')
            self.assertFalse((root/'output/yiqiban/files').exists())
            self.assertEqual((root/'output/yiqiban/manifest.json').read_bytes(),b'exact signed manifest bytes')
            self.assertTrue((root/'output/yiqiban/source.json').is_file())
            self.assertTrue((root/'source/yiqiban/files/image.tar').is_file())
    def test_real_dpkg_extracts_owned_files_with_modes_and_reproducible_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = {'usr/bin/fixture': (b'#!/bin/sh\nexit 0\n', 0o755),
                     'opt/fixture/resource': (b'unchanged signed bytes', 0o644)}
            control = b'Package: fixture\nVersion: 1.2.0-1\nArchitecture: arm64\nMaintainer: Test <test@example.invalid>\nDescription: Fixture\n'
            output = root / 'fixture.deb'
            debian.pack(output, files, {'control': (control, 0o644)}, 1700000000, root)
            self.assertIn('Architecture: arm64', subprocess.check_output(['dpkg-deb', '-f', str(output)], text=True))
            self.assertIn('-rwxr-xr-x 0/0', subprocess.check_output(['dpkg-deb', '-c', str(output)], text=True))
            subprocess.run(['dpkg-deb', '-x', str(output), str(root / 'extracted')], check=True)
            self.assertEqual((root / 'extracted/opt/fixture/resource').read_bytes(), b'unchanged signed bytes')
            self.assertEqual((root / 'extracted/usr/bin/fixture').stat().st_mode & 0o777, 0o755)
            first = output.read_bytes()
            output.unlink()
            debian.pack(output, files, {'control': (control, 0o644)}, 1700000000, root)
            self.assertEqual(first, output.read_bytes())

    def test_user_config_locates_payload_and_preserves_choices(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app, home = root / 'readonly-app', root / 'home'
            (app / 'bundle').mkdir(parents=True)
            (app / 'bundle/release.json').write_text('{"installer_version":"1.2.0"}')
            original = '{"model_source":"online","local_roots":["../support-files"]}'
            (app / 'bundle/sources.json').write_text(original)
            target = entry.prepare_sources(app, home, {})
            value = json.loads(target.read_text())
            self.assertEqual(value['local_roots'], ['@bundle/support-files', str(app / 'support-files'), str(home / 'AI-Platform-Model-Library')])
            value['model_source'] = 'local'
            target.write_text(json.dumps(value))
            self.assertEqual(entry.prepare_sources(app, home, {}), target)
            self.assertEqual(json.loads(target.read_text())['model_source'], 'local')
            self.assertEqual((app / 'bundle/sources.json').read_text(), original)
            self.assertEqual(entry.prepare_sources(app, home, {'XDG_CONFIG_HOME': 'relative'}), target)
            custom = entry.prepare_sources(app, home, {'XDG_CONFIG_HOME': str(home / 'custom')})
            self.assertTrue(custom.is_relative_to(home / 'custom'))
            target.unlink()
            target.symlink_to(app / 'bundle/sources.json')
            with self.assertRaisesRegex(RuntimeError, '符号链接'):
                entry.prepare_sources(app, home, {})

    def test_integrity_rejects_changed_files_and_traversal(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            resource = root / 'resource'
            resource.write_bytes(b'original')
            manifest = {'files': [{'path': 'resource', 'sha256': __import__('hashlib').sha256(b'original').hexdigest()}]}
            (root / 'installer-manifest.json').write_text(json.dumps(manifest))
            entry.verify_bundle(root)
            resource.write_bytes(b'tampered')
            with self.assertRaisesRegex(RuntimeError, '校验失败'):
                entry.verify_bundle(root)
            manifest['files'][0]['path'] = '../outside'
            (root / 'installer-manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(RuntimeError, '校验失败'):
                entry.verify_bundle(root)

    def test_existing_online_config_recovers_packaged_resources_without_importing_models(self):
        import sys
        sys.path.insert(0, str(ROOT / 'installer-bundle/installer'))
        from artifacts import plan_sources
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app, home, library = root / 'app', root / 'home', root / 'model-library'
            (app / 'bundle').mkdir(parents=True)
            (app / 'bundle/release.json').write_text('{"installer_version":"1.3.0"}')
            target = home / '.config/ai-platform-installer/1.3.0/sources.json'
            target.parent.mkdir(parents=True)
            config = {'schema': 1, 'mode': 'prefer-local', 'model_source': 'online',
                      'local_roots': ['@bundle/support-files', str(library)],
                      'file_urls': {'model.gguf': ['https://models.example.invalid/model.gguf']},
                      'base_urls': {}, 'allow_http': False, 'retries': 2,
                      'operator_setting': 'preserve this value'}
            target.write_text(json.dumps(config))
            target.chmod(0o600)
            rows = []
            for path, category, directory in [('README.md', 'platform', app / 'support-files'),
                                              ('docker/engine.deb', 'docker', app / 'support-files'),
                                              ('model.gguf', 'models', library)]:
                file = directory / 'files' / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(b'test resource')
                rows.append({'path': path, 'category': category, 'sha256': __import__('hashlib').sha256(file.read_bytes()).hexdigest(),
                             'size': file.stat().st_size, 'models': ['model'] if category == 'models' else []})
            catalog = {'resources': rows}
            self.assertFalse(plan_sources(app / 'bundle', catalog, config, root / 'cache')['ready'])
            self.assertEqual(entry.prepare_sources(app, home, {}), target)
            fixed = json.loads(target.read_text())
            self.assertEqual({k: v for k, v in fixed.items() if k != 'local_roots'},
                             {k: v for k, v in config.items() if k != 'local_roots'})
            self.assertEqual(fixed['local_roots'], ['@bundle/support-files', str(app / 'support-files'), str(library)])
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            plan = plan_sources(app / 'bundle', catalog, fixed, root / 'cache')
            self.assertTrue(plan['ready'])
            self.assertEqual({row['path']: row['source'] for row in plan['rows']},
                             {'README.md': 'local', 'docker/engine.deb': 'local', 'model.gguf': 'remote'})
            before = target.read_bytes(), target.stat().st_mtime_ns
            entry.prepare_sources(app, home, {})
            self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns), before)
            self.assertEqual(list(target.parent.glob('.sources-*')), [])

    def test_existing_local_config_keeps_import_choice_when_bundle_marker_is_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app, home = root / 'app', root / 'home'
            (app / 'bundle').mkdir(parents=True)
            (app / 'bundle/release.json').write_text('{"installer_version":"1.3.0"}')
            target = home / '.config/ai-platform-installer/1.3.0/sources.json'
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({'mode': 'prefer-local', 'model_source': 'local',
                                          'local_roots': ['/media/operator/models', str(app / 'support-files')],
                                          'oci_artifacts': {'keep': 'custom repository'}}))
            entry.prepare_sources(app, home, {})
            value = json.loads(target.read_text())
            self.assertEqual(value['model_source'], 'local')
            self.assertEqual(value['local_roots'], ['@bundle/support-files', str(app / 'support-files'), '/media/operator/models'])
            self.assertEqual(value['oci_artifacts'], {'keep': 'custom repository'})

    def test_standard_app_metadata_has_no_custom_file_association(self):
        import xml.etree.ElementTree as ET
        desktop, appstream = debian.metadata('1.2.0', '1.2.0-1', '2026-10-15')
        self.assertIn(b'Exec=/usr/bin/ai-platform-installer\n', desktop)
        self.assertNotIn(b'MimeType', desktop)
        tree = ET.fromstring(appstream)
        self.assertEqual(tree.find('launchable').text, 'com.ai-platform.Installer.desktop')
        self.assertEqual(tree.find('releases/release').get('version'), '1.2.0-1')

    def test_pack_refuses_path_traversal_and_existing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaisesRegex(RuntimeError, 'Invalid package path'):
                debian.pack(root / 'bad.deb', {'../escape': (b'x', 0o644)}, {}, 1, root)
            output = root / 'keep'
            output.write_bytes(b'original')
            with self.assertRaises(FileExistsError):
                debian.write_ar(output, [], 1)
            self.assertEqual(output.read_bytes(), b'original')
