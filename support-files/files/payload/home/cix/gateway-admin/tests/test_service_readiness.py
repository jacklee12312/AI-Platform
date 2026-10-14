import importlib
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import main as agent


def inspect(health='healthy', running=True):
    state = {'Running': running, 'StartedAt': 'test-start', 'Health': {'Status': health}}
    return SimpleNamespace(returncode=0, stdout=json.dumps([{'State': state}]))


class ServiceReadinessTests(unittest.TestCase):
    def test_tts_and_ocr_allowlisted(self):
        for service, container in [('matcha', 'houmo-matcha-tts'), ('kokoro', 'houmo-kokoro-tts'), ('glm-ocr', 'houmo-glm-ocr-0.9b')]:
            self.assertEqual(agent.CONTAINERS[service], container)
            self.assertIn('multi-model', agent.MODE_SERVICES[service])

    def test_restart_waits_for_health(self):
        with mock.patch.object(agent, '_docker', side_effect=[inspect('starting'), inspect()]) as docker, mock.patch.object(agent.time, 'sleep'):
            result = agent._wait_restart_ready('matcha')
        self.assertTrue(result['ready'])
        self.assertEqual(docker.call_count, 2)
        self.assertEqual(result['verification'], 'docker_health')

    def test_unhealthy_timeout_is_not_success(self):
        with mock.patch.object(agent, '_docker', return_value=inspect('unhealthy')), mock.patch.object(agent.time, 'monotonic', side_effect=[0, 1, 500]), mock.patch.object(agent.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, '健康检查超时'): agent._wait_restart_ready('copaw')

    def test_crashed_container_not_automatically_restarted(self):
        with mock.patch.object(agent, '_docker', return_value=inspect(running=False)) as docker:
            with self.assertRaisesRegex(RuntimeError, '未自动重试'): agent._wait_restart_ready('copaw')
        self.assertEqual(docker.call_count, 1)


if __name__ == '__main__': unittest.main()
