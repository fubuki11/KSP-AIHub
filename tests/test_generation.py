import copy
import io
from http.server import ThreadingHTTPServer
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ksp_aihub.common import HubError
from ksp_aihub.config import Configuration
from ksp_aihub.hub import Hub
import test_hub as fixtures


class OutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), fixtures.MockProvider)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    setUp = fixtures.ProviderTests.setUp

    def stream(self, events, done=True):
        self.server.content_type = "text/event-stream"
        self.server.reply = ("".join("data: " + json.dumps(e) + "\n\n" for e in events) + ("data: [DONE]\n\n" if done else "")).encode()

    def generate(self):
        return self.adapter.generate(self.config.profiles["one"], self.messages, "json")

    def test_chat_stream_reasoning_is_not_final_output_and_usage_is_retained(self):
        self.stream([
            {"choices": [{"delta": {"reasoning_content": "private thinking"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": '{"ok":'}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": 'true}'}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 20, "completion_tokens": 50}},
        ])
        result = self.generate()
        self.assertEqual(json.loads(result["jsonText"]), {"ok": True})
        self.assertEqual(result["outputTokens"], 50)
        self.assertTrue(self.server.received[0][2]["stream"])
        self.assertNotIn("private thinking", json.dumps(result))

    def test_sse_metadata_overhead_does_not_consume_final_text_budget(self):
        events = [{"padding": "x" * 1200, "choices": [{"delta": {"content": ""}}]} for _ in range(1800)]
        events.append({"choices": [{"delta": {"content": '{"ok":true}'}, "finish_reason": "stop"}]})
        self.stream(events)
        self.assertGreater(len(self.server.reply), 2000000)
        self.assertEqual(json.loads(self.generate()["jsonText"]), {"ok": True})

    def test_decoded_text_is_still_bounded_with_many_small_stream_frames(self):
        event = {"choices": [{"delta": {"content": "x" * 100000}}]}
        stream = io.BytesIO((("data: " + json.dumps(event) + "\n\n") * 21).encode())
        with self.assertRaises(HubError) as error: self.adapter._stream(stream, "chat_completions", time.monotonic() + 5)
        self.assertEqual(error.exception.code, "provider_response_limit")

    def test_chat_truncation_has_actionable_safe_budget_metadata(self):
        self.stream([{"choices": [{"delta": {"reasoning_content": "secret-provider-key"}, "finish_reason": "length"}],
                      "usage": {"completion_tokens": 6000, "completion_tokens_details": {"reasoning_tokens": 6000}}}])
        with self.assertRaises(HubError) as error: self.generate()
        self.assertEqual(error.exception.code, "output_truncated")
        self.assertEqual(error.exception.details["reasoningTokens"], 6000)
        self.assertNotIn("secret-provider-key", str(error.exception) + json.dumps(error.exception.details))

    def test_valid_json_without_completion_is_never_accepted(self):
        self.stream([{"choices": [{"delta": {"content": '{"ok":true}'}}]}])
        with self.assertRaises(HubError) as error: self.generate()
        self.assertEqual(error.exception.code, "provider_incomplete")

    def test_complete_json_with_length_finish_still_fails(self):
        self.server.reply = json.dumps({"choices": [{"finish_reason": "length", "message": {"content": '{}'}}]}).encode()
        with self.assertRaises(HubError) as error: self.generate()
        self.assertEqual(error.exception.code, "output_truncated")

    def test_content_parts_and_single_whole_response_fence_are_supported(self):
        for text in ('```json\n{"ok":true}\n```', [{"type": "text", "text": '{"ok":true}'}]):
            self.server.reply = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": text}}]}).encode()
            self.assertEqual(json.loads(self.generate()["jsonText"]), {"ok": True})

    def test_prose_and_partial_objects_are_not_brace_extracted_or_repaired(self):
        for text in ('Here is a design: {"ok":true}', '{"ok":', '[]'):
            self.server.reply = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": text}}]}).encode()
            with self.assertRaises(HubError) as error: self.generate()
            self.assertEqual(error.exception.code, "invalid_json_output")

    def test_refusals_tools_and_empty_output_are_distinct(self):
        for reason, message, code in (("content_filter", {"content": ""}, "model_refusal"),
                                       ("stop", {"content": "", "refusal": "secret-provider-key"}, "model_refusal"),
                                       ("tool_calls", {"content": None}, "model_tools_unsupported"),
                                       ("stop", {"content": "", "reasoning_content": "thoughts"}, "empty_model_output")):
            self.server.reply = json.dumps({"choices": [{"finish_reason": reason, "message": message}]}).encode()
            with self.assertRaises(HubError) as error: self.generate()
            self.assertEqual(error.exception.code, code)
            self.assertNotIn("secret-provider-key", str(error.exception))

    def test_responses_incomplete_event_retains_reason_and_token_usage(self):
        self.config.profiles["one"]["protocol"] = "responses"
        self.stream([{"type": "response.incomplete", "response": {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                      "usage": {"output_tokens": 6000, "output_tokens_details": {"reasoning_tokens": 5990}}}}])
        with self.assertRaises(HubError) as error: self.generate()
        self.assertEqual(error.exception.code, "output_truncated")
        self.assertEqual(error.exception.details["outputTokens"], 6000)

    def test_responses_refusal_deltas_are_not_mistaken_for_empty_json(self):
        self.config.profiles["one"]["protocol"] = "responses"
        self.stream([{"type": "response.refusal.delta", "delta": "hidden reason"},
                     {"type": "response.completed", "response": {"status": "completed", "output": []}}])
        with self.assertRaises(HubError) as error: self.generate()
        self.assertEqual(error.exception.code, "model_refusal")

    def test_messages_stream_completion_and_truncation(self):
        self.config.profiles["one"]["protocol"] = "messages"
        for finish, code in (("end_turn", None), ("max_tokens", "output_truncated")):
            self.stream([{"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
                         {"type": "content_block_delta", "delta": {"type": "text_delta", "text": '{"ok":true}'}},
                         {"type": "message_delta", "delta": {"stop_reason": finish}, "usage": {"output_tokens": 12}},
                         {"type": "message_stop"}], done=False)
            if code:
                with self.assertRaises(HubError) as error: self.generate()
                self.assertEqual(error.exception.code, code)
            else: self.assertEqual(self.generate()["outputTokens"], 12)

    def test_reasoning_parameters_are_explicit_and_protocol_appropriate(self):
        self.server.reply = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": '{}'}}]}).encode()
        p = self.config.profiles["one"]
        p["reasoningEffort"] = "low"; self.generate()
        self.assertEqual(self.server.received[-1][2]["reasoning_effort"], "low")
        p.update(reasoningEffort="provider_default", thinkingMode="disabled"); self.generate()
        self.assertEqual(self.server.received[-1][2]["thinking"], {"type": "disabled"})

    def test_codex_budget_is_honestly_marked_provider_managed(self):
        self.config.profiles["one"].update(protocol="responses", flavor="codex")
        self.server.reply = json.dumps({"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": '{}'}]}]}).encode()
        result = self.generate()
        self.assertTrue(result["providerManagedOutput"])
        self.assertEqual(result["outputLimit"], -1)
        self.assertNotIn("max_output_tokens", self.server.received[-1][2])

    def test_transport_timeout_is_not_a_model_format_error(self):
        with patch.object(self.adapter.opener, "open", side_effect=TimeoutError), self.assertRaises(HubError) as error: self.generate()
        self.assertEqual(error.exception.code, "provider_timeout")


class GenerationSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.value = fixtures.configuration()
        class Adapter:
            seen = []
            def generate(self, profile, messages, fmt):
                self.seen.append(profile)
                return {"text": "{}", "jsonText": "{}", "inputTokens": 1, "outputTokens": 1}
        self.adapter = Adapter()
        self.hub = Hub(Configuration(self.value), self.temp.name, fixtures.MemoryStore(), self.adapter)

    def test_settings_persist_without_changing_routes_or_original_config(self):
        before = copy.deepcopy(self.value)
        self.hub.set_generation({"profile": "one", "settings": {"maxOutputTokens": 18000, "recoveryMaxOutputTokens": 24000, "timeout": 400, "reasoningEffort": "low"}})
        fresh = Hub(Configuration(self.value), self.temp.name, fixtures.MemoryStore())
        self.assertEqual(fresh.ui()["maxOutputTokens"], 18000)
        self.assertEqual(fresh.resolve()[0], "one")
        self.assertEqual(before, self.value)

    def test_recovery_is_bounded_by_profile_and_never_mutates_normal_budget(self):
        self.hub.set_generation({"profile": "one", "settings": {"maxOutputTokens": 16000, "recoveryMaxOutputTokens": 20000}})
        req = {"messages": [{"role": "user", "content": "test"}]}
        self.hub.generate(req); self.hub.generate(dict(req, recovery=True)); self.hub.generate(req)
        self.assertEqual([p["maxOutputTokens"] for p in self.adapter.seen], [16000, 20000, 16000])

    def test_bad_settings_cannot_inject_endpoint_or_bypass_limits(self):
        for settings in ({"baseUrl": "https://other.example"}, {"maxOutputTokens": 100000}, {"recoveryMaxOutputTokens": 100},
                         {"timeout": 601}, {"thinkingMode": "disabled", "reasoningEffort": "low"}):
            with self.assertRaises(HubError): self.hub.set_generation({"profile": "one", "settings": settings})
        with self.assertRaises(HubError): self.hub.generate({"messages": [{"role": "user", "content": "hi"}], "recovery": 2})


class GenerationHttpTests(unittest.TestCase):
    setUp = fixtures.HubHttpTests.setUp
    close = fixtures.HubHttpTests.close
    request = fixtures.HubHttpTests.request

    def test_generation_settings_and_error_metadata_cross_real_http(self):
        from urllib.error import HTTPError
        self.request('/v1/generation-settings', {'profile': 'one', 'settings': {'maxOutputTokens': 20000, 'timeout': 300}})
        self.assertEqual(self.request('/v1/ui')['maxOutputTokens'], 20000)
        with patch.object(self.hub.adapters, 'generate', side_effect=HubError('output_truncated', 'Budget reached', 502, {'outputTokens': 6000})):
            with self.assertRaises(HTTPError) as error:
                self.request('/v1/generate', {'messages': [{'role': 'user', 'content': 'test'}]})
            try: response = json.loads(error.exception.read())
            finally: error.exception.close()
        self.assertEqual(response['code'], 'output_truncated')
        self.assertEqual(response['details']['outputTokens'], 6000)


if __name__ == "__main__": unittest.main()
