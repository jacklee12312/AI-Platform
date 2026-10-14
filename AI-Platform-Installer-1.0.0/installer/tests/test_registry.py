"""OCI transfer tests use a local HTTP registry, no external services or project deployment."""
import gzip,hashlib,io,json,os,socket,sys,tarfile,tempfile,threading,unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import artifacts,registry

def resource(data,name):return {'path':'payload/home/cix/models/'+name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'category':'models','models':['fixture']}

class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.data=b'0123456789abcdef'*4096;self.rows=[resource(self.data,'one.bin')]
        self.blob=self.data;self.token_requests=0;self.blob_requests=[];self.request_scopes=[];self.bad_manifest=False;self.foreign_realm=False;self.client=None;outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                if self.path.startswith('/token?'):
                    outer.token_requests+=1;outer.request_scopes.append(self.path)
                    payload=b'{"token":"fixture-token-secret"}';self.send_response(200);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload);return
                if self.headers.get('Authorization')!='Bearer fixture-token-secret':
                    self.send_response(401);realm='http://127.0.0.1:1/no-token' if outer.foreign_realm else outer.base+'/token'
                    self.send_header('WWW-Authenticate',f'Bearer realm="{realm}",service="fixture",scope="repository:untrusted:push"');self.end_headers();return
                if '/manifests/' in self.path:
                    body=outer.manifest+(b' ' if outer.bad_manifest else b'');self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
                if '/blobs/' in self.path:
                    span=self.headers.get('Range');outer.blob_requests.append(span);offset=int(span[6:-1]) if span else 0
                    self.send_response(206 if span else 200)
                    if span:self.send_header('Content-Range',f'bytes {offset}-{len(outer.blob)-1}/{len(outer.blob)}')
                    self.send_header('Content-Length',str(len(outer.blob)-offset));self.end_headers();self.wfile.write(outer.blob[offset:]);return
                self.send_error(404)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.base=f'http://127.0.0.1:{self.server.server_port}'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.configure()
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def configure(self,fmt='blob',files=None):
        digest='sha256:'+hashlib.sha256(self.blob).hexdigest();self.manifest=json.dumps({'schemaVersion':2,'layers':[{'digest':digest,'size':len(self.blob)}]}).encode()
        self.source={'registry':self.base,'repository':'aistore/fixture','reference':'v1','manifest_digest':'sha256:'+hashlib.sha256(self.manifest).hexdigest(),'blob_digest':digest,'blob_size':len(self.blob),'format':fmt,'files':files or {self.rows[0]['path']:None}}
        self.config={'schema':1,'mode':'prefer-local','local_roots':[],'base_urls':{},'file_urls':{},'allow_http':True,'retries':1,'timeout_seconds':2,'oci_artifacts':{'fixture':self.source}}
        self.store=artifacts.BlobStore(self.root,self.config,self.root/'cache',rows=self.rows)
    def archive(self,entries=None):
        self.rows=[resource(b'first','one.bin'),resource(b'second','two.bin')]
        entries=entries if entries is not None else [('pack/a',b'first',None),('pack/b',b'second',None),('pack/extra.txt',b'ignored',None)]
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w:gz') as tar:
            for name,data,kind in entries:
                entry=tarfile.TarInfo(name);entry.size=len(data)
                if kind:entry.type=kind;entry.linkname='../escape'
                tar.addfile(entry,io.BytesIO(data) if entry.isfile() else None)
        self.blob=stream.getvalue();self.configure('tar+gzip',{self.rows[0]['path']:'pack/a',self.rows[1]['path']:'pack/b'})
    def materialize(self):return artifacts.materialize(self.root,{'resources':self.rows},self.config,self.root/'staged',self.root/'cache')
    def test_anonymous_blob_and_scoped_memory_token(self):
        target=self.store.ensure(self.rows[0]);self.assertEqual(target.read_bytes(),self.data);self.assertEqual(self.token_requests,1)
        self.assertIn('repository%3Aaistore%2Ffixture%3Apull',self.request_scopes[0]);self.assertNotIn('fixture-token-secret',json.dumps(self.config))
    def test_blob_range_resume(self):
        part=artifacts.blob_path(self.store.cache,self.rows[0]).with_suffix('.part');part.parent.mkdir(parents=True);part.write_bytes(self.data[:321])
        self.store.ensure(self.rows[0]);self.assertEqual(self.blob_requests,['bytes=321-'])
    def test_cached_blob_avoids_registry(self):
        self.store.ensure(self.rows[0]);self.bad_manifest=True;self.store.ensure(self.rows[0]);self.assertEqual(len(self.blob_requests),1)
    def test_changed_manifest_is_rejected(self):
        self.bad_manifest=True
        with self.assertRaises(artifacts.SourceError):self.store.ensure(self.rows[0])
        self.assertFalse(artifacts.blob_path(self.store.cache,self.rows[0]).exists());self.assertEqual(self.blob_requests,[])
    def test_pending_tag_resolves_after_upload_and_checks_original_digest(self):
        self.source['manifest_digest']=None;self.source['resolve_tag']=True;self.source['reference']='v1'
        artifacts.source_config(self.root,self.config)
        self.assertEqual(self.store.ensure(self.rows[0]).read_bytes(),self.data)
        self.assertEqual(len(self.blob_requests),1)
    def test_pending_tag_rejects_wrong_layer(self):
        self.source['manifest_digest']=None;self.source['resolve_tag']=True;self.source['reference']='v1'
        self.manifest=json.dumps({'schemaVersion':2,'layers':[{'digest':'sha256:'+'0'*64,'size':len(self.blob)}]}).encode()
        with self.assertRaises(artifacts.SourceError):self.store.ensure(self.rows[0])
        self.assertFalse(artifacts.blob_path(self.store.cache,self.rows[0]).exists())
    def test_pending_tag_requires_explicit_opt_in(self):
        self.source['manifest_digest']=None
        with self.assertRaises(artifacts.SourceError):artifacts.source_config(self.root,self.config)
    def test_foreign_token_realm_is_rejected(self):
        self.foreign_realm=True
        with self.assertRaises(artifacts.SourceError):self.store.ensure(self.rows[0])
        self.assertEqual(self.token_requests,0)
    def test_expired_token_is_refreshed(self):
        client=registry.RegistryClient(self.source,2);client.token='expired';client.verify_manifest();self.assertEqual(self.token_requests,1)
    def test_cross_origin_redirect_never_forwards_auth(self):
        handler=registry.SameOriginRedirect();req=registry.request.Request(self.base+'/blob',headers={'Authorization':'Bearer fixture-token-secret'})
        with self.assertRaises(registry.RegistryError):handler.redirect_request(req,None,302,'',{},'http://127.0.0.1:1/external')
    def test_archive_maps_verified_files_once(self):
        self.archive();self.materialize()
        for row,data in zip(self.rows,[b'first',b'second']):self.assertEqual((self.root/'staged'/row['path']).read_bytes(),data)
        self.assertEqual(len(self.blob_requests),1);self.assertFalse((self.root/'staged/pack/extra.txt').exists())
    def test_archive_inner_hash_mismatch_never_deploys(self):
        self.archive([('pack/a',b'wrong',None),('pack/b',b'second',None)])
        with self.assertRaises(artifacts.SourceError):self.materialize()
        self.assertFalse((self.root/'staged').exists())
    def test_archive_missing_member_never_deploys(self):
        self.archive([('pack/a',b'first',None)])
        with self.assertRaises(artifacts.SourceError):self.materialize()
        self.assertFalse((self.root/'staged').exists())
    def test_archive_traversal_is_rejected(self):
        self.archive([('../escape',b'bad',None)])
        with self.assertRaises((RuntimeError,ValueError)):self.materialize()
        self.assertFalse((self.root/'escape').exists());self.assertFalse((self.root/'staged').exists())
    def test_archive_link_is_rejected(self):
        self.archive([('pack/a',b'',tarfile.SYMTYPE)])
        with self.assertRaises(artifacts.SourceError):self.materialize()
    def test_archive_duplicate_is_rejected(self):
        self.archive([('pack/a',b'first',None),('pack/a',b'first',None),('pack/b',b'second',None)])
        with self.assertRaises(artifacts.SourceError):self.materialize()
    def test_archive_budget_counts_compressed_once(self):
        self.archive();p=artifacts.plan_sources(self.root,{'resources':self.rows},self.config,self.store.cache)
        self.assertTrue(p['ready']);self.assertEqual(p['archive_bytes'],len(self.blob));self.assertEqual(p['remote_transfer_bytes'],len(self.blob));self.assertEqual(p['cache_bytes_needed'],len(self.blob)+11)
    def test_local_only_makes_no_registry_requests(self):
        self.config['mode']='local-only';store=artifacts.BlobStore(self.root,self.config,self.store.cache,rows=self.rows)
        with self.assertRaises(artifacts.SourceError):store.ensure(self.rows[0])
        self.assertEqual(self.token_requests,0)
    def test_http_requires_explicit_consent(self):
        self.config['allow_http']=False
        with self.assertRaises(artifacts.SourceError):artifacts.source_config(self.root,self.config)
    def test_blob_config_cannot_change_release_hash(self):
        self.source['blob_digest']='sha256:'+'a'*64
        with self.assertRaises(artifacts.SourceError):self.store.ensure(self.rows[0])
    def test_archive_member_mapping_rejects_escape(self):
        self.archive();self.source['files'][self.rows[0]['path']]='../../escape'
        with self.assertRaises((RuntimeError,ValueError)):artifacts.source_config(self.root,self.config)
    def test_bundle_auxiliary_source_works_with_standalone(self):
        self.config['local_roots']=['@bundle/support-files']
        with mock.patch.dict(os.environ,AI_PLATFORM_SOURCE_BASE=str(self.root/'media-base')):
            self.assertEqual(artifacts.source_roots(self.root,self.config),(self.root/'support-files',))
    def test_public_error_does_not_include_token_or_url(self):
        self.bad_manifest=True
        with self.assertRaises(artifacts.SourceError) as exc:self.store.ensure(self.rows[0])
        self.assertNotIn('fixture-token-secret',str(exc.exception));self.assertNotIn(self.base,str(exc.exception))

if __name__=='__main__':unittest.main(verbosity=2)
