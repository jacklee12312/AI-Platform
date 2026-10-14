import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from app.config import Registry
from app.runtime_safety import RuntimeSafety
from app.usage import APIKeyUsageStore
with mock.patch.dict(os.environ,MODEL_REGISTRY=str(Path(__file__).resolve().parents[1]/'config/models.json')):
    from app import main as gateway


class AdmissionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);self.path=root/'state.json'
        boot=root/'boot';boot.write_text('boot')
        self.safety=RuntimeSafety(self.path,required=True,boot_path=boot)
        mode=root/'mode';mode.write_text('multi-model')
        self.registry=Registry(Path(__file__).resolve().parents[1]/'config/models.json',mode_path=mode)
        self.data={'schema_version':1,'boot_id':'boot','heartbeat_epoch':time.time(),'incident':None,'maintenance':None,
                   'services':{'houmo-copaw':{'verified':True,'running':True,'health':'healthy'}}}
        self.write()
        self.store=APIKeyUsageStore(root/'usage.db');self.store.initialize();self.key=self.store.create_key('test')
        for name,value in [('RUNTIME_SAFETY',self.safety),('REGISTRY',self.registry),('USAGE_STORE',self.store)]:
            patcher=mock.patch.object(gateway,name,value);patcher.start();self.addCleanup(patcher.stop)
        self.client=TestClient(gateway.app)

    def write(self):self.path.write_text(json.dumps(self.data),encoding='utf-8')

    def test_fatal_denies_before_upstream_and_does_not_reserve_quota(self):
        self.data['incident']={'id':'first','kind':'npu_fatal','source':'houmo-copaw','detail':'ERR_FATAL','at':'2026-09-07T00:00:00Z'};self.write()
        with mock.patch.object(gateway,'_call_json') as upstream:
            response=self.client.post('/v1/chat/completions',json={'model':'copaw-flash-9b','messages':[{'role':'user','content':'hello'}]},headers={'Authorization':'Bearer '+self.key['key']})
        self.assertEqual(response.status_code,503);self.assertEqual(response.json()['error']['code'],'npu_quarantined')
        upstream.assert_not_called()
        self.assertEqual(self.store.get_key(self.key['id'])['access']['daily_requests'],0)

    def test_probe_does_not_trust_healthy_backend_when_isolated(self):
        self.data['incident']={'id':'first'};self.write()
        import asyncio
        with mock.patch.object(gateway,'_call_json') as upstream:
            result=asyncio.run(gateway._probe(self.registry.get('copaw-flash-9b')))
        self.assertEqual(result['reason'],'npu_quarantined');upstream.assert_not_called()

    def test_missing_corrupt_stale_wrong_boot_and_monitor_failure_are_closed(self):
        entry=self.registry.get('copaw-flash-9b')
        for change in [{'heartbeat_epoch':0},{'boot_id':'other'},{'monitor_error':'reader_failed'},{'heartbeat_epoch':float('nan')}]:
            self.data.update(change);self.write()
            self.assertEqual(self.safety.status(entry)['reason'],'npu_monitor_unavailable')
            self.data.update(heartbeat_epoch=time.time(),boot_id='boot',monitor_error=None)
        self.path.write_text('{');self.assertIsNotNone(self.safety.status(entry))
        self.path.unlink();self.assertIsNotNone(self.safety.status(entry))

    def test_cpu_services_untouched_and_recovery_is_not_ready(self):
        self.path.unlink()
        self.assertIsNone(self.safety.status(self.registry.get('sensevoice')))
        self.data['maintenance']={'id':'r'};self.write()
        self.assertEqual(self.safety.status(self.registry.get('copaw-flash-9b'))['reason'],'npu_recovering')

    def test_fatal_during_nonstream_response_is_not_reported_success(self):
        import asyncio
        def response(*args,**kwargs):
            self.data['incident']={'id':'during-inference'};self.write();return {'choices':[]}
        with mock.patch.object(gateway,'request_json',side_effect=response):
            with self.assertRaises(gateway.GatewayError):
                asyncio.run(gateway._call_json('http://copaw:17701/v1/chat/completions',method='POST'))


if __name__=='__main__':unittest.main()
