"""Real HTTP/filesystem installation tests and bounded host-operation contracts."""
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
HOME=ROOT/'build/resources-1.2.0/payload/home/cix'
sys.path.insert(0,str(HOME/'gateway-admin'))
from agent.model_store import Catalog, ModelStore, atomic_json
from agent.model_install import Installer
from agent.registry import RegistryClient
from agent.model_api import LocalImport, local_path, saved_job, Selection
from host_ops import runtime, server


class Runtime:
    def __init__(self): self.running=False;self.fail=False;self.calls=[]
    def preflight(self, *a): return [],[]
    def inspect(self, model): return {'State':{'Running':self.running}} if self.running else None
    def image_ready(self, model): return True
    def stop(self, model): self.running=False;self.calls.append('stop')
    def start(self, model, progress): self.running=True;self.calls.append('start')
    def probe(self, model):
        self.calls.append('probe')
        if self.fail:raise RuntimeError('inference failed')
        return {'passed':True}


class ModelSupplyTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.body=b'fixed model weights '*500
        self.sha=hashlib.sha256(self.body).hexdigest()
        self.model=copy.deepcopy(Catalog().models['mobilenet_v2'])
        self.model['files']=[{'target':self.model['files'][0]['target'],'sha256':self.sha,'size':len(self.body),
            'url':'https://modelscope.cn/fixed'}]

    def store(self, model=None):
        p=self.root/'catalog.json';atomic_json(p,{'schema_version':1,'models':[model or self.model]})
        return ModelStore(self.root/'cache',Catalog(p))

    def archive(self, name='weights/model.cix'):
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w:gz') as tar:
            item=tarfile.TarInfo(name);item.size=len(self.body);tar.addfile(item,io.BytesIO(self.body))
        return stream.getvalue()

    def tar_model(self, data):
        model=copy.deepcopy(self.model);f=model['files'][0];f.pop('url')
        f['oci_source']={'registry':'http://36.111.34.121:15436','repository':'aistore/fixture','reference':'v1',
            'manifest_digest':'sha256:'+'1'*64,'blob_digest':'sha256:'+hashlib.sha256(data).hexdigest(),
            'blob_size':len(data),'format':'tar+gzip','files':{'payload/home/cix/'+f['target']:'weights/model.cix'}}
        return model

    def test_all_published_models_use_installer_harbor_sources(self):
        sources=json.loads((ROOT/'installer-bundle/sources.json').read_text())['oci_artifacts']
        for base,count in [(HOME,2),(ROOT/'build/resources-1.2.0/variants/single',1)]:
            doc=json.loads((base/'gateway-admin/agent/model_catalog.json').read_text())
            self.assertEqual(len(doc['models']),25 if count==2 else 24)
            for model in doc['models']:
                for item in model['files']:
                    self.assertNotIn('url',item)
                    if 'oci_source' in item:
                        source=item['oci_source'];self.assertIn(source,list(sources.values()))
                        self.assertIn('payload/home/cix/'+item['target'],source['files'])
                        self.assertEqual(source.get('device_count',count),count)
                    else:self.assertTrue((HOME/'gateway-admin/agent'/item['bundled_file']).is_file())

    def test_projector_is_shared_between_single_and_dual(self):
        rows=[]
        for base in [HOME,ROOT/'build/resources-1.2.0/variants/single']:
            d=json.loads((base/'gateway-admin/agent/model_catalog.json').read_text())
            m=next(m for m in d['models'] if m['id']=='qwen3.8-27b')
            rows.append(next(f for f in m['files'] if 'mmproj-' in f['target']))
        for key in ['sha256','size','target']:self.assertEqual(rows[0][key],rows[1][key])
        for key in ['repository','blob_digest','blob_size']:self.assertEqual(rows[0]['oci_source'][key],rows[1]['oci_source'][key])
        self.assertEqual(rows[0]['oci_source']['repository'],'aistore/qwen3-8-27b-mmproj')

    def test_raw_import_install_and_probe_use_real_files(self):
        store=self.store();local=self.root/'weights.gguf';local.write_bytes(self.body)
        self.assertEqual(store.import_local(local)['files_imported'],1)
        home=self.root/'home';home.mkdir();rt=Runtime();inst=Installer(store,home,rt)
        inst.save({'schema_version':1,'models':{},'revision':0})
        result=inst.install(['mobilenet_v2'],True,lambda p:None)
        self.assertTrue(result['activated']);self.assertEqual((home/self.model['files'][0]['target']).read_bytes(),self.body)
        self.assertEqual(rt.calls,['stop','start','probe']);self.assertTrue(inst.snapshot()['models'][0]['enabled'])

    def test_gateway_admission_changes_only_after_successful_activation(self):
        sys.path.insert(0,str(HOME/'ai-model-gateway'))
        from app.config import Registry
        from app.errors import GatewayError
        store=self.store();local=self.root/'model.cix';local.write_bytes(self.body);store.import_local(local)
        home=self.root/'home';home.mkdir();inst=Installer(store,home,Runtime())
        inst.save({'schema_version':1,'revision':0,'models':{}})
        registry=Registry(HOME/'ai-model-gateway/config/models.json',installation_path=inst.state_path)
        with self.assertRaises(GatewayError):registry.get('mobilenet_v2')
        inst.install(['mobilenet_v2'],False,lambda _:None)
        with self.assertRaises(GatewayError):registry.get('mobilenet_v2')
        inst.manage(['mobilenet_v2'],'activate',lambda _:None)
        self.assertTrue(registry.get('mobilenet_v2').enabled)
        inst.manage(['mobilenet_v2'],'disable',lambda _:None)
        with self.assertRaises(GatewayError):registry.get('mobilenet_v2')

    def test_partial_deployment_missing_files_is_not_installed(self):
        store=self.store();home=self.root/'home';home.mkdir();inst=Installer(store,home,Runtime())
        inst.save({'schema_version':1,'revision':0,'models':{'mobilenet_v2':{'installed':True,'enabled':True}}})
        row=inst.snapshot()['models'][0]
        self.assertFalse(row['installed']);self.assertFalse(row['enabled']);self.assertEqual(row['status'],'missing_files')

    def test_failed_inference_restores_old_file_and_closes_admission(self):
        store=self.store();local=self.root/'model.cix';local.write_bytes(self.body);store.import_local(local)
        home=self.root/'home';home.mkdir();rt=Runtime();rt.fail=True;inst=Installer(store,home,rt)
        inst.save({'schema_version':1,'revision':0,'models':{}})
        target=home/self.model['files'][0]['target'];target.parent.mkdir(parents=True);target.write_bytes(b'previous')
        with self.assertRaisesRegex(RuntimeError,'inference failed'):inst.install(['mobilenet_v2'],True,lambda p:None)
        self.assertEqual(target.read_bytes(),b'previous');self.assertFalse(inst.state()['models']['mobilenet_v2']['enabled'])

    def test_oras_tar_import_and_directory_import(self):
        data=self.archive();store=self.store(self.tar_model(data));p=self.root/'oras.tar.gz';p.write_bytes(data)
        self.assertEqual(store.import_local(p)['files_imported'],1)
        self.assertTrue(store.ready(store.catalog.files[self.sha]))
        folder=self.root/'usb';folder.mkdir();(folder/'model.cix').write_bytes(self.body)
        self.assertEqual(store.import_local(folder)['files_imported'],1)

    def test_unknown_raw_file_cannot_add_a_model(self):
        store=self.store();p=self.root/'unknown.gguf';p.write_bytes(b'unknown')
        with self.assertRaises(ValueError):store.import_local(p)
        self.assertFalse(store.ready(store.catalog.files[self.sha]))

    def test_full_installer_folder_skips_platform_and_runtime_archives(self):
        store=self.store();folder=self.root/'delivery';folder.mkdir()
        target=folder/'payload/home/cix'/self.model['files'][0]['target'];target.parent.mkdir(parents=True);target.write_bytes(self.body)
        for name in ['all-arm64-images.tar.gz','cosyvoice-runtime.tar.gz']:
            (folder/name).write_bytes(self.archive('platform/manifest.json'))
        self.assertEqual(store.import_local(folder)['files_imported'],1)
        self.assertTrue(store.ready(store.catalog.files[self.sha]))

    def test_archive_traversal_is_rejected(self):
        data=self.archive('../escape');store=self.store(self.tar_model(data));p=self.root/'archive';p.write_bytes(data)
        with self.assertRaises(ValueError):store.import_local(p)
        self.assertFalse((self.root/'escape').exists())

    def test_symlink_directory_is_rejected(self):
        store=self.store();folder=self.root/'usb';folder.mkdir();(folder/'link').symlink_to('/etc/passwd')
        with self.assertRaisesRegex(ValueError,'链接'):store.import_local(folder)

    def test_real_registry_http_range_archive_extraction_and_digest(self):
        data=self.archive();model=self.tar_model(data);source=model['files'][0]['oci_source']
        manifest=json.dumps({'layers':[{'digest':source['blob_digest'],'size':len(data)}]}).encode()
        source['manifest_digest']='sha256:'+hashlib.sha256(manifest).hexdigest();ranges=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_GET(self):
                body=manifest if '/manifests/' in self.path else data
                start=0
                if self.headers.get('Range'):start=int(self.headers['Range'][6:-1]);ranges.append(start)
                self.send_response(206 if start else 200)
                if start:self.send_header('Content-Range',f'bytes {start}-{len(body)-1}/{len(body)}')
                self.end_headers();self.wfile.write(body[start:])
        http=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=http.serve_forever,daemon=True).start()
        self.addCleanup(http.server_close);self.addCleanup(http.shutdown)
        store=self.store(model)
        (store.root/'cache'/source['blob_digest'][7:]).with_suffix('.part').write_bytes(data[:10])
        def client(s,timeout):return RegistryClient({**s,'registry':f'http://127.0.0.1:{http.server_port}'},timeout)
        with patch('agent.registry.RegistryClient',side_effect=client):store.fetch(['mobilenet_v2'])
        self.assertEqual(ranges,[10]);self.assertEqual(store.cached(store.catalog.files[self.sha]).read_bytes(),self.body)

    def test_local_path_cannot_escape_import_roots(self):
        with self.assertRaises(ValueError):local_path('staging','../.env')
        self.assertEqual(len(Selection(ids=list(Catalog().models)).ids),25)


class HostOperationsTests(unittest.TestCase):
    def test_real_unix_socket_requires_token_and_exposes_only_fixed_routes(self):
        import http.client,socket
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'ops.sock'
            with server.Server(str(path),server.Handler) as service:
                thread=threading.Thread(target=service.serve_forever,daemon=True);thread.start()
                class Connection(http.client.HTTPConnection):
                    def connect(self):
                        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.connect(str(path))
                try:
                    with patch.object(server,'token',return_value='fixture-token'),patch.object(server,'overview',return_value={'version':'1.2.0'}):
                        connection=Connection('localhost');connection.request('GET','/overview');response=connection.getresponse();self.assertEqual(response.status,403);response.read();connection.close()
                        connection=Connection('localhost');connection.request('GET','/overview',headers={'Authorization':'Bearer fixture-token'});response=connection.getresponse();self.assertEqual(json.loads(response.read())['version'],'1.2.0');connection.close()
                        connection=Connection('localhost');connection.request('POST','/execute',body=b'{}',headers={'Authorization':'Bearer fixture-token'});response=connection.getresponse();self.assertEqual(response.status,404);response.read();connection.close()
                finally:service.shutdown();thread.join()

    def test_adding_component_preserves_existing_connection_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder);previous={'components':{'mysql':{'password':'existing-mysql-secret'}},'selected_components':['mysql']}
            atomic_json(home/'AI-Platform-1.1-connections.json',previous)
            with (patch.object(runtime,'HOME',home),patch.object(runtime,'STATE',home/'state'),
                  patch('pwd.getpwnam',return_value=SimpleNamespace(pw_uid=os.getuid(),pw_gid=os.getgid()))):
                instance=runtime.manager(['redis'],{'redis_password':'new-redis-secret-123'})
                instance.export_connections();updated=runtime.read_connections()
            self.assertEqual(updated['components']['mysql'],previous['components']['mysql'])
            self.assertEqual(updated['components']['redis']['password'],'new-redis-secret-123')
            self.assertEqual(updated['selected_components'],['mysql','redis'])

    def test_milvus_plan_requires_new_dependency_credentials(self):
        with patch.object(runtime,'read_connections',return_value={'components':{}}):
            with self.assertRaisesRegex(ValueError,'minio'):runtime.settings_for(['milvus'])
            settings=runtime.settings_for(['milvus'],{'minio_user':'operator','minio_password':'fixture-secret-123'})
        self.assertEqual(runtime.resolved_components(settings),['etcd','minio','milvus'])

    def test_existing_credentials_cannot_be_silently_changed(self):
        previous={'components':{'redis':{'password':'existing-secret-123'}}}
        with patch.object(runtime,'read_connections',return_value=previous):
            self.assertEqual(runtime.settings_for(['redis'])['redis_password'],'existing-secret-123')
            with self.assertRaisesRegex(ValueError,'已有组件凭据'):runtime.settings_for(['redis'],{'redis_password':'replacement-secret'})

    def test_stop_dependency_is_blocked_while_milvus_runs(self):
        rows=[{'id':'minio','managed':True,'running':True},{'id':'milvus','managed':True,'running':True}]
        with patch.object(runtime,'component_rows',return_value=rows),patch.object(runtime,'run') as command:
            with self.assertRaisesRegex(ValueError,'Milvus'):runtime.action('minio','stop')
            command.assert_not_called()

    def test_unknown_services_never_reach_a_command(self):
        with patch.object(runtime,'run') as command:
            for value in ['sshd','redis;rm','../../etc/passwd']:
                with self.assertRaises(ValueError):runtime.action(value,'restart')
                with self.assertRaises(ValueError):runtime.logs(value)
            command.assert_not_called()

    def test_logs_redact_stored_credentials(self):
        with (patch.object(runtime,'run',return_value=SimpleNamespace(stdout='password=secret-value',stderr='')),
              patch.object(runtime,'read_connections',return_value={'components':{'redis':{'password':'secret-value'}}})):
            self.assertNotIn('secret-value',str(runtime.logs('redis')))

    def test_probe_http_health_does_not_imply_mqtt_connection(self):
        result=SimpleNamespace(returncode=0,stdout='')
        with patch.object(runtime,'run',return_value=result):
            value=server.probe_status()
        self.assertIsNone(value['mqtt_connected'])

    def test_restart_job_is_unknown_after_agent_loss(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'MODELDECK_STORE_ROOT':folder}):
            p=Path(folder)/'jobs'/('1'*32+'.json');atomic_json(p,{'status':'running','kind':'component-restart'})
            self.assertEqual(saved_job('1'*32)['status'],'unknown')

    def test_monitor_retains_recovery_state_and_samples(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(runtime,'STATE',Path(folder)),patch.object(server,'metrics',return_value={'at':1,'cpu_percent':1,'memory_percent':1,'disks':[]}),patch.object(server,'probe_status',return_value={'running':False}),patch.object(runtime,'component_rows',return_value=[]):
            server.sample()
            with server.database() as db:self.assertEqual(db.execute('SELECT count(*) FROM alerts WHERE resolved IS NULL').fetchone()[0],1)
            with patch.object(server,'probe_status',return_value={'running':True,'registered':True,'mqtt_connected':True}):server.sample()
            with server.database() as db:self.assertEqual(db.execute('SELECT count(*) FROM alerts WHERE resolved IS NULL').fetchone()[0],0)
