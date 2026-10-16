"""Offline signature, missing/corrupt libraries, compatibility, and crash recovery."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import atomic_json,sha256
import npu_payload as payload
import npu_runtime as runtime

BUNDLE=Path(__file__).resolve().parents[2]
BASE='services:\n  kokoro-tts:\n    image: kokoro-cix:0.2.0\n    volumes:\n    - ./model:/models:ro\n    environment:\n      HF_HUB_OFFLINE: "1"\n'


def mkdir(path):Path(path).mkdir(parents=True,exist_ok=True,mode=0o755)
def result(code):return subprocess.CompletedProcess([],code,stdout='',stderr='incompatible' if code else '')


class NpuOfflineTests(unittest.TestCase):
    def fixture(self,root):
        resources=root/'resources';state=root/'state';home=root/'home'
        source=resources/runtime.RELATIVE;source.parent.mkdir(parents=True);source.write_text(BASE)
        target=home/'kokoro-cix/docker-compose.yml';target.parent.mkdir(parents=True);target.write_text(BASE)
        (state/'private/local-configs').mkdir(parents=True)
        installer=SimpleNamespace(bundle=BUNDLE,root=state,home=home,resources=resources,
                                  selected=['kokoro-82m'],event=mock.Mock())
        manifest,archive=payload.load(BUNDLE)
        prepared=(state/'host-runtime/releases'/payload.RELEASE_ID,manifest)
        return installer,source,target,prepared

    def old_record(self,installer,source,target):
        folder=installer.root/'host-runtime/kokoro'
        generated=installer.root/'private/local-configs/kokoro-compose.yml'
        generated.write_text(runtime.render(BASE,folder));target.write_bytes(generated.read_bytes())
        record={'schema':1,'base_sha256':sha256(source),'compose_sha256':sha256(generated),
                'files':{str(folder/name):hashlib.sha256(b'old-library').hexdigest() for name in payload.MEMBERS}}
        atomic_json(runtime.record_file(installer.root,'kokoro'),record)
        return generated,record

    def test_real_signed_archive_and_vendor_file_hashes_verify(self):
        manifest,archive=payload.load(BUNDLE);data=payload.contents(manifest,archive)
        self.assertEqual(manifest['release_id'],payload.RELEASE_ID)
        self.assertEqual(set(data),payload.MEMBERS)
        self.assertLess(archive.stat().st_size,2*1024**2)

    def test_signature_and_archive_tampering_rejected_before_install(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'catalog').mkdir()
            shutil.copyfile(BUNDLE/'catalog/release-public.pem',root/'catalog/release-public.pem')
            shutil.copyfile(BUNDLE/'release.json',root/'release.json')
            shutil.copytree(BUNDLE/'npu-runtime',root/'npu-runtime')
            p=root/'npu-runtime/manifest.json';before=p.read_bytes();p.write_bytes(before+b' ')
            with self.assertRaisesRegex(RuntimeError,'签名'):payload.load(root)
            p.write_bytes(before)
            archive=root/'npu-runtime/cix-noe-3.1.2-arm64.tar.gz'
            with archive.open('ab') as stream:stream.write(b'tamper')
            with self.assertRaisesRegex(RuntimeError,'归档校验'):payload.load(root)

    def test_tar_extra_link_duplicate_and_missing_files_rejected(self):
        manifest,_=payload.load(BUNDLE)
        for kind in ['traversal','symlink','duplicate','missing']:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as folder:
                p=Path(folder)/'test.tgz'
                with tarfile.open(p,'w:gz') as tar:
                    if kind!='missing':
                        entry=tarfile.TarInfo('../outside' if kind=='traversal' else 'lib/libnoe.so')
                        if kind=='symlink':entry.type=tarfile.SYMTYPE;entry.linkname='/etc/passwd'
                        tar.addfile(entry)
                        if kind=='duplicate':tar.addfile(entry)
                with self.assertRaises(RuntimeError):payload.contents(manifest,p)
                self.assertFalse((Path(folder)/'outside').exists())

    def test_missing_files_installed_and_corruption_backed_up_then_repaired(self):
        with tempfile.TemporaryDirectory() as folder,mock.patch('npu_payload.directory',side_effect=mkdir):
            root=Path(folder);events=[];path,manifest=payload.install(BUNDLE,root,events.append)
            for row in manifest['files']:self.assertEqual(sha256(path/row['path']),row['sha256'])
            (path/'lib/libnoe.so').write_bytes(b'broken')
            (path/'lib/libaipu_driver.so').unlink()
            again,_=payload.install(BUNDLE,root,events.append)
            self.assertEqual(again,path)
            self.assertTrue(any(p.read_bytes()==b'broken' for p in (root/'private/npu-runtime-backups').iterdir()))
            for row in manifest['files']:self.assertEqual(sha256(path/row['path']),row['sha256'])
            self.assertIn('补齐 1 个文件，修复 1 个文件',events[-1])

    def test_existing_symlink_cannot_redirect_library_repair(self):
        with tempfile.TemporaryDirectory() as folder,mock.patch('npu_payload.directory',side_effect=mkdir):
            root=Path(folder);path,_=payload.install(BUNDLE,root)
            outside=root/'outside';outside.write_bytes(b'keep')
            p=path/'lib/libnoe.so';p.unlink();p.symlink_to(outside)
            with self.assertRaisesRegex(RuntimeError,'路径不可信'):payload.install(BUNDLE,root)
            self.assertEqual(outside.read_bytes(),b'keep')

    def test_missing_host_sdk_automatically_uses_bundle_without_dpkg_or_pip(self):
        with tempfile.TemporaryDirectory() as folder,mock.patch('npu_runtime.trusted',side_effect=Path),mock.patch('npu_runtime.subprocess.check_output',side_effect=AssertionError('must not query host packages')):
            installer,source,target,prepared=self.fixture(Path(folder))
            with mock.patch('npu_runtime.probe',side_effect=[result(1),result(0)]) as probe:
                runtime.configure_component(installer,'kokoro',prepared)
            record=json.loads(runtime.record_file(installer.root,'kokoro').read_text())
            self.assertEqual(record['runtime_release_id'],payload.RELEASE_ID)
            self.assertEqual(record['actual_npu_inference'],'passed')
            self.assertEqual(source.read_text(),BASE)
            self.assertIn('/host-runtime/releases/',target.read_text())
            self.assertEqual(probe.call_args_list[0].args[2],'/models/bert2.cix')

    def test_incompatible_existing_runtime_migration_recovers_after_interrupted_apply(self):
        with tempfile.TemporaryDirectory() as folder,mock.patch('npu_runtime.trusted',side_effect=Path):
            installer,source,target,prepared=self.fixture(Path(folder));old,_=self.old_record(installer,source,target)
            original_live=target.read_bytes();original_generated=old.read_bytes()
            with mock.patch('npu_runtime.probe',return_value=result(0)),mock.patch('npu_runtime.apply_record',side_effect=RuntimeError('simulated power interruption')):
                with self.assertRaisesRegex(RuntimeError,'interruption'):runtime.configure_component(installer,'kokoro',prepared)
            self.assertEqual(target.read_bytes(),original_live)
            self.assertEqual(old.read_bytes(),original_generated)
            runtime.apply_record(installer.resources,installer.root,'kokoro',target,installer.home)
            record=json.loads(runtime.record_file(installer.root,'kokoro').read_text())
            self.assertEqual(target.read_bytes(),Path(record['compose_path']).read_bytes())
            self.assertEqual(Path(record['previous_compose']['path']).read_bytes(),original_live)
            self.assertEqual(source.read_text(),BASE)

    def test_compatible_native_image_is_reused_and_total_failure_does_not_change_config(self):
        for codes,success in [([result(0)],True),([result(1),result(1)],False)]:
            with self.subTest(success=success),tempfile.TemporaryDirectory() as folder:
                installer,source,target,prepared=self.fixture(Path(folder))
                with mock.patch('npu_runtime.probe',side_effect=codes):
                    if success:runtime.configure_component(installer,'kokoro',prepared)
                    else:
                        with self.assertRaisesRegex(RuntimeError,'实际 NPU 推理仍失败'):runtime.configure_component(installer,'kokoro',prepared)
                self.assertEqual(target.read_bytes(),source.read_bytes())
                self.assertFalse(runtime.record_file(installer.root,'kokoro').exists())

    def test_probe_requires_real_inference_marker_and_cleans_up_on_timeout(self):
        with mock.patch('npu_runtime.subprocess.run',side_effect=[result(0),result(0)]) as run:
            self.assertNotEqual(runtime.probe(model='/models/test.cix').returncode,0)
            self.assertEqual(run.call_args_list[-1].args[0][:3],['docker','rm','-f'])
        with mock.patch('npu_runtime.subprocess.run',side_effect=[subprocess.TimeoutExpired('docker',90),result(0)]) as run:
            self.assertNotEqual(runtime.probe(model='/models/test.cix').returncode,0)
            self.assertEqual(run.call_args_list[-1].args[0][:3],['docker','rm','-f'])

    def test_initial_install_without_npu_models_prepares_later_modeldeck_activation(self):
        with tempfile.TemporaryDirectory() as folder,mock.patch('npu_runtime.trusted',side_effect=Path):
            installer,source,target,prepared=self.fixture(Path(folder));installer.selected=['copaw-flash-9b']
            with mock.patch('npu_runtime.probe',side_effect=[result(1),result(0)]) as probe:
                runtime.configure_component(installer,'kokoro',prepared)
            record=json.loads(runtime.record_file(installer.root,'kokoro').read_text())
            self.assertEqual(record['actual_npu_inference'],'deferred-until-model-installation')
            self.assertIsNone(probe.call_args_list[0].args[2])
            self.assertIn('/host-runtime/releases/',target.read_text())
            self.assertFalse((installer.home/'kokoro-cix/model').exists())


if __name__=='__main__':unittest.main(verbosity=2)
