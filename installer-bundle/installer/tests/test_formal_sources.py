"""Hardware routing and release safety contracts for the formal OCI sources."""
import copy,json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from artifacts import load_catalog,source_config,oci_source,remote_candidates,plan_sources,SourceError
from selection import selected_catalog,supported_on
BUNDLE=Path(__file__).resolve().parents[2]

class FormalSourcesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog=load_catalog(BUNDLE);cls.config=source_config(BUNDLE)

    def test_single_dual_selects_both_correct_gguf_layers(self):
        for count,repo in [(1,'aistore/qwen3-8-27b'),(2,'aistore/qwen3-8-27b-2')]:
            rows=selected_catalog(self.catalog,['qwen3.8-27b'],count)['resources']
            rows=[r for r in rows if r['category']=='models']
            self.assertEqual(len(rows),2)
            for r in rows:
                s=oci_source(self.config,r)[1]
                self.assertEqual(s['repository'],repo);self.assertEqual(s['reference'],'v1')
                self.assertEqual(s['blob_digest'],'sha256:'+r['sha256'])
                self.assertEqual(s['blob_size'],r['size'])
                self.assertEqual(remote_candidates(self.config,r),[])
            self.assertTrue(any('/mmproj-' in r['path'] for r in rows))

    def test_qwen36_dual_only_with_fixed_two_files(self):
        with self.assertRaisesRegex(ValueError,'不支持'):selected_catalog(self.catalog,['qwen3.6-35b-a3b'],1)
        self.assertFalse(supported_on(self.catalog,'qwen3.6-35b-a3b',None))
        rows=[r for r in selected_catalog(self.catalog,['qwen3.6-35b-a3b'],2)['resources'] if r['category']=='models']
        self.assertEqual(len(rows),2)
        self.assertEqual({oci_source(self.config,r)[1]['repository'] for r in rows},{'aistore/qwen3.6-35b-a3b-2'})

    def test_plan_retains_card_count_for_remote_preflight(self):
        subset=selected_catalog(self.catalog,['qwen3.8-27b'],1)
        with tempfile.TemporaryDirectory() as cache:
            plan=plan_sources(BUNDLE,subset,self.config,cache)
        for r in plan['rows']:
            if r['category']=='models':self.assertEqual(oci_source(self.config,r)[1]['repository'],'aistore/qwen3-8-27b')

    def test_ambiguous_device_sources_and_missing_count_rejected(self):
        config=copy.deepcopy(self.config)
        config['oci_artifacts']['duplicate']=copy.deepcopy(config['oci_artifacts']['qwen38-1-mmproj'])
        with self.assertRaises(SourceError):source_config(BUNDLE,config)
        with self.assertRaises(ValueError):selected_catalog(self.catalog,['qwen3.8-27b'],True)
        with self.assertRaises(ValueError):selected_catalog(self.catalog,['qwen3.8-27b'],3)

    def test_local_selection_does_not_fallback_to_network(self):
        config=dict(self.config,model_source='local')
        for r in selected_catalog(self.catalog,['qwen3.8-27b'],2)['resources']:
            if r['category']=='models':
                self.assertIsNone(oci_source(config,r));self.assertEqual(remote_candidates(config,r),[])

if __name__=='__main__':unittest.main()
