import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import runtime_overlay

BUNDLE=Path(__file__).resolve().parents[2]


class RuntimeOverlayTests(unittest.TestCase):
    def test_real_signature_and_base_hashes_are_valid(self):
        manifest=runtime_overlay.load(BUNDLE)
        self.assertEqual(manifest['release_id'],runtime_overlay.RELEASE_ID)
        self.assertEqual({r['path'] for r in manifest['files']},runtime_overlay.MEMBERS)

    def test_tampered_overlay_fails_even_when_base_catalog_is_valid(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            shutil.copytree(BUNDLE/'catalog',root/'catalog')
            shutil.copytree(BUNDLE/runtime_overlay.DIRECTORY,root/runtime_overlay.DIRECTORY)
            shutil.copy2(BUNDLE/'release.json',root/'release.json')
            file=root/runtime_overlay.DIRECTORY/'files/gateway-admin/host_ops/runtime.py'
            file.write_bytes(file.read_bytes()+b'\n# modified\n')
            with self.assertRaisesRegex(RuntimeError,'内容校验'):runtime_overlay.load(root)

    def test_overlay_requires_exact_original_source(self):
        with tempfile.TemporaryDirectory() as folder:
            original=Path(folder)/'original.py';original.write_bytes(b'wrong-base')
            with self.assertRaisesRegex(RuntimeError,'基础文件不同'):
                runtime_overlay.effective_source(BUNDLE,'gateway-admin/host_ops/runtime.py',original)

    def test_unrelated_sources_are_not_overlaid(self):
        original=Path('/fixture/unchanged')
        self.assertEqual(runtime_overlay.effective_source(BUNDLE,'unrelated.py',original),original)
