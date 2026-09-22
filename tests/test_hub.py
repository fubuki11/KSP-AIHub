import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from unittest.mock import patch

from ksp_aihub.auth import AuthBroker
from ksp_aihub.common import HubError, loads
from ksp_aihub.config import Configuration
from ksp_aihub.hub import Hub
from ksp_aihub.providers import ProviderAdapters
from ksp_aihub.server import GatewayServer
from ksp_aihub.store import SecretStore, StateLock


class MemoryStore:
    def __init__(self): self.values = {}
    def get(self, key): return copy.deepcopy(self.values.get(key))
    def put(self, key, value): self.values[key] = copy.deepcopy(value)


def configuration(base="http://127.0.0.1:19999", port=18181):
    return {"schemaVersion": 1, "port": port, "defaultProfile": "one", "clients": {}, "oauthRegistrations": {},
            "credentials": {"key": {"kind": "api_key_store", "provider": "openai", "allowedOrigins": [base]}},
            "profiles": {"one": {"provider": "openai", "protocol": "chat_completions", "baseUrl": base + "/v1",
                                  "model": "model-a", "credential": "key", "timeout": 5}}}


class ConfigTests(unittest.TestCase):
    def test_state_directory_has_only_one_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            with StateLock(directory):
                with self.assertRaises(HubError):
                    with StateLock(directory): pass
            with StateLock(directory): pass

    def test_protocol_and_credentials_are_separate(self):
        config = configuration()
        config["profiles"]["two"] = {**config["profiles"]["one"], "model": "model-b"}
        loaded = Configuration(config)
        self.assertEqual(loaded.profiles["two"]["credential"], "key")

    def test_credential_cannot_move_to_another_host(self):
        value = configuration()
        value["profiles"]["one"]["baseUrl"] = "https://other.example/v1"
        with self.assertRaises(HubError): Configuration(value)

    def test_no_plaintext_keys_in_config(self):
        value = configuration()
        value["credentials"]["key"]["apiKey"] = "do-not-store-here"
        with self.assertRaises(HubError): Configuration(value)

    def test_anthropic_oauth_is_explicitly_unavailable(self):
        value = configuration()
        value["credentials"]["key"].update(kind="oauth_managed", provider="anthropic", registration="unavailable")
        value["profiles"]["one"].update(provider="anthropic", protocol="messages")
        broker = AuthBroker(Configuration(value), MemoryStore())
        self.assertEqual(broker.status("key"), "auth_unsupported")
        with self.assertRaises(HubError) as error: broker.begin("key")
        self.assertEqual(error.exception.code, "auth_unsupported")

    def test_json_duplicate_and_nonfinite_rejected(self):
        for text in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}'):
            with self.assertRaises(HubError): loads(text)


class OAuthTests(unittest.TestCase):
    def setUp(self):
        value = configuration()
        value["credentials"]["key"].update(kind="oauth_managed", registration="own", provider="openai_compatible")
        value["profiles"]["one"]["provider"] = "openai_compatible"
        value["oauthRegistrations"]["own"] = {"enabled": True, "clientId": "registered-test-client",
            "authorizationUrl": "https://issuer.example/authorize", "tokenUrl": "https://issuer.example/token",
            "redirectUri": "http://127.0.0.1:18181/oauth/callback", "scopes": ["inference"],
            "allowedApiOrigins": ["http://127.0.0.1:19999"]}
        self.clock = [1000]
        self.store = MemoryStore()
        self.broker = AuthBroker(Configuration(value), self.store, clock=lambda: self.clock[0])

    def test_pkce_callback_and_state_are_one_time(self):
        started = self.broker.begin("key")
        query = parse_qs(urlsplit(started["authorizationUrl"]).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertNotIn("verifier", started["authorizationUrl"])
        with patch.object(self.broker, "_token_request", return_value=({"access_token": "access-1", "refresh_token": "refresh-1"}, 2000)) as exchange:
            self.broker.callback(query["state"][0], "authorization-code")
        self.assertEqual(exchange.call_args.args[1]["grant_type"], "authorization_code")
        self.assertIn("code_verifier", exchange.call_args.args[1])
        self.assertEqual(self.broker.status("key"), "ready")
        with self.assertRaises(HubError): self.broker.callback(query["state"][0], "authorization-code")

    def test_state_expiry_and_config_changes(self):
        query = parse_qs(urlsplit(self.broker.begin("key")["authorizationUrl"]).query)
        self.clock[0] += 601
        with self.assertRaises(HubError): self.broker.callback(query["state"][0], "code")
        query = parse_qs(urlsplit(self.broker.begin("key")["authorizationUrl"]).query)
        self.broker.config.registrations["own"]["clientId"] = "changed"
        with self.assertRaises(HubError): self.broker.callback(query["state"][0], "code")

    def test_new_login_invalidates_old_tab(self):
        old = parse_qs(urlsplit(self.broker.begin("key")["authorizationUrl"]).query)["state"][0]
        self.broker.begin("key")
        with self.assertRaises(HubError): self.broker.callback(old, "code")

    def test_refresh_rotation_is_saved_and_not_repeated(self):
        registration = self.broker.registration(self.broker.credential("key"))
        self.store.put("key", {"access": "expired", "refresh": "refresh-old", "expires": 900, "binding": self.broker.fingerprint(registration)})
        with patch.object(self.broker, "_token_request", return_value=({"access_token": "access-new", "refresh_token": "refresh-new"}, 2000)) as exchange:
            first = self.broker.headers(self.broker.config.profiles["one"])
            second = self.broker.headers(self.broker.config.profiles["one"])
        self.assertEqual(exchange.call_count, 1)
        self.assertEqual(first[0]["Authorization"], "Bearer access-new")
        self.assertEqual(second[1], "access-new")
        self.assertEqual(self.store.get("key")["refresh"], "refresh-new")

    def test_registration_is_required_not_borrowed(self):
        self.broker.config.registrations["own"]["enabled"] = False
        self.assertEqual(self.broker.status("key"), "oauth_registration_missing")

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI test")
    def test_dpapi_round_trip_is_not_plaintext(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SecretStore(directory)
            store.put("test", {"apiKey": "unique-dpapi-test-secret"})
            self.assertEqual(store.get("test")["apiKey"], "unique-dpapi-test-secret")
            self.assertNotIn(b"unique-dpapi-test-secret", next(Path(directory).glob("*.json")).read_bytes())


class MockProvider(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.received.append((self.path, dict(self.headers), json.loads(body)))
        self.send_response(self.server.status)
        self.send_header("Content-Type", self.server.content_type)
        self.end_headers()
        self.wfile.write(self.server.reply)
    def log_message(self, *args): pass


class ProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()
    def setUp(self):
        self.server.received = []; self.server.status = 200; self.server.content_type = "application/json"
        self.config = Configuration(configuration(f"http://127.0.0.1:{self.server.server_port}"))
        self.store = MemoryStore(); self.store.put("key", {"apiKey": "secret-provider-key"})
        self.adapter = ProviderAdapters(AuthBroker(self.config, self.store))
        self.messages = [{"role": "system", "content": "system rule"}, {"role": "user", "content": "你好"}]

    def test_openai_chat_json_and_usage(self):
        self.server.reply = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}],
                                       "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
        result = self.adapter.generate(self.config.profiles["one"], self.messages, "json")
        path, headers, request = self.server.received[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer secret-provider-key")
        self.assertEqual(json.loads(result["jsonText"]), {"ok": True})
        self.assertEqual(result["inputTokens"], 10)
        self.assertNotIn("secret-provider-key", json.dumps(request))

    def test_anthropic_uses_messages_protocol_and_api_key(self):
        profile = self.config.profiles["one"]; profile.update(provider="anthropic", protocol="messages")
        self.config.credentials["key"]["provider"] = "anthropic"
        self.server.reply = json.dumps({"stop_reason": "end_turn", "content": [{"type": "text", "text": "hello"}], "usage": {"input_tokens": 2, "output_tokens": 1}}).encode()
        result = self.adapter.generate(profile, self.messages, "text")
        path, headers, request = self.server.received[0]
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(headers["X-Api-Key"], "secret-provider-key")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(request["system"], "system rule")
        self.assertEqual(result["text"], "hello")

    def test_responses_stream_reconstructs_empty_aggregate(self):
        self.config.profiles["one"]["protocol"] = "responses"
        self.server.content_type = "text/event-stream"
        events = [{"type": "response.output_text.delta", "delta": '{"ok":true}'},
                  {"type": "response.completed", "response": {"status": "completed", "output": []}}]
        self.server.reply = "".join("data: " + json.dumps(e) + "\n\n" for e in events).encode()
        result = self.adapter.generate(self.config.profiles["one"], self.messages, "json")
        self.assertEqual(json.loads(result["jsonText"]), {"ok": True})

    def test_provider_error_is_not_retried_or_echoed(self):
        self.server.status = 401; self.server.reply = b"secret-provider-key"
        with self.assertRaises(HubError) as error: self.adapter.generate(self.config.profiles["one"], self.messages, "text")
        self.assertNotIn("secret-provider-key", str(error.exception))
        self.assertEqual(len(self.server.received), 1)

    def test_truncated_or_key_echo_output_is_rejected(self):
        for reason, text in [("length", "partial"), ("stop", "secret-provider-key")]:
            self.server.reply = json.dumps({"choices": [{"finish_reason": reason, "message": {"content": text}}]}).encode()
            with self.assertRaises(HubError): self.adapter.generate(self.config.profiles["one"], self.messages, "text")


class HubHttpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        # Reserve an ephemeral port first, then hand it to the real gateway configuration.
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
        value = configuration(port=port); value["profiles"]["two"] = {**value["profiles"]["one"], "model": "model-b"}
        store = MemoryStore(); store.put("key", {"apiKey": "secret-test-key"})
        class Adapter:
            def generate(self, profile, messages, mode):
                return {"text": "{}", "jsonText": "{}" if mode == "json" else "", "inputTokens": 1, "outputTokens": 1}
        self.hub = Hub(Configuration(value), temporary.name, store, Adapter())
        self.token = "test-hub-ipc-token-at-least-32-characters"
        self.server = GatewayServer(self.hub, self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.close)
        self.url = f"http://127.0.0.1:{port}"

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def request(self, path, body=None, token=True, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        if token: h["Authorization"] = "Bearer " + self.token
        req = Request(self.url + path, data=None if body is None else json.dumps(body).encode(), headers=h)
        with urlopen(req, timeout=5) as response: return json.load(response)

    def test_health_requires_ipc_token_and_rejects_browser_origin(self):
        self.assertTrue(self.request("/v1/health")["ok"])
        for kwargs in ({"token": False}, {"headers": {"Origin": "https://example.org"}}, {"headers": {"Host": "example.org"}}):
            with self.assertRaises(HTTPError): self.request("/v1/health", **kwargs)

    def test_selection_is_per_mod_and_generation_uses_snapshot(self):
        self.request("/v1/select", {"clientId": "KSPAutoCraft", "profile": "two", "model": "custom-model"})
        self.assertEqual(self.request("/v1/ui?clientId=KSPAutoCraft")["model"], "custom-model")
        self.assertEqual(self.request("/v1/ui?clientId=AnotherMod")["model"], "model-a")
        result = self.request("/v1/generate", {"clientId": "KSPAutoCraft", "format": "json", "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual((result["profile"], result["model"], result["jsonText"]), ("two", "custom-model", "{}"))

    def test_client_cannot_inject_endpoint_or_auth(self):
        for field in ("baseUrl", "credential", "Authorization"):
            with self.assertRaises(HTTPError):
                self.request("/v1/generate", {"clientId": "test", "messages": [{"role": "user", "content": "hello"}], field: "forbidden"})

    def test_profiles_never_return_secrets(self):
        value = self.request("/v1/profiles")
        self.assertNotIn("secret-test-key", json.dumps(value))
        self.assertEqual(value["profiles"][0]["authState"], "ready")

    def test_default_ui_and_anonymous_request_use_global_scope(self):
        ui = self.request("/v1/ui")
        self.assertEqual(ui["scope"], "global")
        self.assertIsNone(ui["clientId"])
        self.request("/v1/select", {"scope": "global", "profile": "two", "model": "shared-model"})
        result = self.request("/v1/generate", {"messages": [{"role": "user", "content": "test"}]})
        self.assertEqual(result["model"], "shared-model")
        self.assertTrue(self.request("/v1/ui?clientId=UnregisteredMod")["inheritsGlobal"])

    def test_http_override_reset_returns_to_global(self):
        self.request("/v1/select", {"clientId": "ModA", "profile": "two"})
        self.request("/v1/select", {"scope": "client", "clientId": "ModA", "inherit": True})
        ui = self.request("/v1/ui?clientId=ModA")
        self.assertTrue(ui["inheritsGlobal"])
        self.assertEqual(ui["model"], "model-a")
        with self.assertRaises(HTTPError): self.request("/v1/ui?clientId=")


if __name__ == "__main__": unittest.main()
