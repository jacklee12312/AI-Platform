from __future__ import annotations

import importlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from admin_app.api_reference import build_openapi, build_reference


def registry():
    source = json.loads((ROOT.parent / "ai-model-gateway/config/models.json").read_text(encoding="utf-8"))
    return {"object": "list", "data": [
        {key: value for key, value in item.items() if key not in {"base_url", "headers"}}
        for item in source["models"]
    ]}


class ReferenceTests(unittest.TestCase):
    def test_reference_contains_fields_responses_timeouts_protocols_and_public_errors(self):
        result = build_reference(registry(), {"settings": {
            "chat_timeout_seconds": 420, "cv_timeout_seconds": 210, "audio_timeout_seconds": 700,
            "guard_timeout_seconds": 95, "stream_guard_timeout_seconds": 35, "asr_stream_max_seconds": 1800,
        }})
        endpoints = {item["id"]: item for item in result["endpoints"]}
        self.assertEqual(len(endpoints), 12)
        self.assertEqual(endpoints["chat"]["timeout_value"]["value"], 420)
        self.assertTrue(endpoints["chat"]["timeout_value"]["confirmed"])
        self.assertIn("qwen3.8-27b", endpoints["chat"]["models"])
        self.assertEqual(endpoints["legacy-ocr"]["models"], ["glm-ocr-0.9b"])
        self.assertTrue(any(field["name"] == "messages[].content[].image_url.url" for field in endpoints["chat"]["request_fields"]))
        self.assertTrue(any(field["name"] == "choices[].message.content" for field in endpoints["chat"]["response_fields"]))
        self.assertIn("[DONE]", " ".join(endpoints["chat"]["streaming"]["rules"]))
        self.assertIn("done", " ".join(endpoints["asr-ws"]["streaming"]["events"]))
        for endpoint in endpoints.values():
            if endpoint["auth"]:
                self.assertIn("invalid_api_key", endpoint["error_codes"])
                self.assertIn("rate_limit_exceeded", endpoint["error_codes"])
        self.assertGreaterEqual(len(result["errors"]), 30)

    def test_unavailable_runtime_is_truthfully_marked_and_does_not_leak_configuration(self):
        result = build_reference(None, {"settings": {"chat_timeout_seconds": 360, "API_KEY": "do-not-export"}}, registry_error=True)
        self.assertFalse(result["registry_available"])
        self.assertTrue(all(not endpoint["models_confirmed"] for endpoint in result["endpoints"]))
        self.assertNotIn("do-not-export", json.dumps(result, ensure_ascii=False))

    def test_openapi_covers_every_public_http_route_and_websocket_contract(self):
        reference = build_reference(registry(), {"settings": {}})
        document = build_openapi(reference)
        source = (ROOT.parent / "ai-model-gateway/app/main.py").read_text(encoding="utf-8")
        declared_http = set(re.findall(r'@app\.(?:get|post)\("([^\"]+)"', source))
        public_http = {path for path in declared_http if path.startswith("/v1") or path.startswith("/health")}
        self.assertEqual(public_http, set(document["paths"]))
        self.assertEqual(document["paths"]["/v1/chat/completions"]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"],
                         "#/components/schemas/ChatCompletionRequest")
        self.assertIn("multipart/form-data", document["paths"]["/v1/audio/transcriptions"]["post"]["requestBody"]["content"])
        self.assertIn("image/png", document["paths"]["/v1/cv/{operation}"]["post"]["requestBody"]["content"])
        self.assertIn("audio/wav", document["paths"]["/v1/audio/speech"]["post"]["responses"]["200"]["content"])
        self.assertEqual(document["paths"]["/v1/audio/tts"]["post"]["x-alias-of"], "/v1/audio/speech")
        self.assertEqual(document["paths"]["/health"]["get"]["security"], [])
        self.assertEqual(document["paths"]["/v1/models"]["get"]["security"], [{"BearerAuth": []}])
        self.assertEqual(document["x-websocket-endpoints"][0]["path"], "/v1/audio/transcriptions/stream")
        self.assertNotIn("/internal/api-keys", document["paths"])


class ReferenceAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from hash_password import encode
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, ADMIN_DATA_DIR=cls.temp.name,
                                  ADMIN_PASSWORD_HASH=encode("reference-test-password"))
        cls.env.start()
        sys.modules.pop("admin_app.main", None)
        cls.module = importlib.import_module("admin_app.main")
        cls.context = TestClient(cls.module.app)
        cls.client = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)
        cls.env.stop()
        cls.temp.cleanup()

    def test_reference_and_openapi_require_login_and_download_complete_document(self):
        for path in ["/api/api-reference", "/api/api-reference/openapi.json"]:
            self.assertEqual(self.client.get(path).status_code, 401)
        self.client.post("/api/login", json={"password": "reference-test-password"})
        async def gateway(method, path, **_):
            self.assertEqual((method, path), ("GET", "/v1/models"))
            return registry()
        async def agent(method, path, **_):
            self.assertEqual((method, path), ("GET", "/v1/config"))
            return {"settings": {"chat_timeout_seconds": 333, "audio_timeout_seconds": 611}}
        with mock.patch.object(self.module, "_gateway", side_effect=gateway), mock.patch.object(self.module, "_agent", side_effect=agent):
            result = self.client.get("/api/api-reference")
            downloaded = self.client.get("/api/api-reference/openapi.json")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(next(item for item in result.json()["timeouts"] if item["id"] == "chat_timeout_seconds")["value"], 333)
        self.assertIn("attachment", downloaded.headers["Content-Disposition"])
        document = downloaded.json()
        self.assertEqual(document["openapi"], "3.1.0")
        self.assertIn("/v1/chat/completions", document["paths"])
        self.assertNotIn("/api/api-reference", document["paths"])


if __name__ == "__main__":
    unittest.main()
