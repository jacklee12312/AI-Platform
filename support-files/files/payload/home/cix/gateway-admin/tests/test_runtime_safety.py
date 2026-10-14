import json
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent.runtime_safety import RuntimeSafety, fatal_record
from agent.runtime_probes import specification, validate


def item(name='houmo-copaw', version='one'):
    return {'Name':'/'+name,'Id':version,'RestartCount':0,
            'State':{'Running':True,'StartedAt':'2026-09-07T00:00:00Z','Health':{'Status':'healthy'}},
            'Config':{'Env':['HOUMO_API_KEY=fixture-private-key'],'Cmd':[]}}


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.items=[item()];self.logs='';self.fail_logs=False;self.calls=[]
        def command(args,**kwargs):
            self.calls.append(args)
            if args[1]=='ps': body='\n'.join(x['Name'][1:] for x in self.items)
            elif args[1]=='inspect': body=json.dumps(self.items)
            else: body=self.logs
            return CompletedProcess(args,125 if args[1]=='logs' and self.fail_logs else 0,body,'')
        self.command=command
        self.monitor=RuntimeSafety(Path(self.temp.name),command=command,boot_id='boot')

    def verify(self):
        self.monitor.scan(); token=self.monitor.begin_recovery()
        verified={k:v['identity'] for k,v in self.monitor.snapshot()['services'].items() if v['running']}
        self.monitor.finish_recovery(token,verified)

    def test_healthy_without_inference_remains_unverified(self):
        self.monitor.scan()
        self.assertFalse(self.monitor.snapshot()['services']['houmo-copaw']['verified'])
        self.verify()
        self.assertTrue(self.monitor.snapshot()['services']['houmo-copaw']['verified'])

    def test_fatal_is_persistent_redacted_and_healthy_cannot_clear_it(self):
        self.verify();self.logs="2026-09-07T01:00:00Z [2026-09-07 01:00:00.156] [tid 7] [error] src/tcim_stream_impl.cc:107:SyncYield: 'ret != tcim::Status::ERR_FATAL' FAILED! fixture-private-key"
        self.monitor.scan();first=self.monitor.snapshot()['incident']
        self.assertNotIn('fixture-private-key',json.dumps(first))
        self.logs='';self.monitor.scan()
        other=RuntimeSafety(Path(self.temp.name),command=self.command,boot_id='boot');other.scan()
        self.assertEqual(first,other.snapshot()['incident'])
        self.assertTrue((self.monitor.history/(first['id']+'.json')).exists())
        self.assertFalse(any(call[1] in {'start','stop','restart'} for call in self.calls))

    def test_installation_verification_only_admits_matching_tested_instance(self):
        self.items.append(item('houmo-bge-m3'))
        self.monitor.scan()
        state = self.monitor.snapshot()
        identity = state['services']['houmo-copaw']['identity']
        self.monitor.verify_installation('houmo-copaw', identity, state['revision'])
        self.assertTrue(self.monitor.snapshot()['services']['houmo-copaw']['verified'])
        self.assertFalse(self.monitor.snapshot()['services']['houmo-bge-m3']['verified'])
        self.monitor.mark_fault('test', 'new fatal')
        with self.assertRaises(RuntimeError): self.monitor.verify_installation('houmo-copaw', identity, state['revision'])
        self.assertIsNotNone(self.monitor.snapshot()['incident'])

    def test_new_instance_and_reboot_invalidate_old_verification(self):
        self.verify();self.items=[item(version='two')];self.monitor.scan()
        self.assertFalse(self.monitor.snapshot()['services']['houmo-copaw']['verified'])
        self.verify()
        other=RuntimeSafety(Path(self.temp.name),command=self.command,boot_id='new-boot');other.scan()
        self.assertFalse(other.snapshot()['services']['houmo-copaw']['verified'])

    def test_capture_failure_does_not_advance_cursor(self):
        self.monitor.scan();cursor=dict(self.monitor.cursors)
        self.fail_logs=True
        with self.assertRaises(RuntimeError):self.monitor.scan()
        self.assertEqual(cursor,self.monitor.cursors)
        self.assertIsNotNone(self.monitor.snapshot()['monitor_error'])

    def test_new_fault_during_recovery_prevents_clear(self):
        self.monitor.scan();token=self.monitor.begin_recovery()
        verified={k:v['identity'] for k,v in self.monitor.snapshot()['services'].items()}
        self.monitor.mark_fault('houmo-copaw','ERR_FATAL')
        with self.assertRaisesRegex(RuntimeError,'新故障'):self.monitor.finish_recovery(token,verified)
        self.assertIsNotNone(self.monitor.snapshot()['incident'])

    def test_probe_for_old_instance_and_partial_device_coverage_rejected(self):
        self.monitor.scan();token=self.monitor.begin_recovery()
        verified={k:v['identity'] for k,v in self.monitor.snapshot()['services'].items()}
        self.items.append(item('houmo-bge-m3'))
        with self.assertRaisesRegex(RuntimeError,'未经实际推理'):self.monitor.finish_recovery(token,verified)
        self.items=[item(version='two')]
        with self.assertRaisesRegex(RuntimeError,'实例发生变化'):self.monitor.finish_recovery(token,verified)

    def test_successful_explicit_recovery_archives_incident(self):
        self.monitor.scan();self.monitor.mark_fault('houmo-copaw','SyncYieldErr')
        incident=self.monitor.snapshot()['incident'];self.verify()
        self.assertIsNone(self.monitor.snapshot()['incident'])
        self.assertIn('resolved_at',json.loads((self.monitor.history/(incident['id']+'.json')).read_text()))

    def test_corrupt_state_requires_new_verification(self):
        self.monitor.path.parent.mkdir(parents=True)
        self.monitor.path.write_text('{')
        other=RuntimeSafety(Path(self.temp.name),command=self.command,boot_id='boot');other.scan()
        self.assertFalse(other.snapshot()['services']['houmo-copaw']['verified'])

    def test_fixed_probes_do_not_confuse_bad_values_with_success(self):
        self.assertTrue(validate({'data':[{'embedding':[0.1]*1024}]},'embedding'))
        self.assertFalse(validate({'data':[{'embedding':[float('nan')]*1024}]},'embedding'))
        self.assertFalse(validate({'data':[{'embedding':[0]*1024}]},'embedding'))
        self.assertFalse(validate({'results':[{'index':0,'relevance_score':0}]*2},'rerank'))
        self.assertTrue(specification('houmo-glm-ocr-0.9b')[2]['messages'][0]['content'][0]['image_url']['url'].startswith('data:image/png;base64,'))

    def test_only_structured_runtime_faults_trigger_isolation(self):
        records = [
            'xh2a_ipu_sync_group failed! ret:16 result:0 group_id:70 timeout_us:100000000',
            'Device 0 Core 0 Tile 2: StaticOpIdx = 53542, indicator 19 & 20 are not synchronized',
            '[2026-09-04 04:08:00.156] [tid 7] [error] [Stream] [0xaaaadd277380] Model:copaw-flash_decode DevID:0 SyncYieldErr:12 Stream:0xaaaadd2c4580',
        ]
        for record in records:
            self.assertTrue(fatal_record('2026-09-04T04:08:00.170547200Z '+record))
        self.assertTrue(fatal_record(
            '2026-09-04T10:38:06+0800 cix-localhost kernel: '
            '[pid:178,cpu7,kworker/7:2]xh2a_ipu xh2a_ipu0: group 0 00ab exec timeout!'
        ))
        self.assertFalse(fatal_record(
            '2026-09-11T11:40:09.040219349Z xh2a_ipu_sync_group failed! '
            'ret:4 result:0 hal_errno:4 group_id:24340 device_id:0 timeout_us:5000000'
        ))
        self.assertFalse(fatal_record(
            '2026-09-11T11:40:09.040665048Z HoumoVLLModel decode Run returned '
            'ERR_FATAL; automatic reset failed, run hm_smi -rd before retrying'
        ))
        self.verify()
        for text in ['解释 ERR_FATAL 和 SyncYieldErr', 'answer: '+records[0],
                     '[2026-09-04 04:08:00.156] [tid 7] [info] prompt: '+records[2]]:
            self.logs=text;self.monitor.scan()
            self.assertIsNone(self.monitor.snapshot()['incident'])


if __name__=='__main__':unittest.main()
