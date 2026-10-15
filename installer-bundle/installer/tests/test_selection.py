"""Signed-parent selection must not leak unselected model weights into the plan."""
import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from artifacts import load_catalog
from selection import selected_catalog,validate_selected

BUNDLE=Path(__file__).resolve().parents[2]


class SelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog=load_catalog(BUNDLE)
    def test_one_model_keeps_platform_and_only_its_weight(self):
        subset=selected_catalog(self.catalog,['copaw-flash-9b'],1)
        self.assertEqual(subset['models'],['copaw-flash-9b'])
        self.assertEqual(len([r for r in subset['resources'] if r['category']=='models']),1)
        self.assertEqual(len([r for r in subset['resources'] if r['category']!='models']),
                         len([r for r in self.catalog['resources'] if r['category']!='models']))
        self.assertEqual(len(self.catalog['models']),25)  # Parent is immutable.
    def test_cosyvoice_keeps_only_shared_test_audio(self):
        subset=selected_catalog(self.catalog,['cosyvoice3-0.5b'],2)
        rows=[r for r in subset['resources'] if r['category']=='models']
        self.assertEqual(len(rows),2)
        self.assertTrue(any(r['path'].endswith('/sensevoice/model/test_wavs/zh.wav') for r in rows))
        self.assertFalse(any('/sensevoice/model/' in r['path'] and not r['path'].endswith('/test_wavs/zh.wav') for r in rows))
    def test_unknown_duplicate_and_cpu_only_rejected(self):
        for values in [['not-in-release'],['copaw-flash-9b','copaw-flash-9b'],['sensevoice'],[]]:
            with self.subTest(values=values),self.assertRaises(ValueError):validate_selected(self.catalog,values)

    def test_qwen38_selects_exactly_one_hardware_variant(self):
        for count,chosen,other in [(1,'1d-xh2','2d-xh2'),(2,'2d-xh2','1d-xh2')]:
            with self.subTest(device_count=count):
                subset=selected_catalog(self.catalog,['qwen3.8-27b'],count)
                paths=[row['path'] for row in subset['resources'] if row['category']=='models']
                self.assertTrue(any(chosen in path for path in paths))
                self.assertFalse(any(other in path for path in paths))
                self.assertEqual(len(paths),2)
                self.assertTrue(any('mmproj-Qwen3.8-27b' in path for path in paths))
                self.assertTrue(any('20260911' in path for path in paths))
        with self.assertRaises(ValueError):selected_catalog(self.catalog,['qwen3.8-27b'],0)


if __name__=='__main__':unittest.main(verbosity=2)
