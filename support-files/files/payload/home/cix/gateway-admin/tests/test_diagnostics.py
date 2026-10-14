import copy
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from fastapi.testclient import TestClient

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from agent.diagnostics import arguments, bounded_command, collect, compare, number
from admin_app.diagnostics import DiagnosticsStore
from diagnostics_fixture import fixture, SECRET, CONTAINERS, HEALTH_PORTS


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.fixture=fixture(Path(self.temp.name))
    def tearDown(self):self.temp.cleanup()

    def test_real_values_are_not_replaced_with_saved_or_baseline_values(self):
        snapshot=self.fixture['collect']()
        model=next(i for i in snapshot['parameters'] if i['service']=='copaw')
        self.assertEqual((model['actual_slots'],model['saved_slots'],model['baseline_slots']),(1,2,2))
        self.assertEqual(model['slots_baseline_diff'],'different');self.assertEqual(model['slots_saved_diff'],'different')
        settings={s['id']:s for s in snapshot['settings']}
        self.assertEqual((settings['max_body_mb']['actual'],settings['max_body_mb']['saved']),(25,20))
        self.assertEqual(settings['max_body_mb']['diff'],'different')
        self.assertFalse(settings['generation_defaults_enabled']['actual'])
        stopped=next(i for i in snapshot['parameters'] if i['service']=='qwen38')
        self.assertIsNone(stopped['actual_context']);self.assertEqual(stopped['startup_context'],131072)
        self.assertEqual(stopped['context_baseline_diff'],'unknown')

    def test_only_allowlisted_values_and_fault_codes_leave_agent(self):
        snapshot=self.fixture['collect']();body=json.dumps(snapshot)
        self.assertNotIn(SECRET,body);self.assertNotIn('UNKNOWN_SECRET',body);self.assertNotIn('--api-key',body)
        self.assertEqual(snapshot['faults'][0]['code'],'err_fatal')
        self.assertEqual(snapshot['recoveries'][0]['fault_codes'],['err_fatal'])
        self.assertTrue(all(c[0] in {'ps','inspect','image','compose','logs'} for c in self.fixture['calls']))
        self.assertTrue(all('config' in c for c in self.fixture['calls'] if c[0]=='compose'))

    def test_missing_environment_and_failed_probes_are_unknown(self):
        for c in self.fixture['raw']:c['Config']['Env']=[]
        snapshot=collect(self.fixture['run'],CONTAINERS,HEALTH_PORTS,self.fixture['paths'],lambda *a: None)
        self.assertTrue(all(s['actual'] is None and s['diff']=='unknown' for s in snapshot['settings']))
        self.assertTrue(all(s['actual_slots'] is None for s in snapshot['parameters']))

    def test_failed_inventory_and_saved_compose_do_not_imply_absence_or_match(self):
        def run(*args,**kw):
            if args[0] in {'ps','compose','logs'}:return subprocess.CompletedProcess(args,1,'','secret error')
            return self.fixture['run'](*args,**kw)
        snapshot=collect(run,CONTAINERS,HEALTH_PORTS,self.fixture['paths'])
        self.assertFalse(snapshot['inventory_available']);self.assertTrue(snapshot['errors'])
        self.assertTrue(all(c['state']=='unknown' for c in snapshot['containers']))
        self.assertTrue(all(s['diff']=='unknown' for s in snapshot['settings']))
        self.assertTrue(all(not v['available'] for v in snapshot['configuration_files']))

    def test_numeric_flags_do_not_parse_shell_commands_or_leak_values(self):
        self.assertEqual(arguments(['-np','2','--ctx-size=4096','--api-key',SECRET]),{'slots':2,'context':4096})
        self.assertEqual(arguments(['sh','-c','server -np 2 --api-key '+SECRET]),{})
        self.assertIsNone(number('NaN'));self.assertIsNone(number(True));self.assertEqual(compare(None,None),'unknown')

    def test_bounded_command_caps_output_and_times_out(self):
        result=bounded_command([sys.executable,'-c','print("x" * 100000)'],max_bytes=1024)
        self.assertEqual(result.returncode,125);self.assertLessEqual(len(result.stdout),1024)
        result=bounded_command([sys.executable,'-c','import time;time.sleep(5)'],timeout=0.1)
        self.assertEqual(result.returncode,124)


class PersistenceTests(unittest.TestCase):
    def test_observation_deduplicates_and_distinguishes_replacement_from_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);snapshot=fixture(root/'fixture')['collect']();store=DiagnosticsStore(root/'diagnostics.db')
            store.observe(snapshot);first=store.history();store.observe(snapshot)
            self.assertEqual(first,store.history())
            self.assertTrue(all(e['kind']=='first_observed' for e in first['deployment']))
            snapshot['containers'][0]['started_at']='2026-09-07T01:00:00Z';store.observe(snapshot)
            self.assertEqual(store.history()['deployment'][0]['kind'],'process_restarted')
            snapshot['containers'][0]['container_id']='f'*64;snapshot['containers'][0]['created_at']='2026-09-07T02:00:00Z';store.observe(snapshot)
            history=DiagnosticsStore(root/'diagnostics.db').history()
            self.assertEqual(history['deployment'][0]['kind'],'container_replaced')
            self.assertEqual(len(history['fault']),len(first['fault']))
            incomplete=copy.deepcopy(snapshot);incomplete['containers']=[];incomplete['faults']=[];store.observe(incomplete)
            self.assertEqual(history,store.history())


class DiagnosticsAPITests(unittest.TestCase):
    def setUp(self):
        from hash_password import encode
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name)
        self.environment=mock.patch.dict(os.environ,{'ADMIN_DATA_DIR':str(root/'data'),'ADMIN_PASSWORD_HASH':encode('diagnostic-test-password'),'ADMIN_AGENT_TOKEN':SECRET})
        self.environment.start();sys.modules.pop('admin_app.main',None);self.module=importlib.import_module('admin_app.main')
        self.context=TestClient(self.module.app);self.client=self.context.__enter__();self.snapshot=fixture(root/'fixture')['collect']()
    def tearDown(self):self.context.__exit__(None,None,None);self.environment.stop();self.temp.cleanup()
    def login(self):self.client.post('/api/login',json={'password':'diagnostic-test-password'})

    def test_session_readonly_diagnostic_export_and_error_redaction(self):
        self.assertEqual(self.client.get('/api/diagnostics').status_code,401)
        self.assertEqual(self.client.get('/api/diagnostics/export.json').status_code,401)
        self.login();self.module._audit('config-apply','runtime','failed','r1',{'job':{'error':'token='+SECRET}})
        with mock.patch.object(self.module,'_agent',new=mock.AsyncMock(return_value=self.snapshot)) as agent:
            result=self.client.get('/api/diagnostics');self.assertEqual(result.status_code,200)
            export=self.client.get('/api/diagnostics/export.json');self.assertEqual(export.status_code,200)
        self.assertTrue(all(c.args[0]=='GET' and c.args[1]=='/v1/diagnostics' for c in agent.call_args_list))
        self.assertNotIn(SECRET,export.text);self.assertIn('attachment',export.headers['Content-Disposition'])
        self.assertEqual(result.json()['operations'][0]['status'],'failed')

    def test_outage_keeps_history_but_withdraws_current_claims(self):
        self.login()
        with mock.patch.object(self.module,'_agent',new=mock.AsyncMock(return_value=self.snapshot)):
            before=self.client.get('/api/diagnostics').json()
        with mock.patch.object(self.module,'_agent',new=mock.AsyncMock(side_effect=RuntimeError('offline'))):
            after=self.client.get('/api/diagnostics').json()
        self.assertFalse(after['agent_available']);self.assertIsNone(after['current']);self.assertEqual(before['history'],after['history'])

    def test_agent_endpoint_requires_token_and_reuses_bounded_cache(self):
        from agent import main
        main.DIAGNOSTICS_CACHE.clear()
        with TestClient(main.app) as client, mock.patch.object(main,'collect_diagnostics',return_value=self.snapshot) as collector, mock.patch.object(main,'AGENT_TOKEN',SECRET):
            self.assertEqual(client.get('/v1/diagnostics').status_code,401)
            first=client.get('/v1/diagnostics',headers={'X-Agent-Token':SECRET});second=client.get('/v1/diagnostics',headers={'X-Agent-Token':SECRET})
            self.assertFalse(first.json()['cached']);self.assertTrue(second.json()['cached']);self.assertEqual(collector.call_count,1)
        main.DIAGNOSTICS_CACHE.clear()
