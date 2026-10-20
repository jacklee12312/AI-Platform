"""GB10 lifecycle failure boundaries and official weight verification."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'gb10-bundle'))
from gb10.core import Controller, LABEL, atomic, unchanged, verify_model, safe_file
from gb10 import operations
from gb10 import configuration
from gb10.evaluation import stable_snapshot


class WeightVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_official_sha256_and_git_blob_hash_then_detect_change(self):
        data = b'test weights'
        (self.root / 'weight').write_bytes(data)
        (self.root / 'config').write_bytes(b'{}')
        manifest = {'revision': 'a' * 40, 'files': [
            {'name': 'weight', 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()},
            {'name': 'config', 'size': 2, 'git_blob_sha1': hashlib.sha1(b'blob 2\0{}').hexdigest()}]}
        evidence = verify_model(self.root, manifest)
        unchanged(self.root, evidence)
        (self.root / 'weight').write_bytes(b'bad weights!')
        with self.assertRaises(ValueError):
            unchanged(self.root, evidence)
        with self.assertRaises(ValueError):
            verify_model(self.root, manifest)

    def test_escape_and_symlink_never_imported(self):
        with self.assertRaises(ValueError):
            safe_file(self.root, '../outside')
        outside = self.root / 'outside'
        outside.write_text('data')
        (self.root / 'link').symlink_to(outside)
        with self.assertRaises(ValueError):
            safe_file(self.root, 'link')


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        profile = json.loads((ROOT / 'gb10-bundle/profile.json').read_text())
        self.c = Controller({'model_root': str(self.root), 'management_token': 'fixture'}, profile, self.root)
        self.c.verified = Mock(return_value={})

    def test_unowned_container_is_never_stopped(self):
        self.c.inspect = Mock(return_value={'Config': {'Labels': {LABEL: 'false'}}})
        self.c.run = Mock()
        with self.assertRaises(ValueError):
            self.c.stop('ai-platform-gb10-model')
        self.c.run.assert_not_called()

    def test_wrong_answer_is_not_ready(self):
        self.c.http = Mock(return_value={'choices': [{'message': {'content': ''}}]})
        with self.assertRaises(RuntimeError):
            self.c.inference('gpt')
        self.c.http.return_value = {'choices': [{'message': {'content': '42'}}]}
        self.assertTrue(self.c.inference('gpt')['passed'])

    def test_failure_restores_previous_model_and_still_reports_failure(self):
        atomic(self.root / 'state/desired-mode', 'gpt\n')
        self.c.image = Mock()
        self.c.http = Mock(return_value={'requests': []})
        self.c.stop = Mock()
        self.c.launch = Mock(side_effect=[RuntimeError('out of memory'), {'passed': True}])
        with patch('gb10.core.unchanged'), patch('gb10.core.read', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'out of memory'):
                self.c.switch('nemotron')
        self.assertEqual([call.args[0] for call in self.c.launch.call_args_list], ['nemotron', 'gpt'])

    def test_invalid_mode_and_changed_weights_never_stop_service(self):
        self.c.stop = Mock()
        with self.assertRaises(ValueError):
            self.c.switch('untrusted-model')
        self.c.stop.assert_not_called()

    def test_new_model_uses_isolated_network_and_loopback_published_port(self):
        self.c.config.update(cache_root=str(self.root / 'cache'), tiktoken_root=str(self.root / 'tiktoken'))
        self.c.stop = Mock()
        self.c.image = Mock(return_value='sha256:fixture')
        self.c.run = Mock(return_value=SimpleNamespace(stdout='container-fixture'))
        self.c.inspect = Mock(return_value={'State': {'Running': True}})
        self.c.http = Mock(return_value={})
        self.c.inference = Mock(return_value={'passed': True})
        with patch('gb10.core.unchanged'), patch('gb10.core.read', return_value={}):
            self.c.launch('gpt', lambda _: None)
        command = self.c.run.call_args.args[0]
        self.assertEqual(command[command.index('--network')+1], 'bridge')
        self.assertEqual(command[command.index('-p')+1], '127.0.0.1:17701:8000')

    def test_unknown_gateway_state_restores_admission_without_stopping_model(self):
        atomic(self.root / 'state/desired-mode', 'gpt\n')
        self.c.image = Mock()
        self.c.http = Mock(return_value={})
        self.c.launch = Mock()
        with patch('gb10.core.unchanged'), patch('gb10.core.read', return_value={}):
            with self.assertRaisesRegex(RuntimeError, '在途请求'):
                self.c.switch('nemotron')
        self.c.launch.assert_not_called()
        self.assertEqual((self.root / 'state/active-mode').read_text(), 'gpt\n')

    def test_profile_separate_from_immutable_parent(self):
        spec = json.loads((ROOT / 'release-config.json').read_text())
        profile = json.loads((ROOT / 'gb10-bundle/profile.json').read_text())
        self.assertEqual(spec['profiles']['gb10']['resource_release_id'], profile['resource_release_id'])
        self.assertNotEqual(spec['resource_release_id'], profile['resource_release_id'])
        self.assertEqual(profile['runtime']['kv_cache_dtype'], 'fp8')
        self.assertFalse(profile['runtime']['prefix_cache'])

    def test_reused_verification_must_match_the_signed_manifest_revision(self):
        self.c.config['bundle'] = str(self.root / 'bundle')
        model_root = self.c.model_path('gpt')
        model_root.mkdir()
        (model_root / 'weights').write_bytes(b'weights')
        manifest = {'revision': 'a' * 40, 'files': [{'name': 'weights', 'size': 7,
                     'sha256': hashlib.sha256(b'weights').hexdigest()}]}
        path = self.root / 'bundle/models/gpt-oss-120b-mxfp4.manifest.json'
        atomic(path, manifest)
        self.c.verify_files('gpt')
        self.assertEqual(Controller.verified(self.c, 'gpt')['revision'], 'a' * 40)
        manifest['revision'] = 'b' * 40
        atomic(path, manifest)
        with self.assertRaisesRegex(ValueError, '签名发行'):
            Controller.verified(self.c, 'gpt')


class ComponentBoundaries(unittest.TestCase):
    def test_only_namespaced_allowlisted_names_can_be_operated(self):
        self.assertEqual(operations.name('redis'), 'ai-platform-gb10-redis')
        with self.assertRaises(ValueError):
            operations.name('1Panel-maxkb-G4Yt')

    def test_foreign_same_named_container_is_not_adopted(self):
        info = [{'Config': {'Labels': {}}, 'State': {'Running': True}}]
        with patch('gb10.operations.run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(info))):
            with self.assertRaisesRegex(ValueError, '未接管'):
                operations.inspect('mysql')


class ConfigurationAndEvaluationTests(unittest.TestCase):
    def body(self):
        return {'settings': {'chat_timeout_seconds': 300, 'generation_defaults_enabled': False},
                'models': [{'id': mid, 'enabled': True, 'base_url': 'http://127.0.0.1:17701'}
                           for mid in ('gpt-oss-120b', 'nemotron-3-super')]}

    def controller(self):
        return SimpleNamespace(profile=json.loads((ROOT / 'gb10-bundle/profile.json').read_text()))

    def test_configuration_cannot_inject_env_or_redirect_backend(self):
        body = self.body()
        self.assertTrue(configuration.validate(self.controller(), body)['valid'])
        body['settings']['LD_PRELOAD'] = '/tmp/inject.so'
        with self.assertRaises(ValueError):
            configuration.validate(self.controller(), body)
        body = self.body()
        body['models'][0]['base_url'] = 'http://untrusted.example'
        with self.assertRaises(ValueError):
            configuration.validate(self.controller(), body)

    def test_integer_environment_settings_reject_fractional_values(self):
        body = self.body()
        body['settings']['chat_timeout_seconds'] = 300.5
        with self.assertRaises(ValueError):
            configuration.validate(self.controller(), body)

    def test_evaluation_snapshot_ignores_heartbeat_but_detects_instance_change(self):
        value = {'runtime': {'container_id': 'one', 'boot_id': 'boot', 'verified': True, 'heartbeat_epoch': 1},
                 'modelctl': {'configured_mode': 'gpt'}}
        first = stable_snapshot(value, b'{}', b'{}')
        value['runtime']['heartbeat_epoch'] = 2
        self.assertEqual(first, stable_snapshot(value, b'{}', b'{}'))
        value['runtime']['container_id'] = 'two'
        self.assertNotEqual(first, stable_snapshot(value, b'{}', b'{}'))


if __name__ == '__main__':
    unittest.main()
