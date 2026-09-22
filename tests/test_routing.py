import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest

from ksp_aihub.common import HubError
from ksp_aihub.config import Configuration
from ksp_aihub.hub import Hub
from test_hub import MemoryStore, configuration


class RoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.value = configuration()
        self.value["profiles"]["two"] = {**self.value["profiles"]["one"], "model": "model-b"}
        self.value["profiles"]["three"] = {**self.value["profiles"]["one"], "model": "model-c"}
        self.store = MemoryStore()
        self.store.put("key", {"apiKey": "test-routing-key"})
        self.hub = self.reload()

    def reload(self, adapters=None):
        return Hub(Configuration(self.value), self.path, self.store, adapters)

    def route(self, client=None, **kwargs):
        name, profile = self.hub.resolve(client, **kwargs)
        return name, profile["model"]

    def test_all_consumers_and_anonymous_requests_inherit_global(self):
        for client in (None, "ModA", "org.example.ModB", "UnregisteredMod"):
            self.assertEqual(self.route(client), ("one", "model-a"))
        self.hub.select({"scope": "global", "profile": "two", "model": "global-model"})
        for client in (None, "ModA", "org.example.ModB", "UnregisteredMod"):
            self.assertEqual(self.route(client), ("two", "global-model"))
        self.assertEqual(self.hub.ui()["scope"], "global")
        self.assertIsNone(self.hub.ui()["clientId"])
        self.assertTrue(self.hub.ui("ModA")["inheritsGlobal"])

    def test_client_override_does_not_change_other_consumers(self):
        self.hub.select({"scope": "global", "profile": "two", "model": "shared"})
        self.hub.select({"scope": "client", "clientId": "ModA", "profile": "one", "model": "special"})
        self.hub.select({"scope": "global", "profile": "three"})
        self.assertEqual(self.route("ModA"), ("one", "special"))
        self.assertEqual(self.route("ModB"), ("three", "model-c"))
        self.assertEqual(self.route(), ("three", "model-c"))
        self.assertTrue(self.hub.ui("ModA")["hasOverride"])

    def test_request_profile_model_take_precedence_without_cross_profile_model_leak(self):
        self.hub.select({"clientId": "ModA", "profile": "one", "model": "only-for-one"})
        self.assertEqual(self.route("ModA", model="request-model"), ("one", "request-model"))
        self.assertEqual(self.route("ModA", profile="two"), ("two", "model-b"))
        self.assertEqual(self.route("ModA", profile="two", model="request-model"), ("two", "request-model"))
        self.assertEqual(self.route("ModA"), ("one", "only-for-one"))

    def test_clear_override_tracks_future_global_changes_after_restart(self):
        self.hub.select({"clientId": "ModA", "profile": "one", "model": "special"})
        self.hub.select({"scope": "client", "clientId": "ModA", "inherit": True})
        self.hub.select({"scope": "global", "profile": "two", "model": "shared"})
        self.hub = self.reload()
        self.assertEqual(self.route("ModA"), ("two", "shared"))
        self.hub.select({"scope": "global", "profile": "three"})
        self.assertEqual(self.route("ModA"), ("three", "model-c"))

    def test_clear_configured_override_is_persistent(self):
        self.value["clients"] = {"ModA": {"profile": "two", "model": "configured-special"}}
        self.hub = self.reload()
        self.assertEqual(self.route("ModA"), ("two", "configured-special"))
        self.hub.select({"scope": "client", "clientId": "ModA", "inherit": True})
        self.hub = self.reload()
        self.assertEqual(self.route("ModA"), ("one", "model-a"))
        self.assertTrue(self.hub.ui("ModA")["inheritsGlobal"])

    def test_global_reset_uses_config_and_preserves_client_override(self):
        self.hub.select({"scope": "global", "profile": "two"})
        self.hub.select({"clientId": "ModA", "profile": "three"})
        self.hub.select({"scope": "global", "inherit": True})
        self.assertEqual(self.route(), ("one", "model-a"))
        self.assertEqual(self.route("ModA"), ("three", "model-c"))

    def test_legacy_file_and_legacy_select_request_are_preserved(self):
        path = self.path / "selections.json"
        original = json.dumps({"LegacyMod": {"profile": "two", "model": "legacy-model"}}).encode()
        path.write_bytes(original)
        self.hub = self.reload()
        self.assertEqual(path.read_bytes(), original, "Loading must not rewrite existing state")
        self.assertEqual(self.route("LegacyMod"), ("two", "legacy-model"))
        self.hub.select({"clientId": "SecondMod", "profile": "three"})
        state = json.loads(path.read_text())
        self.assertEqual(state["schemaVersion"], 2)
        self.assertEqual(state["clients"]["LegacyMod"]["model"], "legacy-model")
        self.hub = self.reload()
        self.assertEqual(self.route("LegacyMod"), ("two", "legacy-model"))

    def test_removed_profile_does_not_silently_fall_back(self):
        self.hub.select({"clientId": "ModA", "profile": "two"})
        del self.value["profiles"]["two"]
        self.hub = self.reload()
        with self.assertRaises(HubError): self.route("ModA")
        status = self.hub.ui("ModA")
        self.assertFalse(status["routingReady"])
        self.assertIn("one", status["profileIds"])
        self.hub.select({"clientId": "ModA", "inherit": True})
        self.assertEqual(self.route("ModA"), ("one", "model-a"))

    def test_missing_global_route_can_be_configured_in_ui(self):
        self.value["defaultProfile"] = None
        self.hub = self.reload()
        self.assertFalse(self.hub.ui()["routingReady"])
        self.assertEqual(self.hub.ui()["profileIds"], ["one", "two", "three"])
        self.hub.select({"scope": "global", "profile": "two"})
        self.assertEqual(self.route(), ("two", "model-b"))

    def test_scope_and_reset_validation(self):
        invalid = [
            {"scope": "global", "clientId": "ModA", "profile": "one"},
            {"scope": "client", "profile": "one"},
            {"scope": "wrong", "profile": "one"},
            {"scope": "global", "inherit": "true"},
            {"scope": "global", "inherit": True, "profile": "one"},
            {"scope": "global", "inherit": True, "model": "x"},
            {"clientId": "", "profile": "one"},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(HubError): self.hub.select(value)
        for client in ("", " ", {}, "../Mod"):
            with self.subTest(client=client), self.assertRaises(HubError): self.route(client)

    def test_known_consumers_are_discovered_without_registration(self):
        self.route("org.vendor.ModA")
        self.route("AnotherMod")
        self.assertEqual(self.hub.ui()["clientIds"], ["AnotherMod", "org.vendor.ModA"])

    def test_framework_runtime_and_default_config_do_not_name_a_consumer_mod(self):
        root = Path(__file__).resolve().parents[1]
        for directory, pattern in ((root / "service/ksp_aihub", "*.py"), (root / "src/KSPAIHub", "*.cs")):
            for path in directory.glob(pattern):
                with self.subTest(path=path.name):
                    self.assertNotIn("KSPAutoCraft", path.read_text(encoding="utf-8"))
        example = json.loads((root / "examples/hub.example.json").read_text())
        self.assertEqual(example["clients"], {})

    def test_inflight_generation_snapshot_is_not_changed_by_global_selection(self):
        entered, release = threading.Event(), threading.Event()
        snapshots, results, errors = [], [], []
        class Adapter:
            def generate(self, profile, messages, output_format):
                entered.set()
                if not release.wait(5): raise RuntimeError("test timeout")
                snapshots.append(copy.deepcopy(profile))
                return {"text": "ok", "jsonText": "", "inputTokens": -1, "outputTokens": -1}
        self.hub = self.reload(Adapter())
        def generate():
            try: results.append(self.hub.generate({"messages": [{"role": "user", "content": "test"}]}))
            except Exception as error: errors.append(error)
        thread = threading.Thread(target=generate); thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.hub.select({"scope": "global", "profile": "two", "model": "later-model"})
        finally:
            release.set(); thread.join(5)
        self.assertFalse(errors)
        self.assertEqual(snapshots[0]["model"], "model-a")
        self.assertEqual(results[0]["model"], "model-a")
        self.assertEqual(self.route(), ("two", "later-model"))


if __name__ == "__main__": unittest.main()
