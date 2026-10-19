"""Execute isolated imports from the exact private worker copy, not the source tree."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import common,service_entry
BUNDLE=Path(__file__).resolve().parents[2]


class WorkerStagingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.bundle=self.root/'bundle';self.installer=self.bundle/'installer'
        self.installer.mkdir(parents=True);self.state=self.root/'state';self.code=self.state/'private/code'
        self.code.parent.mkdir(parents=True)
        patch=mock.patch.object(service_entry,'STATE_ROOT',self.state);patch.start();self.addCleanup(patch.stop)
        self.files={'engine.py':'def lazy_check():\n from container_storage import VALUE\n return VALUE\n',
            'verify_selected.py':'from common import VALUE\n','common.py':'from version import VALUE\n',
            'version.py':'VALUE=7\n','container_storage.py':'from docker_runtime import VALUE\n',
            'docker_runtime.py':'VALUE=19\n'}

    def seal(self,files=None):
        for name,text in (self.files if files is None else files).items():(self.installer/name).write_text(text)
        common.atomic_json(self.bundle/'release.json',{'source_commit':'fixture','installer_version':'1.3.0'})
        paths=[*sorted(self.installer.glob('*.py')),self.bundle/'release.json']
        common.atomic_json(self.bundle/'installer-manifest.json',{'format':1,'files':[
            {'path':p.relative_to(self.bundle).as_posix(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]})

    def stage(self):service_entry.stage_worker(self.bundle,self.code)

    def test_lazy_dependencies_import_from_private_code_without_source_or_pythonpath(self):
        self.seal();self.stage()
        result=subprocess.run(['/usr/bin/python3','-I','-B','-c',
            'import sys;sys.path.insert(0,sys.argv[1]);import engine;assert engine.lazy_check()==19;print(engine.__file__)',str(self.code)],
            cwd=self.root,capture_output=True,text=True,env=dict(os.environ,PYTHONPATH=str(self.installer)))
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),str(self.code/'engine.py'))
        self.assertIn('container_storage',common.read_json(self.code/'worker-manifest.json')['checked_imports'])

    def test_new_sealed_module_is_staged_without_editing_a_copy_list(self):
        self.files['future_backend.py']='VALUE=23\n'
        self.files['engine.py']+='def future():\n from future_backend import VALUE\n return VALUE\n'
        self.seal();self.stage()
        self.assertTrue((self.code/'future_backend.py').is_file())
        self.assertIn('future_backend',common.read_json(self.code/'worker-manifest.json')['checked_imports'])

    def test_unsealed_lazy_local_dependency_is_rejected_before_replacing_old_code(self):
        self.seal();self.code.mkdir();(self.code/'original').write_text('keep')
        manifest=common.read_json(self.bundle/'installer-manifest.json')
        manifest['files']=[r for r in manifest['files'] if r['path']!='installer/container_storage.py']
        common.atomic_json(self.bundle/'installer-manifest.json',manifest)
        with self.assertRaisesRegex(RuntimeError,'完整性清单'):self.stage()
        self.assertEqual((self.code/'original').read_text(),'keep')

    def test_import_failure_preserves_old_worker_and_removes_partial_directory(self):
        self.files['container_storage.py']='raise ImportError("fixture_dependency_failure")\n'
        self.seal();self.code.mkdir();(self.code/'original').write_text('keep')
        with self.assertRaisesRegex(RuntimeError,'后台模块依赖自检失败'):self.stage()
        self.assertEqual((self.code/'original').read_text(),'keep')
        self.assertEqual(sorted(p.name for p in self.code.parent.iterdir()),['code'])

    def test_fresh_publish_excludes_stale_and_unsealed_python_files(self):
        self.seal();self.code.mkdir();(self.code/'stale.py').write_text('raise RuntimeError("stale")')
        (self.installer/'unsealed.py').write_text('raise RuntimeError("unsealed")')
        self.stage()
        self.assertFalse((self.code/'stale.py').exists());self.assertFalse((self.code/'unsealed.py').exists())

    def test_copied_checksum_is_verified_before_old_worker_is_replaced(self):
        self.seal();self.code.mkdir();(self.code/'original').write_text('keep')
        (self.installer/'engine.py').write_text('changed after verification')
        with self.assertRaisesRegex(RuntimeError,'复制校验失败'):self.stage()
        self.assertEqual((self.code/'original').read_text(),'keep')

    def test_all_real_backend_dependencies_import_from_the_staged_tree(self):
        files={p.name:p.read_text('utf-8') for p in (BUNDLE/'installer').glob('*.py')}
        self.seal(files);self.stage()
        proof=common.read_json(self.code/'worker-manifest.json')
        self.assertIn('container_storage',proof['checked_imports'])
        self.assertIn('yiqiban',proof['checked_imports'])
        self.assertIn('yiqiban_verify',proof['checked_imports'])
        self.assertIn('yiqiban_source',proof['checked_imports'])
        self.assertNotIn('wizard',proof['checked_imports'])
        self.assertEqual(len(list(self.code.glob('*.py'))),len(files))


if __name__=='__main__':unittest.main(verbosity=2)
