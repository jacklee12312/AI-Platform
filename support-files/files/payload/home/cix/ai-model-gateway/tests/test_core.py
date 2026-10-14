import json
import tempfile
import unittest
from pathlib import Path

from app.chat import normalize_tool_history
from app.config import Registry
from app.defaults import apply_generation_defaults
from app.errors import GatewayError
from app.guard import messages_for_guard, moderation_result, parse_guard_output
from app.streaming import sse_event, stream_assessment
from app.usage import APIKeyUsageStore, normalize_usage


class ChatCompatibilityTests(unittest.TestCase):
    def test_generation_defaults_fill_only_missing_fields(self):
        payload, applied = apply_generation_defaults(
            {"temperature": 0.7, "stream": True},
            enabled=True,
            max_tokens=512,
            temperature=0.2,
            top_p=0.9,
        )
        self.assertEqual(payload["temperature"], 0.7)
        self.assertEqual(payload["max_tokens"], 512)
        self.assertEqual(payload["top_p"], 0.9)
        self.assertEqual(applied, ["max_tokens", "top_p"])

    def test_generation_defaults_can_be_disabled(self):
        payload, applied = apply_generation_defaults(
            {"messages": []}, enabled=False, max_tokens=512, temperature=0.2, top_p=0.9
        )
        self.assertNotIn("max_tokens", payload)
        self.assertEqual(applied, [])

    def test_mixed_tool_call_message_is_normalized_without_mutating_input(self):
        messages = [
            {"role": "user", "content": "查询订单"},
            {
                "role": "assistant",
                "content": "工具超时，我将重试。",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "已发货"},
        ]

        normalized = normalize_tool_history(messages)

        self.assertIsNone(normalized[1]["content"])
        self.assertEqual(normalized[1]["tool_calls"], messages[1]["tool_calls"])
        self.assertEqual(messages[1]["content"], "工具超时，我将重试。")
        self.assertIsNot(normalized[1], messages[1])

    def test_non_tool_messages_are_preserved(self):
        messages = [
            {"role": "assistant", "content": "普通回答"},
            {"role": "assistant", "content": "空工具列表", "tool_calls": []},
            {"role": "tool", "content": "结果", "tool_calls": [{"id": "unexpected"}]},
        ]

        normalized = normalize_tool_history(messages)

        self.assertEqual(normalized, messages)
        self.assertIs(normalized[0], messages[0])
        self.assertIs(normalized[1], messages[1])
        self.assertIs(normalized[2], messages[2])

    def test_mixed_tool_call_text_is_omitted_from_guard_history(self):
        messages = [
            {"role": "user", "content": "查询订单"},
            {
                "role": "assistant",
                "content": "工具超时，我将重试。",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "已发货"},
        ]

        guarded = messages_for_guard(normalize_tool_history(messages), include_assistant=False)

        self.assertEqual(guarded, [{"role": "user", "content": "查询订单"}])

    def test_output_guard_sees_only_final_assistant_text(self):
        history = [
            {"role": "user", "content": "查询订单"},
            {
                "role": "assistant",
                "content": "工具超时，我将重试。",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "已发货"},
        ]
        output_messages = normalize_tool_history(history) + [{"role": "assistant", "content": "订单已发货"}]

        guarded = messages_for_guard(output_messages, include_assistant=True)

        self.assertEqual(guarded, [
            {"role": "user", "content": "查询订单"},
            {"role": "assistant", "content": "订单已发货"},
        ])


class GuardTests(unittest.TestCase):
    def test_safe_output(self):
        result = parse_guard_output("Safety: Safe\nCategories: None")
        self.assertEqual(result.severity, "Safe")
        self.assertFalse(result.flagged)
        self.assertEqual(result.categories, ())

    def test_unsafe_output(self):
        result = parse_guard_output("Safety: Unsafe\nCategories: Violent, Non-violent Illegal Acts")
        self.assertTrue(result.flagged)
        self.assertEqual(result.categories, ("Violent", "Non-violent Illegal Acts"))
        normalized = moderation_result(result)
        self.assertTrue(normalized["categories"]["violent"])
        self.assertTrue(normalized["categories"]["non_violent_illegal_acts"])

    def test_concatenated_guard_labels(self):
        result = parse_guard_output("Safety: SafeCategories: None")
        self.assertEqual(result.severity, "Safe")
        self.assertEqual(result.categories, ())

    def test_multimodal_messages_keep_text_only(self):
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}},
            ],
        }]
        self.assertEqual(messages_for_guard(messages), [{"role": "user", "content": "describe this"}])

    def test_terminal_assistant_inclusion_is_explicit(self):
        messages = [{"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}]
        self.assertEqual(len(messages_for_guard(messages, include_assistant=False)), 1)
        self.assertEqual(len(messages_for_guard(messages, include_assistant=True)), 2)

    def test_historical_assistant_is_preserved(self):
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "follow-up"},
        ]
        self.assertEqual(len(messages_for_guard(messages, include_assistant=False)), 3)

    def test_image_only_message_gets_guard_placeholder(self):
        messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}}]}]
        self.assertEqual(messages_for_guard(messages)[0]["role"], "user")
        self.assertIn("Image content", messages_for_guard(messages)[0]["content"])

    def test_stream_guard_assessment(self):
        assessment = stream_assessment({
            "assessment": {
                "severity": "Unsafe",
                "category": "Illegal Acts",
                "risk_probability": 0.99,
            }
        })
        self.assertTrue(assessment.flagged)
        self.assertEqual(assessment.categories, ("Illegal Acts",))

    def test_sse_event_has_complete_frame(self):
        frame = sse_event({"model": "copaw-flash-9b"})
        self.assertTrue(frame.startswith(b"data: "))
        self.assertTrue(frame.endswith(b"\n\n"))


class RegistryTests(unittest.TestCase):
    def _registry(self):
        config = {
            "models": [
                {"id": "ready", "capability": "chat", "backend": "openai", "base_url": "http://ready", "enabled": True},
                {"id": "qwen", "capability": "chat", "backend": "openai", "base_url": "http://qwen", "enabled": True, "modes": ["qwen38"]},
                {"id": "multi", "capability": "chat", "backend": "openai", "base_url": "http://multi", "enabled": True, "modes": ["multi-model"]},
                {"id": "pending", "capability": "ocr", "backend": "pending", "base_url": None, "enabled": False},
            ]
        }
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".json")
        with handle:
            json.dump(config, handle)
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        mode_handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        with mode_handle:
            mode_handle.write("qwen38\n")
        self.addCleanup(Path(mode_handle.name).unlink, missing_ok=True)
        return Registry(handle.name, mode_path=mode_handle.name)

    def test_ready_model(self):
        self.assertEqual(self._registry().get("ready", "chat").id, "ready")

    def test_pending_is_503(self):
        with self.assertRaises(GatewayError) as captured:
            self._registry().get("pending")
        self.assertEqual(captured.exception.status_code, 503)
        self.assertEqual(captured.exception.code, "model_unavailable")

    def test_model_in_active_mode(self):
        registry = self._registry()
        self.assertEqual(registry.active_mode(), "qwen38")
        self.assertEqual(registry.get("qwen", "chat").id, "qwen")

    def test_model_outside_active_mode_is_503(self):
        with self.assertRaises(GatewayError) as captured:
            self._registry().get("multi", "chat")
        self.assertEqual(captured.exception.status_code, 503)
        self.assertEqual(captured.exception.code, "model_inactive")


class APIKeyUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = APIKeyUsageStore(Path(self.temp.name) / "usage.db")
        self.store.initialize("legacy-key-value")

    def tearDown(self):
        self.temp.cleanup()

    def test_bootstrap_and_generated_keys_authenticate_independently(self):
        legacy = self.store.authenticate("legacy-key-value")
        self.assertIsNotNone(legacy)
        created = self.store.create_key("客户 A")
        self.assertTrue(created["key"].startswith("mdk_"))
        self.assertEqual(self.store.authenticate(created["key"])["id"], created["id"])
        self.assertIsNone(self.store.authenticate("wrong-key"))

        self.store.update_key(created["id"], enabled=False)
        self.assertIsNone(self.store.authenticate(created["key"]))
        self.store.update_key(created["id"], enabled=True)
        self.assertIsNotNone(self.store.authenticate(created["key"]))
        self.store.revoke_key(created["id"])
        self.assertIsNone(self.store.authenticate(created["key"]))

    def test_usage_is_separated_and_aggregated_by_key(self):
        key_a = self.store.create_key("客户 A")
        key_b = self.store.create_key("客户 B")
        self.store.record_usage(
            api_key_id=key_a["id"], request_id="req-a", endpoint="/v1/chat/completions",
            model="copaw-flash-9b", usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        )
        self.store.record_usage(
            api_key_id=key_b["id"], request_id="req-b", endpoint="/v1/chat/completions",
            model="copaw-flash-9b", usage={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
        )

        all_usage = self.store.usage_report()
        self.assertEqual(all_usage["summary"]["input_tokens"], 16)
        self.assertEqual(all_usage["summary"]["output_tokens"], 10)
        self.assertEqual(all_usage["summary"]["total_tokens"], 26)
        self.assertEqual(len(all_usage["by_key"]), 2)

        only_a = self.store.usage_report(key_id=key_a["id"])
        self.assertEqual(only_a["summary"]["request_count"], 1)
        self.assertEqual(only_a["summary"]["total_tokens"], 18)
        self.assertEqual(only_a["recent"][0]["key_name"], "客户 A")

    def test_missing_usage_is_explicitly_recorded_as_zero(self):
        created = self.store.create_key("无 usage 上游")
        self.store.record_usage(
            api_key_id=created["id"], request_id="req-zero", endpoint="/v1/chat/completions",
            model="model", usage=None, status="interrupted",
        )
        report = self.store.usage_report(key_id=created["id"])
        self.assertEqual(report["summary"]["unreported_requests"], 1)
        self.assertEqual(report["summary"]["total_tokens"], 0)
        self.assertFalse(report["recent"][0]["usage_reported"])

    def test_usage_field_names_are_normalized(self):
        self.assertEqual(normalize_usage({"prompt_tokens": 2, "completion_tokens": 3}), (2, 3, 5, True))
        self.assertEqual(normalize_usage({"input_tokens": 4, "output_tokens": 6, "total_tokens": 10}), (4, 6, 10, True))


if __name__ == "__main__":
    unittest.main()
