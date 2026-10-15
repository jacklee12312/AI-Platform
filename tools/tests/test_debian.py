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
