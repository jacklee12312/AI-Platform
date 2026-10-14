"""Contracts for Qwen3.6 and the shared Harbor downloader; no host lifecycle changes."""
import copy,hashlib,io,json,sys,tempfile,unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent.model_store import Catalog,ModelStore
from agent import device_recovery,model_runtime,runtime_probes,main
from admin_app.main import DEVICE_MODES

class FormalReleaseTests(unittest.TestCase):
    def test_all_lifecycle_adapters_register_qwen36(self):
        c=Catalog();m=c.models['qwen3.6-35b-a3b']
        self.assertEqual(m['port'],17712);self.assertEqual(m['modes'],['qwen36'])
        self.assertEqual(main.MODE_SERVICES['qwen36'],{'qwen36'})
        self.assertIn('qwen36',DEVICE_MODES)
        self.assertEqual(device_recovery.MODE_CONTAINERS['qwen36'],m['container'])
        port,path,body,kind=runtime_probes.specification(m['container'])
        self.assertEqual(port,17712);self.assertEqual(kind,'digit7')
        self.assertEqual(body['model'],m['id']);self.assertIsInstance(body['messages'][0]['content'],list)

    def test_qwen_multimodal_files_use_two_locked_oci_layers(self):
        c=Catalog()
        for mid,repo in [('qwen3.8-27b','aistore/qwen3-8-27b-2'),('qwen3.6-35b-a3b','aistore/qwen3.6-35b-a3b-2')]:
            rows=c.models[mid]['files'];self.assertEqual(len(rows),2)
            for item in rows:
                s=item['oci_source'];self.assertEqual(s['repository'],repo)
                self.assertEqual(s['blob_digest'],'sha256:'+item['sha256'])
                self.assertEqual(s['blob_size'],item['size'])
                self.assertNotIn('url',item)

    def test_model_manager_uses_scoped_registry_client_and_resumes(self):
        payload=b'test-gguf-content';item=copy.deepcopy(Catalog().models['qwen3.6-35b-a3b']['files'][0])
        item.update(sha256=hashlib.sha256(payload).hexdigest(),size=len(payload))
        item['oci_source'].update(blob_digest='sha256:'+item['sha256'],blob_size=len(payload))
        class Response(io.BytesIO):
            status=206;headers={'Content-Range':f'bytes 4-{len(payload)-1}/{len(payload)}'}
        with tempfile.TemporaryDirectory() as folder:
            store=ModelStore(folder);store.cached(item).with_suffix('.part').write_bytes(payload[:4])
            with mock.patch('agent.registry.RegistryClient') as client:
                client.return_value.blob_url.return_value='http://36.111.34.121:15436/v2/aistore/test/blobs/sha256:'+item['sha256']
                client.return_value.open.return_value=Response(payload[4:])
                result=store.download(item)
                client.return_value.verify_manifest.assert_called_once()
                self.assertEqual(client.return_value.open.call_args.args[0].get_header('Range'),'bytes=4-')
            self.assertEqual(result.read_bytes(),payload)

    def test_incompatible_card_is_rejected_by_model_manager(self):
        c=Catalog();m=c.models['qwen3.6-35b-a3b']
        with tempfile.TemporaryDirectory() as folder:
            runtime=model_runtime.ModelRuntime(folder,c)
            result=mock.Mock(stdout=json.dumps({'Architecture':'arm64','OSType':'linux','MemTotal':64*1024**3}),returncode=0)
            with mock.patch.object(runtime,'run',return_value=result),mock.patch.object(runtime,'image_ready',return_value=True),mock.patch.object(runtime,'inspect',return_value=None),mock.patch.object(runtime,'assert_safe'),mock.patch.object(Path,'is_char_device',return_value=False):
                errors,_=runtime.preflight([m],True)
            self.assertTrue(any('需要双卡设备' in e for e in errors))

if __name__=='__main__':unittest.main()
