import copy
from pathlib import Path
import threading
import uuid

from .auth import AuthBroker
from .common import HubError, identifier, loads, messages
from .config import Configuration, GENERATION_FIELDS
from .models import ModelCatalog
from .presets import PRESETS, connection
from .providers import ProviderAdapters
from .store import SecretStore, atomic_json


class Hub:
    def __init__(self, config, state_directory, store=None, adapters=None):
        self.directory = Path(state_directory)
        self.profile_path = self.directory / "configured-profiles.json"
        self.configured = {"schemaVersion": 1, "profiles": {}, "credentials": {}}
        if self.profile_path.exists():
            value = loads(self.profile_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or set(value) != {"schemaVersion", "profiles", "credentials"} or value["schemaVersion"] != 1:
                raise HubError("invalid_configuration", "Invalid configured profile store.")
            if not all(isinstance(value[k], dict) for k in ("profiles", "credentials")):
                raise HubError("invalid_configuration", "Invalid configured profile entries.")
            combined = copy.deepcopy(config.data)
            for key in ("profiles", "credentials"):
                if set(value[key]) & set(combined.get(key, {})):
                    raise HubError("invalid_configuration", "A managed profile/credential conflicts with hub.json; rename the duplicate.")
                combined.setdefault(key, {}).update(value[key])
            config = Configuration(combined)
            self.configured = value
        self.config = config
        self.generation_path = self.directory / "generation-settings.json"
        self.generation_settings = {"schemaVersion": 1, "profiles": {}}
        if self.generation_path.exists():
            value = loads(self.generation_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or set(value) != {"schemaVersion", "profiles"} or value["schemaVersion"] != 1 or not isinstance(value["profiles"], dict):
                raise HubError("invalid_configuration", "Invalid generation settings store.")
            data = copy.deepcopy(config.data)
            for name, settings in value["profiles"].items():
                if name not in config.profiles or not isinstance(settings, dict) or set(settings) - GENERATION_FIELDS:
                    raise HubError("invalid_configuration", "Generation settings refer to an unknown profile or option.")
                data["profiles"][name].update(settings)
            config = Configuration(data)
            self.generation_settings = value
            self.config = config
        self.selection_path = self.directory / "selections.json"
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(2)
        self.auth = AuthBroker(config, store or SecretStore(self.directory / "credentials"))
        self.adapters = adapters or ProviderAdapters(self.auth)
        self.catalog = ModelCatalog(self.auth)
        # Runtime overrides are distinct from configured per-client defaults.
        # None is a persistent "inherit global" tombstone, including for config clients.
        self.selections = {}
        self.global_selection = None
        self.known_clients = set(config.clients)
        if self.selection_path.exists():
            value = loads(self.selection_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict): raise HubError("invalid_configuration", "Invalid selection store.")
            if type(value.get("schemaVersion")) is int:
                if value["schemaVersion"] != 2 or set(value) != {"schemaVersion", "global", "clients"} or not isinstance(value["clients"], dict):
                    raise HubError("invalid_configuration", "Unsupported selection store schema.")
                self.global_selection = self._stored_selection(value["global"])
                entries = value["clients"]
            else:
                entries = value  # v0.1 flat client map; migrate only on the next explicit save.
            for client, selection in entries.items():
                identifier(client)
                self.selections[client] = self._stored_selection(selection)
                self.known_clients.add(client)

    @staticmethod
    def _stored_selection(value):
        if value is None: return None
        if not isinstance(value, dict) or set(value) - {"profile", "model"} or "profile" not in value:
            raise HubError("invalid_configuration", "Invalid stored selection.")
        identifier(value["profile"], "profile ID")
        if value.get("model") is not None: Configuration.model(value["model"])
        # Preserve unavailable profile references instead of silently changing providers.
        return copy.deepcopy(value)

    @staticmethod
    def _client_id(client):
        return None if client is None else identifier(client, "client ID")

    def _selection(self, client):
        if client is not None:
            if len(self.known_clients) < 256: self.known_clients.add(client)
            if client in self.selections:
                if self.selections[client] is not None: return self.selections[client], "client_override"
            elif client in self.config.clients:
                return self.config.clients[client], "client_configuration"
        if self.global_selection is not None: return self.global_selection, "global_default"
        return {"profile": self.config.default_profile}, "configuration_default"

    def resolve(self, client=None, profile=None, model=None):
        client = self._client_id(client)
        if profile is not None: identifier(profile, "profile ID")
        if model is not None: self.config.model(model)
        with self.lock:
            choice, _ = self._selection(client)
            profile_id = profile if profile is not None else choice.get("profile")
            if profile_id not in self.config.profiles or not self.config.profiles[profile_id].get("enabled", True):
                raise HubError("profile_unavailable", "Choose an enabled global profile or a valid consumer override.", 409)
            value = copy.deepcopy(self.config.profiles[profile_id])
            selected_model = choice.get("model") if choice.get("profile") == profile_id else None
            value["model"] = self.config.model(model if model is not None else selected_model or value["model"])
            return profile_id, value

    def profiles(self):
        result = []
        for name, profile in self.config.profiles.items():
            credential = self.config.credentials[profile["credential"]]
            result.append({"id": name, "label": profile.get("label", name), "provider": profile["provider"], "protocol": profile["protocol"],
                           "model": profile["model"], "enabled": profile.get("enabled", True), "credential": profile["credential"],
                           "authKind": credential["kind"], "authState": self.auth.status(profile["credential"]),
                           "text": True, "jsonObject": True, "tools": False, "images": False})
        return {"ok": True, "apiVersion": 1, "profiles": result}

    def add_profile(self, request):
        name, profile, credential = connection(request)
        with self.lock:
            if name in self.config.profiles or profile["credential"] in self.config.credentials:
                raise HubError("profile_exists", "That profile ID already exists. Select it or choose a new ID.", 409)
            if len(self.config.profiles) >= 128:
                raise HubError("profile_limit", "At most 128 profiles can be configured.", 409)
            data = copy.deepcopy(self.config.data)
            data.setdefault("profiles", {})[name] = profile
            data.setdefault("credentials", {})[profile["credential"]] = credential
            config = Configuration(data)
            managed = copy.deepcopy(self.configured)
            managed["profiles"][name] = profile
            managed["credentials"][profile["credential"]] = credential
            atomic_json(self.profile_path, managed)
            self.auth.extend_configuration(config)
            self.config, self.configured = config, managed
        return {"ok": True, "profile": name}

    def set_api_key(self, credential, value):
        self.auth.set_api_key(credential, value)
        self.catalog.invalidate(credential)

    def set_generation(self, request):
        if not isinstance(request, dict) or set(request) != {"profile", "settings"}:
            raise HubError("invalid_request", "Expected profile and generation settings.")
        name, settings = request["profile"], request["settings"]
        identifier(name, "profile ID")
        if not isinstance(settings, dict) or not settings or set(settings) - GENERATION_FIELDS:
            raise HubError("invalid_request", "Invalid generation settings fields.")
        with self.lock:
            if name not in self.config.profiles: raise HubError("profile_unavailable", "Unknown profile.", 404)
            data = copy.deepcopy(self.config.data)
            data["profiles"][name].update(settings)
            config = Configuration(data)
            saved = copy.deepcopy(self.generation_settings)
            saved["profiles"][name] = {k: config.profiles[name][k] for k in GENERATION_FIELDS}
            atomic_json(self.generation_path, saved)
            self.auth.extend_configuration(config)
            self.config, self.generation_settings = config, saved
        return {"ok": True}

    def models(self, profile_id, refresh=False):
        identifier(profile_id, "profile ID")
        _, profile = self.resolve(profile=profile_id)
        try:
            result = self.catalog.list(profile, refresh)
            models = result["models"]
            return {"ok": True, "profile": profile_id, "modelIds": [m["id"] for m in models],
                    "modelLabels": [m["label"] + " | " + m["id"] + " | " + m["protocol"] for m in models],
                    "modelProtocols": [m["protocol"] for m in models], "cached": result["cached"], "fetchedAt": result["fetchedAt"],
                    "modelsStatus": "ready", "modelsMessage": "" if models else "The endpoint returned no compatible text models. You can still enter an ID manually."}
        except HubError as error:
            # Discovery is optional. Never remove a chosen/manual model because listing failed.
            return {"ok": True, "profile": profile_id, "modelsStatus": error.code, "modelsMessage": str(error),
                    "modelIds": [], "modelLabels": [], "modelProtocols": [], "cached": False, "fetchedAt": 0}

    def ui(self, client=None):
        client = self._client_id(client)
        rows = [p for p in self.profiles()["profiles"] if p["enabled"]]
        with self.lock:
            _, source = self._selection(client)
            value = {"ok": True, "scope": "global" if client is None else "client", "clientId": client,
                     "selectionSource": source, "inheritsGlobal": client is not None and source in ("global_default", "configuration_default"),
                     "hasOverride": source in ("client_override", "client_configuration"),
                     "clientIds": sorted(self.known_clients)[:256],
                     "profileIds": [p["id"] for p in rows],
                      "profileLabels": [f'{p["label"]} [{p["id"]}] | {p["provider"]} | {p["authState"]}' for p in rows]}
            value.update(presetIds=list(PRESETS), presetLabels=[p["label"] for p in PRESETS.values()])
            try:
                profile_id, profile = self.resolve(client)
                value.update(routingReady=True, selectedProfile=profile_id, model=profile["model"],
                              credential=profile["credential"], authState=self.auth.status(profile["credential"]),
                              authKind=self.config.credentials[profile["credential"]]["kind"], routeError="",
                              baseUrl=profile["baseUrl"], protocol=profile["protocol"])
                value.update(maxOutputTokens=profile["maxOutputTokens"], recoveryMaxOutputTokens=profile["recoveryMaxOutputTokens"],
                             timeoutSeconds=profile["timeout"], streamEnabled=profile["stream"], reasoningEffort=profile["reasoningEffort"], thinkingMode=profile["thinkingMode"],
                             providerManagedOutput=profile.get("flavor") == "codex" or self.config.credentials[profile["credential"]]["kind"] == "opencode_oauth")
            except HubError as error:
                if error.code != "profile_unavailable": raise
                value.update(routingReady=False, selectedProfile="", model="", credential="", authKind="", authState="unconfigured", routeError=str(error))
            return value

    def select(self, request):
        if not isinstance(request, dict) or set(request) - {"scope", "clientId", "profile", "model", "inherit"}:
            raise HubError("invalid_request", "Invalid selection request.")
        scope = request.get("scope", "client" if request.get("clientId") is not None else "global")
        if scope not in ("global", "client"):
            raise HubError("invalid_request", "scope must be global or client.")
        if scope == "global" and request.get("clientId") is not None:
            raise HubError("invalid_request", "Global selection must not include clientId.")
        client = identifier(request.get("clientId"), "client ID") if scope == "client" else None
        inherit = request.get("inherit", False)
        if type(inherit) is not bool or (inherit and ("profile" in request or "model" in request)):
            raise HubError("invalid_request", "inherit=true must not include profile/model.")
        selection = None if inherit else copy.deepcopy(self.config.selection({key: request[key] for key in ("profile", "model") if key in request}))
        with self.lock:
            updated = dict(self.selections)
            global_selection = self.global_selection
            if scope == "global": global_selection = selection
            else: updated[client] = selection
            atomic_json(self.selection_path, {"schemaVersion": 2, "global": global_selection, "clients": updated})
            self.selections = updated
            self.global_selection = global_selection
        return self.ui(client)

    def generate(self, request):
        if not isinstance(request, dict) or set(request) - {"clientId", "profile", "model", "format", "messages", "recovery"}:
            raise HubError("invalid_request", "Invalid generation request fields.")
        profile_id, profile = self.resolve(request.get("clientId"), request.get("profile"), request.get("model"))
        recovery = request.get("recovery", False)
        if type(recovery) is not bool: raise HubError("invalid_request", "recovery must be a boolean.")
        if recovery: profile["maxOutputTokens"] = min(profile["maxOutputTokens"] * 2, profile["recoveryMaxOutputTokens"])
        output_format = request.get("format", "text")
        if output_format not in ("text", "json"):
            raise HubError("invalid_request", "format must be text or json.")
        conversation = messages(request.get("messages"))
        if not self.slots.acquire(blocking=False):
            raise HubError("busy", "The gateway is handling its maximum concurrent generations.", 429)
        try:
            result = self.adapters.generate(profile, conversation, output_format)
            return {"ok": True, "requestId": uuid.uuid4().hex, "profile": profile_id, "provider": profile["provider"], "model": profile["model"], **result}
        finally:
            self.slots.release()
