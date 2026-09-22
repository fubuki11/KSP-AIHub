import copy
from pathlib import Path

from .common import HubError, endpoint, identifier, loads, origin, positive


PROTOCOLS = {"openai": {"responses", "chat_completions"}, "openai_compatible": {"responses", "chat_completions", "messages", "zen"}, "anthropic": {"messages"}}
AUTH_KINDS = {"api_key_env", "api_key_store", "oauth_managed", "opencode_oauth"}
GENERATION_FIELDS = {"maxOutputTokens", "recoveryMaxOutputTokens", "timeout", "stream", "reasoningEffort", "thinkingMode"}
EFFORTS = ("provider_default", "none", "minimal", "low", "medium", "high", "xhigh", "max")


class Configuration:
    def __init__(self, value):
        if not isinstance(value, dict) or value.get("schemaVersion") != 1:
            raise HubError("invalid_configuration", "Expected configuration schemaVersion 1.")
        if set(value) - {"schemaVersion", "port", "defaultProfile", "clients", "profiles", "credentials", "oauthRegistrations"}:
            raise HubError("invalid_configuration", "Unknown configuration field.")
        self.data = copy.deepcopy(value)
        self.port = value.get("port", 18181)
        if type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise HubError("invalid_configuration", "port must be 1024-65535.")
        self.profiles = self.data.get("profiles", {})
        self.credentials = self.data.get("credentials", {})
        self.registrations = self.data.get("oauthRegistrations", {})
        self.clients = self.data.get("clients", {})
        if not all(isinstance(v, dict) for v in (self.profiles, self.credentials, self.registrations, self.clients)):
            raise HubError("invalid_configuration", "profiles/credentials/registrations/clients must be objects.")
        for name, credential in self.credentials.items():
            identifier(name)
            if not isinstance(credential, dict) or credential.get("kind") not in AUTH_KINDS:
                raise HubError("invalid_configuration", "Unsupported credential kind.")
            if set(credential) - {"kind", "provider", "env", "registration", "authFile", "allowedOrigins", "keyHeader"}:
                raise HubError("invalid_configuration", "Do not put secret values into credential configuration.")
            origins = credential.get("allowedOrigins")
            if not isinstance(origins, list) or not origins:
                raise HubError("invalid_configuration", "Each credential needs explicit allowedOrigins.")
            credential["allowedOrigins"] = [origin(url) for url in origins]
            if credential.get("keyHeader", "auto") not in ("auto", "authorization", "api-key", "x-api-key", "x-goog-api-key"):
                raise HubError("invalid_configuration", "Unsupported API key header.")
            if credential.get("keyHeader", "auto") != "auto" and credential["kind"] not in ("api_key_env", "api_key_store"):
                raise HubError("invalid_configuration", "Custom key headers apply only to API keys.")
            if credential["kind"] == "api_key_env":
                identifier(credential.get("env"), "API key environment variable")
        for name, profile in self.profiles.items():
            identifier(name)
            if not isinstance(profile, dict) or set(profile) - ({"label", "enabled", "provider", "protocol", "baseUrl", "model", "credential", "jsonMode", "flavor", "modelsUrl", "modelsFormat"} | GENERATION_FIELDS):
                raise HubError("invalid_configuration", "Invalid profile fields.")
            provider = profile.get("provider")
            if provider not in PROTOCOLS or profile.get("protocol") not in PROTOCOLS[provider]:
                raise HubError("invalid_configuration", "Provider and protocol do not match.")
            profile["baseUrl"] = endpoint(profile.get("baseUrl"))
            if profile.get("credential") not in self.credentials:
                raise HubError("invalid_configuration", "Profile references an unknown credential.")
            credential = self.credentials[profile["credential"]]
            if credential.get("provider") != provider:
                raise HubError("invalid_configuration", "Credential is bound to another provider.")
            if origin(profile["baseUrl"]) not in credential["allowedOrigins"]:
                raise HubError("invalid_configuration", "Profile endpoint is outside its credential origin binding.")
            profile["modelsFormat"] = profile.get("modelsFormat", "anthropic" if provider == "anthropic" else "openai")
            if profile["modelsFormat"] not in ("openai", "anthropic", "gemini", "none"):
                raise HubError("invalid_configuration", "Unknown model catalog format.")
            profile["modelsUrl"] = endpoint(profile.get("modelsUrl", profile["baseUrl"] + "/models"))
            if origin(profile["modelsUrl"]) != origin(profile["baseUrl"]):
                raise HubError("invalid_configuration", "Model catalog must share its profile's API origin.")
            self.model(profile.get("model"))
            profile["timeout"] = positive(profile.get("timeout", 300), "timeout", 1, 600)
            tokens = profile.get("maxOutputTokens", 16000)
            if type(tokens) is not int or not 128 <= tokens <= 64000:
                raise HubError("invalid_configuration", "maxOutputTokens must be 128-64000.")
            profile["maxOutputTokens"] = tokens
            recovery = profile.get("recoveryMaxOutputTokens", min(64000, max(32000, tokens)))
            if type(recovery) is not int or not tokens <= recovery <= 64000:
                raise HubError("invalid_configuration", "recoveryMaxOutputTokens must be between maxOutputTokens and 64000.")
            profile["recoveryMaxOutputTokens"] = recovery
            profile["stream"] = profile.get("stream", True)
            profile["reasoningEffort"] = profile.get("reasoningEffort", "provider_default")
            profile["thinkingMode"] = profile.get("thinkingMode", "provider_default")
            if type(profile["stream"]) is not bool or profile["reasoningEffort"] not in EFFORTS or profile["thinkingMode"] not in ("provider_default", "enabled", "disabled"):
                raise HubError("invalid_configuration", "Invalid streaming/reasoning settings.")
            if profile["thinkingMode"] != "provider_default" and (profile["protocol"] != "chat_completions" or profile["reasoningEffort"] != "provider_default"):
                raise HubError("invalid_configuration", "Explicit thinkingMode requires Chat Completions and provider_default reasoningEffort.")
            if profile["protocol"] == "messages" and profile["reasoningEffort"] != "provider_default":
                raise HubError("invalid_configuration", "reasoningEffort is supported for Chat Completions/Responses profiles.")
            if type(profile.get("enabled", True)) is not bool or type(profile.get("jsonMode", True)) is not bool:
                raise HubError("invalid_configuration", "enabled/jsonMode must be boolean.")
            if profile.get("flavor", "standard") not in ("standard", "codex"):
                raise HubError("invalid_configuration", "Unknown endpoint flavor.")
            if profile.get("flavor") == "codex" and (provider != "openai" or profile["protocol"] != "responses"):
                raise HubError("invalid_configuration", "Codex flavor requires OpenAI Responses.")
        self.default_profile = value.get("defaultProfile")
        if self.default_profile is not None and self.default_profile not in self.profiles:
            raise HubError("invalid_configuration", "Unknown default profile.")
        for client, selection in self.clients.items():
            identifier(client, "client ID")
            self.selection(selection)

    @classmethod
    def from_file(cls, path):
        return cls(loads(Path(path).read_text(encoding="utf-8-sig")))

    @staticmethod
    def model(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(ord(c) < 32 for c in value):
            raise HubError("invalid_request", "Model ID must be a nonblank bounded string.")
        return value

    def selection(self, selection):
        if not isinstance(selection, dict) or set(selection) - {"profile", "model"}:
            raise HubError("invalid_request", "Invalid profile selection.")
        profile = selection.get("profile")
        identifier(profile, "profile ID")
        if profile not in self.profiles or not self.profiles[profile].get("enabled", True):
            raise HubError("profile_unavailable", "The selected profile is disabled or unknown.")
        if selection.get("model") is not None:
            self.model(selection["model"])
        return selection
