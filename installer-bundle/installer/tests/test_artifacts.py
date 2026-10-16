"""Real local HTTP/filesystem tests. No project containers or model inference are started."""
import hashlib,json,os,shutil,socket,sys,tempfile,threading,unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import artifacts
from common import atomic_json
BUNDLE=Path(__file__).resolve().parents[2]

def row(data,path='payload/home/cix/models/test.bin'):
    return {'path':path,'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'category':'models','models':['fixture']}

class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.data=b'0123456789abcdef'*16384
        self.item=row(self.data);self.requests=[];self.behavior='range';outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                range_header=self.headers.get('Range');outer.requests.append(range_header)
                if outer.behavior=='404':self.send_error(404);return
                offset=int(range_header[6:-1]) if range_header else 0
                if outer.behavior=='ignore':offset=0
                content=outer.data[offset:]
                if outer.behavior=='corrupt':content=b'X'*len(content)
                if outer.behavior=='oversize':content+=b'XX'
                if range_header and outer.behavior!='ignore':
                    self.send_response(206)
                    start=offset+1 if outer.behavior=='bad-range' else offset
                    self.send_header('Content-Range',f'bytes {start}-{len(outer.data)-1}/{len(outer.data)}')
                else:self.send_response(200)
                self.send_header('Content-Length',str(len(content)));self.end_headers()
                if outer.behavior=='disconnect' and len(outer.requests)==1:
                    self.wfile.write(content[:65536]);self.wfile.flush();self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
                self.wfile.write(content)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}/model?token=do-not-log-this'
        self.config={'schema':1,'mode':'prefer-local','local_roots':[],'base_urls':{},'file_urls':{self.item['path']:[self.url]},'allow_http':True,'timeout_seconds':2,'retries':1}
        self.cache=self.root/'cache';self.store=artifacts.BlobStore(self.root,self.config,self.cache)
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def partial(self,data):
        p=artifacts.blob_path(self.cache,self.item).with_suffix('.part');p.parent.mkdir(parents=True);p.write_bytes(data);return p
    def test_real_download_and_cached_reuse(self):
        p=self.store.ensure(self.item);self.assertEqual(p.read_bytes(),self.data)
        self.store.ensure(self.item);self.assertEqual(len(self.requests),1)
    def test_legacy_cache_reused_after_hash_check_without_downloading_again(self):
        old=self.root/'old-cache';p=artifacts.blob_path(old,self.item);p.parent.mkdir(parents=True);p.write_bytes(self.data)
        with mock.patch('artifacts.LEGACY_CACHE',old):
            plan=artifacts.plan_sources(self.root,{'resources':[self.item]},self.config,self.cache)
            self.assertEqual(plan['bytes']['local'],len(self.data))
            result=self.store.ensure(self.item)
        self.assertEqual(result.read_bytes(),self.data);self.assertEqual(self.requests,[]);self.assertTrue(p.exists())
    def test_corrupt_legacy_cache_cannot_bypass_sha256_validation(self):
        old=self.root/'old-cache';p=artifacts.blob_path(old,self.item);p.parent.mkdir(parents=True);p.write_bytes(b'X'*len(self.data))
        with mock.patch('artifacts.LEGACY_CACHE',old):result=self.store.ensure(self.item)
        self.assertEqual(result.read_bytes(),self.data);self.assertEqual(len(self.requests),1)
    def test_range_resume(self):
        self.partial(self.data[:100]);p=self.store.ensure(self.item)
        self.assertEqual(self.requests,['bytes=100-']);self.assertEqual(p.read_bytes(),self.data)
    def test_server_ignores_range_restart_is_clean(self):
        self.behavior='ignore';self.partial(self.data[:100]);p=self.store.ensure(self.item)
        self.assertEqual(p.read_bytes(),self.data)
    def test_disconnect_keeps_partial_and_second_run_resumes(self):
        self.behavior='disconnect'
        with self.assertRaises(artifacts.SourceError):self.store.ensure(self.item)
        part=artifacts.blob_path(self.cache,self.item).with_suffix('.part');self.assertGreater(part.stat().st_size,0)
        p=self.store.ensure(self.item);self.assertEqual(p.read_bytes(),self.data);self.assertTrue(self.requests[1].startswith('bytes='))
    def test_invalid_range_rejected(self):
        self.behavior='bad-range';part=self.partial(self.data[:100])
        with self.assertRaises(artifacts.SourceError):self.store.ensure(self.item)
        self.assertEqual(part.read_bytes(),self.data[:100]);self.assertFalse(artifacts.blob_path(self.cache,self.item).exists())
    def test_corrupt_download_not_published(self):
        self.behavior='corrupt'
        with self.assertRaises(artifacts.SourceError):self.store.ensure(self.item)
        self.assertFalse(artifacts.blob_path(self.cache,self.item).exists())
    def test_oversized_download_rejected(self):
        self.behavior='oversize'
        with self.assertRaises(artifacts.SourceError):self.store.ensure(self.item)
        self.assertFalse(artifacts.blob_path(self.cache,self.item).exists())
    def test_error_does_not_expose_url_token(self):
        self.behavior='404'
        with self.assertRaises(artifacts.SourceError) as caught:self.store.ensure(self.item)
        self.assertNotIn('do-not-log-this',str(caught.exception));self.assertNotIn('127.0.0.1',str(caught.exception))
    def test_blank_urls_fail_before_any_staging(self):
        config=dict(self.config,file_urls={});dest=self.root/'resources'
        with self.assertRaises(artifacts.SourceError):artifacts.materialize(self.root,{'resources':[self.item]},config,dest,self.cache)
        self.assertFalse(dest.exists());self.assertFalse(self.cache.exists());self.assertFalse(self.requests)
    def test_local_source_and_hardlinked_staging(self):
        p=self.root/'local/files'/self.item['path'];p.parent.mkdir(parents=True);p.write_bytes(self.data)
        config=dict(self.config,mode='local-only',local_roots=[str(self.root/'local')]);dest=self.root/'staged'
        artifacts.materialize(self.root,{'resources':[self.item]},config,dest,self.cache)
        self.assertEqual((dest/self.item['path']).read_bytes(),self.data);self.assertFalse(self.requests)
        self.assertEqual((dest/self.item['path']).stat().st_ino,artifacts.blob_path(self.cache,self.item).stat().st_ino)
    def test_bad_local_source_falls_back_to_remote(self):
        p=self.root/'local/files'/self.item['path'];p.parent.mkdir(parents=True);p.write_bytes(b'X'*len(self.data))
        config=dict(self.config,local_roots=[str(self.root/'local')]);store=artifacts.BlobStore(self.root,config,self.cache)
        self.assertEqual(store.ensure(self.item).read_bytes(),self.data);self.assertEqual(len(self.requests),1)
    def test_pause_preserves_partial_without_publishing(self):
        class Pause(Exception):pass
        def progress(item,n,phase):
            if phase=='下载':raise Pause('pause')
        store=artifacts.BlobStore(self.root,self.config,self.cache,progress)
        with self.assertRaises(Pause):store.ensure(self.item)
        self.assertTrue(artifacts.blob_path(self.cache,self.item).with_suffix('.part').exists())
        self.assertFalse(artifacts.blob_path(self.cache,self.item).exists())
    def test_partial_symlink_rejected(self):
        external=self.root/'external';external.write_bytes(b'preserve')
        part=artifacts.blob_path(self.cache,self.item).with_suffix('.part');part.parent.mkdir(parents=True);part.symlink_to(external)
        with self.assertRaises(artifacts.SourceError):self.store.ensure(self.item)
        self.assertEqual(external.read_bytes(),b'preserve')
    def test_http_requires_explicit_configuration(self):
        with self.assertRaises(artifacts.SourceError):artifacts.source_config(self.root,dict(self.config,allow_http=False))
    def test_no_project_tree_created_if_last_resource_fails(self):
        bad=row(b'missing','payload/second.bin');config=dict(self.config,file_urls={self.item['path']:[self.url],bad['path']:[self.url]})
        dest=self.root/'staged'
        with self.assertRaises(artifacts.SourceError):artifacts.materialize(self.root,{'resources':[self.item,bad]},config,dest,self.cache)
        self.assertFalse(dest.exists());self.assertTrue(artifacts.blob_path(self.cache,self.item).exists())

class CatalogTests(unittest.TestCase):
    def test_online_worker_keeps_bundled_auxiliary_files_available(self):
        catalog=artifacts.load_catalog(BUNDLE)
        config=artifacts.source_config(BUNDLE)
        config['local_roots']=artifacts.worker_source_roots(BUNDLE,config)
        self.assertEqual(config['local_roots'][0],'@bundle/support-files')
        self.assertTrue(all(Path(root).is_absolute() for root in config['local_roots'][1:]))
        auxiliary=[row for row in catalog['resources'] if row['path'] in {
            'payload/home/cix/cix_telecom_projects/sensevoice/model/test_wavs/zh.wav',
        }]
        self.assertEqual(len(auxiliary),1)
        with tempfile.TemporaryDirectory() as directory:
            plan=artifacts.plan_sources(BUNDLE,{'resources':auxiliary},config,Path(directory))
        self.assertTrue(plan['ready'])
        self.assertEqual([row['source'] for row in plan['rows']],['local']*1)

    def test_actual_catalog_signed_and_harbor_sources_configured(self):
        c=artifacts.load_catalog(BUNDLE);config=artifacts.source_config(BUNDLE)
        self.assertEqual(len(c['models']),25);self.assertEqual(c['image_count'],10)
        self.assertEqual(len([r for r in c['resources'] if r['category']=='models']),56)
        fixture=next(r for r in c['resources'] if r['path'].endswith('/sensevoice/model/test_wavs/zh.wav'))
        self.assertEqual(fixture['category'],'platform')
        self.assertEqual(fixture['sha256'],'b77f1794fe374a0ba1ee1dc458bfaf9349496cbbfc32780c50ba3c5a7ad8e373')
        self.assertFalse(any(config['base_urls'].values()))
        qwen_urls={path:urls for path,urls in config['file_urls'].items() if 'qwen3.8-27b/' in path}
        self.assertEqual(len(qwen_urls),0)
        self.assertTrue(all(len(urls)==1 and urls[0].startswith('https://llmoss.houmoai.com/') for urls in qwen_urls.values()))
        self.assertEqual(len(config['oci_artifacts']),29)
        remote={p for s in config['oci_artifacts'].values() for p in s['files']}
        self.assertEqual(len(remote),56)
        self.assertIn('cosyvoice3-0.5b',config['oci_artifacts'])
        self.assertIn('spark-x1-4b',config['oci_artifacts'])
        self.assertTrue(config['oci_artifacts']['spark-x1-4b']['manifest_digest'].startswith('sha256:'))
        self.assertFalse((BUNDLE/'resources').exists())
    def test_tampered_signature_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);shutil.copytree(BUNDLE/'catalog',p/'catalog');shutil.copyfile(BUNDLE/'release.json',p/'release.json')
            (p/'catalog/release-catalog.sig').write_bytes(b'\x00'*64)
            with self.assertRaises(artifacts.SourceError):artifacts.load_catalog(p)
    def test_manifest_mutation_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);shutil.copytree(BUNDLE/'catalog',p/'catalog');shutil.copyfile(BUNDLE/'release.json',p/'release.json')
            with (p/'catalog/release-catalog.json').open('a') as f:f.write(' ')
            with self.assertRaises(artifacts.SourceError):artifacts.load_catalog(p)
    def test_routing_edits_do_not_change_signed_catalog(self):
        config=artifacts.source_config(BUNDLE);config['mode']='remote-only';config['model_source']='online'
        config['base_urls']['models']=['https://example.invalid/repository']
        artifacts.source_config(BUNDLE,config);c=artifacts.load_catalog(BUNDLE)
        self.assertTrue(artifacts.remote_candidates(config,next(r for r in c['resources'] if r['category']=='models')))
    def test_standalone_external_configuration_is_used(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'installer.sources.json';config=artifacts.source_config(BUNDLE);config['mode']='remote-only';atomic_json(p,config)
            with mock.patch.dict(os.environ,AI_PLATFORM_SOURCES_FILE=str(p)):
                self.assertEqual(artifacts.source_config(BUNDLE)['mode'],'remote-only')
    def test_standalone_media_relative_sources_resolve(self):
        with tempfile.TemporaryDirectory() as d:
            config=artifacts.source_config(BUNDLE);config['local_roots']=['../resource-store']
            with mock.patch.dict(os.environ,AI_PLATFORM_SOURCE_BASE=str(Path(d)/'installer-source-base')):
                self.assertEqual(artifacts.source_roots(BUNDLE,config),(Path(d)/'resource-store',))

if __name__=='__main__':unittest.main(verbosity=2)
