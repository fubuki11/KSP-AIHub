import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from ksp_aihub.auth import AuthBroker
from ksp_aihub.common import HubError
from ksp_aihub.config import Configuration
from ksp_aihub.hub import Hub
from ksp_aihub.models import ModelCatalog
from ksp_aihub.presets import PRESETS, connection
from ksp_aihub.providers import ProviderAdapters
from test_hub import MemoryStore, configuration
import test_hub as fixtures


class CatalogHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.received.append((self.path, dict(self.headers)))
        status, value, headers = self.server.replies.pop(0)
        self.send_response(status)
        for key, content in headers.items(): self.send_header(key, content)
        self.end_headers()
        self.wfile.write(value if isinstance(value, bytes) else json.dumps(value).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.received.append((self.path, dict(self.headers), body))
        self.send_response(200); self.end_headers()
        if self.path.endswith("/messages"):
            value = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "hello"}]}
        elif self.path.endswith("/responses"):
            value = {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello"}]}]}
        else:
            value = {"choices": [{"finish_reason": "stop", "message": {"content": "hello"}}]}
        self.wfile.write(json.dumps(value).encode())

    def log_message(self, *args): pass


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), CatalogHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def setUp(self):
        self.server.received = []; self.server.replies = []
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.config = Configuration(configuration(self.base))
        self.store = MemoryStore(); self.store.put("key", {"apiKey": "private-model-list-key"})
        self.auth = AuthBroker(self.config, self.store)
        self.now = [1000]
        self.catalog = ModelCatalog(self.auth, clock=lambda: self.now[0])
        self.profile = self.config.profiles["one"]

    def reply(self, value, status=200, headers=None):
        self.server.replies.append((status, value, headers or {}))

    def test_openai_catalog_deduplicates_and_keeps_new_names(self):
        self.reply({"data": [{"id": "new-model-2099"}, {"id": "z"}, {"id": "new-model-2099"}]})
        result = self.catalog.list(self.profile)
        self.assertEqual([m["id"] for m in result["models"]], ["new-model-2099", "z"])
        path, headers = self.server.received[0]
        self.assertEqual(path, "/v1/models")
        self.assertEqual(headers["Authorization"], "Bearer private-model-list-key")
        self.assertTrue(headers["User-Agent"].startswith("KSPAIHub/"))
        self.assertNotIn("private-model-list-key", json.dumps(result))

    def test_cache_expiry_and_explicit_refresh(self):
        for name in ("first", "second", "third"): self.reply({"data": [{"id": name}]})
        self.assertFalse(self.catalog.list(self.profile)["cached"])
        self.assertTrue(self.catalog.list(dict(self.profile, model="manual-other"))["cached"])
        self.assertEqual(len(self.server.received), 1)
        self.assertEqual(self.catalog.list(self.profile, True)["models"][0]["id"], "second")
        self.now[0] += 301
        self.assertEqual(self.catalog.list(self.profile)["models"][0]["id"], "third")

    def test_cache_does_not_hide_deleted_key(self):
        self.reply({"data": []}); self.catalog.list(self.profile)
        self.store.values.clear()
        with self.assertRaises(HubError) as error: self.catalog.list(self.profile)
        self.assertEqual(error.exception.code, "auth_missing")

    def test_key_change_invalidates_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            hub = Hub(self.config, directory, self.store)
            self.reply({"data": [{"id": "account-a"}]}); self.reply({"data": [{"id": "account-b"}]})
            self.assertEqual(hub.models("one")["modelIds"], ["account-a"])
            hub.set_api_key("key", "replacement-account-key")
            self.assertEqual(hub.models("one")["modelIds"], ["account-b"])
            self.assertEqual(self.server.received[-1][1]["Authorization"], "Bearer replacement-account-key")

    def test_out_of_band_credential_change_does_not_reuse_another_accounts_cache(self):
        self.reply({"data": [{"id": "account-a"}]}); self.reply({"data": [{"id": "account-b"}]})
        self.catalog.list(self.profile)
        self.store.put("key", {"apiKey": "external-replacement-key"})
        self.assertEqual(self.catalog.list(self.profile)["models"][0]["id"], "account-b")

    def test_gemini_pages_use_header_and_filter_non_generation_models(self):
        self.profile.update(modelsFormat="gemini", modelsUrl=self.base + "/v1beta/models")
        self.reply({"models": [{"name": "models/gemini-new", "displayName": "Newest Gemini", "supportedGenerationMethods": ["generateContent"]},
                              {"name": "models/embedding", "supportedGenerationMethods": ["embedContent"]}], "nextPageToken": "next+/="})
        self.reply({"models": [{"name": "models/gemini-next", "supportedGenerationMethods": ["generateContent"]}]})
        result = self.catalog.list(self.profile)
        self.assertEqual([m["id"] for m in result["models"]], ["gemini-new", "gemini-next"])
        self.assertEqual(result["models"][0]["label"], "Newest Gemini")
        for path, headers in self.server.received:
            self.assertEqual(headers["X-Goog-Api-Key"], "private-model-list-key")
            self.assertNotIn("Authorization", headers); self.assertNotIn("private-model-list-key", path)
        self.assertEqual(parse_qs(urlsplit(self.server.received[1][0]).query)["pageToken"], ["next+/="])

    def test_anthropic_pages_and_headers(self):
        self.profile.update(protocol="messages", modelsFormat="anthropic")
        self.reply({"data": [{"id": "claude-new", "display_name": "Claude New"}], "has_more": True, "last_id": "claude-new"})
        self.reply({"data": [{"id": "claude-next"}], "has_more": False})
        result = self.catalog.list(self.profile)
        self.assertEqual(len(result["models"]), 2)
        self.assertEqual(self.server.received[0][1]["X-Api-Key"], "private-model-list-key")
        self.assertEqual(self.server.received[0][1]["Anthropic-Version"], "2023-06-01")
        self.assertEqual(parse_qs(urlsplit(self.server.received[1][0]).query)["after_id"], ["claude-new"])

    def test_mimo_key_header_applies_to_discovery_and_generation(self):
        self.config.credentials["key"]["keyHeader"] = "api-key"
        self.reply({"data": [{"id": "mimo-future"}]}); self.catalog.list(self.profile)
        ProviderAdapters(self.auth).generate(self.profile, [{"role": "user", "content": "hello"}], "text")
        for request in self.server.received:
            self.assertEqual(request[1]["Api-Key"], "private-model-list-key")
            self.assertNotIn("Authorization", request[1])

    def test_zen_list_and_generation_route_each_family(self):
        self.profile["protocol"] = "zen"
        self.reply({"data": [{"id": name} for name in ("gpt-future", "claude-future", "new-chat-model", "jev-1.13")]})
        listed = self.catalog.list(self.profile)
        self.assertEqual({m["id"]: m["protocol"] for m in listed["models"]}, {"gpt-future": "responses", "claude-future": "messages", "new-chat-model": "chat_completions"})
        adapter = ProviderAdapters(self.auth)
        for name, suffix, header in (("gpt-future", "responses", "Authorization"), ("claude-future", "messages", "X-Api-Key"), ("new-chat-model", "chat/completions", "Authorization")):
            result = adapter.generate(dict(self.profile, model=name), [{"role": "user", "content": "hello"}], "text")
            self.assertEqual(result["text"], "hello")
            self.assertEqual(self.server.received[-1][0], "/v1/" + suffix)
            self.assertIn(header, self.server.received[-1][1])
        with self.assertRaises(HubError): adapter.generate(dict(self.profile, model="jev-1.13"), [{"role": "user", "content": "hello"}], "text")

    def test_zen_additional_documented_families_and_explicit_override(self):
        self.profile["protocol"] = "zen"
        adapter = ProviderAdapters(self.auth)
        for name, suffix in (("grok-4.7", "responses"), ("muse-spark-1.3", "responses"), ("qwen3.8-flash", "messages")):
            adapter.generate(dict(self.profile, model=name), [{"role": "user", "content": "hello"}], "text")
            self.assertEqual(self.server.received[-1][0], "/v1/" + suffix)
        adapter.generate(dict(self.profile, model="qwen-custom-chat", protocol="chat_completions"), [{"role": "user", "content": "hello"}], "text")
        self.assertEqual(self.server.received[-1][0], "/v1/chat/completions")

    def test_http_errors_never_echo_body_or_change_manual_route(self):
        with tempfile.TemporaryDirectory() as directory:
            hub = Hub(self.config, directory, self.store)
            hub.select({"profile": "one", "model": "unlisted-manual-model"})
            for status in (401, 403, 404, 429, 500):
                self.reply(b"private-model-list-key", status)
                result = hub.models("one", True)
                self.assertEqual(result["modelsStatus"], "models_http_error")
                self.assertNotIn("private-model-list-key", json.dumps(result))
                self.assertEqual(hub.resolve()[1]["model"], "unlisted-manual-model")

    def test_redirect_is_not_followed(self):
        self.reply({}, 302, {"Location": self.base + "/stolen-key"})
        with self.assertRaises(HubError): self.catalog.list(self.profile)
        self.assertEqual(len(self.server.received), 1)

    def test_repeated_pagination_cursor_is_rejected(self):
        for _ in range(2): self.reply({"data": [{"id": "same"}], "has_more": True, "last_id": "same"})
        with self.assertRaises(HubError): self.catalog.list(self.profile)
        self.assertEqual(len(self.server.received), 2)

    def test_invalid_json_schema_and_secret_echo_are_rejected(self):
        for value in (b"not json", [], {}, {"data": "wrong"}, {"data": [{"id": "private-model-list-key"}]}, {"data": [{"id": "bad\nname"}]}):
            self.reply(value)
            with self.assertRaises(HubError) as error: self.catalog.list(self.profile, True)
            self.assertEqual(error.exception.code, "models_invalid_response")

    def test_catalog_size_and_entry_limits(self):
        for value in (b" " * 2000001, {"data": [{"id": str(i)} for i in range(5001)]}):
            self.reply(value)
            with self.assertRaises(HubError) as error: self.catalog.list(self.profile, True)
            self.assertEqual(error.exception.code, "models_limit")

    def test_manual_only_skips_all_network_requests(self):
        self.profile["modelsFormat"] = "none"
        with self.assertRaises(HubError) as error: self.catalog.list(self.profile)
        self.assertEqual(error.exception.code, "models_unsupported")
        self.assertEqual(self.server.received, [])


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.original = Configuration(configuration())
        self.store = MemoryStore(); self.store.put("key", {"apiKey": "original-private-key"})
        self.hub = Hub(self.original, self.directory.name, self.store)

    def test_all_presets_persist_without_replacing_legacy_profiles_or_selection(self):
        before = copy.deepcopy(self.original.data)
        self.hub.select({"clientId": "OldMod", "profile": "one", "model": "legacy-manual-model"})
        for preset in PRESETS:
            request = {"preset": preset, "id": preset + "-test"}
            if preset == "custom": request.update(baseUrl="https://custom.example/api/v9", modelsFormat="none")
            self.hub.add_profile(request)
            self.hub.set_api_key(preset + "-test-key", "new-private-key-" + preset)
        reloaded = Hub(Configuration(before), self.directory.name, self.store)
        self.assertEqual(len(reloaded.config.profiles), 8)
        self.assertEqual(reloaded.resolve("OldMod")[1]["model"], "legacy-manual-model")
        self.assertEqual(reloaded.resolve()[0], "one")
        self.assertEqual(self.original.data, before)
        self.assertEqual(reloaded.auth.status("key"), "ready")
        self.assertNotIn("private-key", reloaded.profile_path.read_text())

    def test_existing_ids_and_invalid_custom_urls_do_not_persist(self):
        for request in ({"preset": "openai", "id": "one"}, {"preset": "custom", "id": "invalid", "baseUrl": "http://external.example/v1"},
                        {"preset": "custom", "id": "invalid", "baseUrl": "https://custom.example/v1?api_key=secret"},
                        {"preset": "openai", "id": "invalid", "baseUrl": "https://other.example/v1"},
                        {"preset": "custom", "id": "invalid", "baseUrl": "https://custom.example/v1", "protocol": "unknown"}):
            with self.assertRaises(HubError): self.hub.add_profile(request)
        self.assertFalse(self.hub.profile_path.exists())

    def test_same_origin_rule_for_models_endpoint(self):
        value = configuration()
        value["profiles"]["one"]["modelsUrl"] = "https://unrelated.example/models"
        with self.assertRaises(HubError): Configuration(value)

    def test_credentials_cannot_inject_arbitrary_headers(self):
        value = configuration(); value["credentials"]["key"]["keyHeader"] = "bad\r\nInjected"
        with self.assertRaises(HubError): Configuration(value)

    def test_new_example_is_valid_and_preserves_oauth_placeholder(self):
        path = Path(__file__).resolve().parents[1] / "examples/hub.example.json"
        config = Configuration.from_file(path)
        self.assertTrue({"deepseek-api", "mimo-api", "gemini-api", "zen-api"}.issubset(config.profiles))
        self.assertFalse(config.profiles["independent-oauth"]["enabled"])

    def test_save_failure_leaves_running_config_untouched(self):
        with patch("ksp_aihub.hub.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError): self.hub.add_profile({"preset": "deepseek", "id": "new"})
        self.assertNotIn("new", self.hub.config.profiles)
        self.assertNotIn("new-key", self.hub.auth.config.credentials)


class ModelHttpTests(unittest.TestCase):
    setUp = fixtures.HubHttpTests.setUp
    close = fixtures.HubHttpTests.close
    request = fixtures.HubHttpTests.request

    def test_discovery_onboarding_and_auth_are_available_over_real_http(self):
        for path, body in (("/v1/models?profile=one", None), ("/v1/profiles", {"preset": "deepseek", "id": "new"})):
            with self.assertRaises(HTTPError): self.request(path, body, token=False)
        result = self.request("/v1/profiles", {"preset": "deepseek", "id": "new"})
        self.assertEqual(result["profile"], "new")
        self.request("/v1/auth/api-key", {"credential": "new-key", "apiKey": "http-secret-key"})
        with patch.object(self.hub.catalog, "_fetch", return_value={"models": [{"id": "future-model", "label": "Future", "protocol": "chat_completions"}], "fetchedAt": 1000, "cached": False}):
            result = self.request("/v1/models?profile=new&refresh=true")
        self.assertEqual(result["modelIds"], ["future-model"])
        self.assertEqual(result["modelsStatus"], "ready")
        self.assertNotIn("http-secret-key", json.dumps(result))
        self.assertIn("gemini", self.request("/v1/ui")["presetIds"])
        self.assertEqual(self.hub.resolve()[0], "one")

    def test_bad_discovery_query_rejected_before_network(self):
        for path in ("/v1/models", "/v1/models?profile=one&refresh=1", "/v1/models?profile=one&url=https://other.example", "/v1/models?profile=one&profile=two"):
            with self.assertRaises(HTTPError): self.request(path)


if __name__ == "__main__": unittest.main()
