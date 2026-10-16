"""Host driver rendering, resume provenance and exact image reuse budgets."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import npu_runtime
from common import atomic_json,sha256
from engine import Installer


class NpuRuntimeTests(unittest.TestCase):
    def test_render_preserves_device_model_and_image_and_adds_readonly_host_runtime(self):
        baseline='services:\n  kokoro-tts:\n    image: kokoro-cix:0.2.0\n    volumes:\n    - ./model:/models:ro\n    devices:\n    - /dev/aipu:/dev/aipu\n    environment:\n      HF_HUB_OFFLINE: "1"\n'
        result=npu_runtime.render(baseline,Path('/var/lib/fixture/host-runtime/kokoro'))
        self.assertIn('image: kokoro-cix:0.2.0',result)
        self.assertIn('./model:/models:ro',result)
        self.assertIn('/dev/aipu:/dev/aipu',result)
        self.assertIn('AIPU_LIB_PATH: /opt/cix-host-lib/libaipu_driver.so',result)
        self.assertIn('/site-packages/libnoe:ro',result)
        self.assertNotIn('privileged',result)
        with self.assertRaises(RuntimeError):npu_runtime.render('unexpected',Path('/fixture'))

    def test_repair_recovery_only_replaces_exact_original_and_preserves_signed_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);original=root/'source';generated=root/'generated';target=root/'live'
            original.write_text('original');generated.write_text('local');target.write_text('original')
            npu_runtime.apply_local(original,generated,target,root)
            self.assertEqual(target.read_text(),'local');self.assertEqual(original.read_text(),'original')
            npu_runtime.apply_local(original,generated,target,root)
            target.write_text('operator configuration')
            with self.assertRaises(RuntimeError):npu_runtime.apply_local(original,generated,target,root)
            target.unlink();target.symlink_to(original)
            with self.assertRaises(RuntimeError):npu_runtime.apply_local(original,generated,target,root)

    def test_resume_rejects_changed_baseline_library_or_rendered_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);resources=root/'resources';state=root/'state'
            original=resources/npu_runtime.RELATIVE;original.parent.mkdir(parents=True);original.write_text('base')
            generated=state/'private/local-configs/kokoro-compose.yml';generated.parent.mkdir(parents=True);generated.write_text('local')
            library=state/'host-runtime/kokoro/lib/test.so';library.parent.mkdir(parents=True);library.write_bytes(b'library')
            record={'base_sha256':sha256(original),'compose_sha256':sha256(generated),'files':{str(library):sha256(library)}}
            atomic_json(state/'private/kokoro-runtime.json',record)
            # Tempfile parents are writable test directories; trust is checked separately.
            with mock.patch('npu_runtime.trusted',side_effect=lambda p:Path(p)):
                self.assertEqual(npu_runtime.effective_compose(resources,state),generated)
                library.write_bytes(b'tampered')
                with self.assertRaises(RuntimeError):npu_runtime.effective_compose(resources,state)
                library.write_bytes(b'library');original.write_text('different-release')
                with self.assertRaises(RuntimeError):npu_runtime.effective_compose(resources,state)

    def test_budget_requires_every_pinned_image_before_reducing_reserve(self):
        obj=Installer.__new__(Installer);obj.manifest={'docker_disk_budget_bytes':32*1024**3,'images':[{'tag':'a:1','id':'sha256:a','config_id':'sha256:a'},{'tag':'b:1','id':'sha256:b','config_id':'sha256:b'}]}
        with mock.patch('engine.shutil.which',return_value='/usr/bin/docker'),mock.patch('engine.subprocess.run') as run:
            run.return_value=SimpleNamespace(returncode=0,stdout=json.dumps([{'Id':'sha256:a'},{'Id':'sha256:b'}]))
            self.assertEqual(obj.docker_space_budget(),2*1024**3)
            run.return_value=SimpleNamespace(returncode=0,stdout=json.dumps([{'Id':'sha256:a'},{'Id':'sha256:other'}]))
            self.assertEqual(obj.docker_space_budget(),32*1024**3)
            run.return_value=SimpleNamespace(returncode=1,stdout='')
            self.assertEqual(obj.docker_space_budget(),32*1024**3)


if __name__=='__main__':unittest.main(verbosity=2)
