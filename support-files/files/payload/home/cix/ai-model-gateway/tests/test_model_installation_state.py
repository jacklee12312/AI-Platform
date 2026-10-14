import json
import tempfile
import unittest
from pathlib import Path

from app.config import Registry
from app.errors import GatewayError


class InstallationStateTests(unittest.TestCase):
    def test_legacy_dynamic_enable_disable_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); registry = root / 'models.json'; state = root / 'installations.json'
            registry.write_text(json.dumps({'models': [{'id': 'a', 'capability': 'chat', 'base_url': 'http://example', 'enabled': True}, {'id': 'b', 'capability': 'chat', 'base_url': 'http://example', 'enabled': False}]}))
            models = Registry(registry, installation_path=state)
            self.assertTrue(models.get('a').enabled)
            for data in [{}, {'a': {'installed': True, 'enabled': False}}, {'a': {'installed': False, 'enabled': True}}, {'a': 'invalid'}]:
                state.write_text(json.dumps({'schema_version': 1, 'models': data}))
                with self.assertRaises(GatewayError): models.get('a')
            state.write_text(json.dumps({'schema_version': 1, 'models': {k: {'installed': True, 'enabled': True} for k in ['a', 'b']}}))
            self.assertTrue(models.get('a').enabled)
            with self.assertRaises(GatewayError): models.get('b')
            state.write_text('broken')
            self.assertTrue(all(not row.enabled for row in models.entries()))
            with self.assertRaises(GatewayError): models.get('a')
