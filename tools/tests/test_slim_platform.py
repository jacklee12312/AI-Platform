"""Regression coverage for removed services, plain streaming and pinned middleware pulls."""
import asyncio
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
BUNDLE=ROOT/'installer-bundle'
SPEC=json.loads((ROOT/'release-config.json').read_text('utf-8'))
RESOURCES=ROOT/SPEC['resources_root']
HOME=RESOURCES/'payload/home/cix'
sys.path.insert(0,str(BUNDLE/'installer'))
import components


class SlimPlatformTests(unittest.TestCase):
    def test_removed_models_are_absent_from_all_published_registries(self):
        removed={'qwen3guard-stream-0.6b','democratic-eval-v1'}
        catalog=json.loads((BUNDLE/'catalog/release-catalog.json').read_text('utf-8'))
        self.assertEqual(len(catalog['models']),25)
        self.assertFalse(removed.intersection(catalog['models']))
        self.assertEqual(catalog['image_count'],10)
        for base in [HOME,RESOURCES/'variants/single']:
            for relative in ['ai-model-gateway/config/models.json','gateway-admin/agent/model_catalog.json','gateway-admin/admin_app/capability_catalog.json']:
                document=json.loads((base/relative).read_text('utf-8'))
                self.assertFalse(removed.intersection(row['id'] for row in document['models']),str(base/relative))
        self.assertFalse(any('qwen3guard' in r['path'] or '/ballot-reader/' in r['path'] for r in catalog['resources']))

    def test_default_delivery_excludes_all_six_optional_component_archives(self):
        spec=importlib.util.spec_from_file_location('slim_release_workflow',ROOT/'tools/release.py')
        release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)
        catalog=json.loads((BUNDLE/'catalog/release-catalog.json').read_text('utf-8'))
        paths={r['path'] for r in release.resource_rows(BUNDLE,catalog)}
        lock=json.loads((BUNDLE/'catalog/workflow.json').read_text('utf-8'))
        self.assertEqual(len(lock['images']),6)
        self.assertTrue(all(r['archive']['path'] not in paths for r in lock['images']))

    def test_selected_component_pulls_the_exact_arm64_manifest_and_reuses_matching_images(self):
        lock=json.loads((BUNDLE/'catalog/workflow.json').read_text('utf-8'))
        row=next(r for r in lock['images'] if r['id']=='redis')
        valid={'Id':row['config_id'],'Architecture':'arm64','Os':'linux','RootFS':{'Layers':row['rootfs_diff_ids']}}
        with tempfile.TemporaryDirectory() as folder:
            sources=json.loads((BUNDLE/'sources.json').read_text('utf-8'));sources.update(mode='prefer-local',local_roots=[])
            request=Path(folder)/'request.json';request.write_text(json.dumps({'sources':sources}))
            calls=[]
            def run(args,**kwargs):
                calls.append(args)
                if args[:3]==['docker','image','inspect']:
                    exists=any(x[:2]==['docker','pull'] for x in calls)
                    return SimpleNamespace(returncode=0 if exists else 1,stdout=json.dumps([valid]) if exists else '')
                return SimpleNamespace(returncode=0,stdout='')
            flow=SimpleNamespace(settings={'components':{'selected':['redis'],'redis_password':'fixture-password-123'}},lock=lock,
                owner=SimpleNamespace(bundle=BUNDLE,event=mock.Mock()),request_path=request,run=run,asset=mock.Mock())
            adapter=components.Components(flow)
            with mock.patch('artifacts.local_candidates',return_value=[]):adapter.image('redis')
            self.assertEqual([x for x in calls if x[:2]==['docker','pull']], [['docker','pull','--platform=linux/arm64',row['pinned']]])
            flow.asset.assert_not_called()
            calls.clear();flow.run=mock.Mock(return_value=SimpleNamespace(returncode=0,stdout=json.dumps([valid])))
            adapter.image('redis');self.assertEqual(flow.run.call_count,1)
            invalid=dict(valid,Architecture='amd64');flow.run.return_value.stdout=json.dumps([invalid])
            with self.assertRaisesRegex(RuntimeError,'校验失败'):adapter.image('redis')

    def test_local_only_mode_never_pulls_missing_component_images(self):
        lock=json.loads((BUNDLE/'catalog/workflow.json').read_text('utf-8'))
        with tempfile.TemporaryDirectory() as folder:
            sources=json.loads((BUNDLE/'sources.json').read_text('utf-8'));sources.update(mode='local-only',local_roots=[])
            request=Path(folder)/'request.json';request.write_text(json.dumps({'sources':sources}))
            flow=SimpleNamespace(settings={'components':{'selected':['redis'],'redis_password':'fixture-password-123'}},lock=lock,request_path=request,
                owner=SimpleNamespace(bundle=BUNDLE,event=mock.Mock()),run=mock.Mock(return_value=SimpleNamespace(returncode=1,stdout='')))
            with mock.patch('artifacts.local_candidates',return_value=[]),self.assertRaisesRegex(RuntimeError,'本地组件镜像缺失'):
                components.Components(flow).image('redis')
            self.assertFalse(any(c.args[0][:2]==['docker','pull'] for c in flow.run.call_args_list))


class ModelDeckAndGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0,str(HOME/'ai-model-gateway'));sys.path.insert(0,str(HOME/'gateway-admin'))
        with mock.patch.dict(os.environ,{'MODEL_REGISTRY':str(HOME/'ai-model-gateway/config/models.json')}):
            cls.gateway=importlib.import_module('app.main')
        cls.config=importlib.import_module('agent.configuration')
        cls.reference=importlib.import_module('admin_app.api_reference')

    def test_modeldeck_config_cannot_enable_or_advertise_content_review(self):
        self.assertNotIn('guard',{g['id'] for g in self.config.CONFIG_SCHEMA['groups']})
        with self.assertRaises(ValueError):self.config.RuntimeSettings.model_validate({'guard_mode':'block'})
        paths={r.path for r in self.gateway.app.routes}
        self.assertNotIn('/v1/moderations',paths);self.assertNotIn('/v1/documents/analyze',paths)
        self.assertFalse({'moderations','document-analyze'}.intersection(x['id'] for x in self.reference.ENDPOINTS))
        self.assertFalse(any(k.startswith(('Moderation','Ballot')) for k in self.reference.OPENAPI_SCHEMAS))

    def test_remaining_runtime_settings_roundtrip_without_a_guard_compose_service(self):
        texts=[(HOME/p).read_text('utf-8') for p in ['ai-model-gateway/compose.yaml','houmo-docker-deployment/compose.yaml','sensevoice-cpu/compose.yaml']]
        settings=self.config.read_runtime_settings(*texts)
        settings.chat_timeout_seconds+=1
        result,changes,affected=self.config.prepare_runtime_texts(settings,*texts)
        self.assertEqual(affected,{'gateway'});self.assertEqual([x['id'] for x in changes],['chat_timeout_seconds'])
        self.assertEqual(result['houmo'],texts[1])

    def test_chat_sse_forwards_the_first_chunk_immediately_and_records_cancellation(self):
        gateway=self.gateway
        class Stream:
            status_code=200
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def aiter_lines(self):
                yield 'data: '+json.dumps({'choices':[{'delta':{'content':'first'}}]})
                await asyncio.Event().wait()
        class Client:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            def stream(self,*args,**kwargs):return Stream()
        entry=SimpleNamespace(base_url='http://fixture')
        record=mock.Mock()
        async def scenario():
            generator=gateway._chat_events(entry=entry,model_id='fixture',upstream_payload={},request_id='fixture',api_key_id=None)
            first=await asyncio.wait_for(generator.__anext__(),timeout=2)
            self.assertIn('first',first.decode() if isinstance(first,bytes) else first)
            await generator.aclose()
        with mock.patch.object(gateway.httpx,'AsyncClient',return_value=Client()),mock.patch.object(gateway.RUNTIME_SAFETY,'require'),mock.patch.object(gateway.USAGE_STORE,'record_usage',record):
            asyncio.run(scenario())
        self.assertEqual(record.call_args.kwargs['status'],'cancelled')

    def test_nonstream_chat_keeps_usage_and_has_no_review_headers(self):
        from fastapi.testclient import TestClient
        from app.config import Registry
        from app.usage import APIKeyUsageStore
        gateway=self.gateway
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/'models.json'
            path.write_text(json.dumps({'models':[{'id':'fixture','capability':'chat','base_url':'http://fixture','backend':'openai','enabled':True}]}))
            store=APIKeyUsageStore(root/'usage.db');store.initialize();key=store.create_key('fixture')
            result={'choices':[{'message':{'content':'ok'},'finish_reason':'stop'}],'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}
            with mock.patch.object(gateway,'REGISTRY',Registry(path)),mock.patch.object(gateway,'USAGE_STORE',store),mock.patch.object(gateway,'_call_json',mock.AsyncMock(return_value=result)),mock.patch.object(gateway.RUNTIME_SAFETY,'require'):
                with TestClient(gateway.app) as client:
                    response=client.post('/v1/chat/completions',headers={'Authorization':'Bearer '+key['key']},json={'model':'fixture','messages':[{'role':'user','content':'hello'}]})
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json()['choices'][0]['message']['content'],'ok')
                self.assertFalse(any(k.startswith('x-guard') for k in response.headers))


if __name__=='__main__':unittest.main()
