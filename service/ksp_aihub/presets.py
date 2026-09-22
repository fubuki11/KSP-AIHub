"""Connection presets, not a baked-in list of model names."""
import copy

from .common import HubError, endpoint, identifier, origin


PRESETS = {
    "openai": {"label": "OpenAI", "provider": "openai", "protocol": "responses", "baseUrl": "https://api.openai.com/v1"},
    "anthropic": {"label": "Anthropic", "provider": "anthropic", "protocol": "messages", "baseUrl": "https://api.anthropic.com/v1", "modelsFormat": "anthropic"},
    "deepseek": {"label": "DeepSeek", "provider": "openai_compatible", "protocol": "chat_completions", "baseUrl": "https://api.deepseek.com/v1", "reasoningEffort": "low"},
    "mimo": {"label": "Xiaomi MiMo", "provider": "openai_compatible", "protocol": "chat_completions", "baseUrl": "https://api.xiaomimimo.com/v1", "keyHeader": "api-key"},
    "gemini": {"label": "Google Gemini", "provider": "openai_compatible", "protocol": "chat_completions", "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai",
               "modelsUrl": "https://generativelanguage.googleapis.com/v1beta/models", "modelsFormat": "gemini", "reasoningEffort": "low"},
    "zen": {"label": "OpenCode Zen", "provider": "openai_compatible", "protocol": "zen", "baseUrl": "https://opencode.ai/zen/v1"},
    "custom": {"label": "Custom compatible API", "provider": "openai_compatible", "protocol": "chat_completions", "baseUrl": ""},
}


def connection(request):
    if not isinstance(request, dict) or set(request) - {"preset", "id", "baseUrl", "protocol", "modelsFormat"}:
        raise HubError("invalid_request", "Expected preset, profile id and optional custom connection fields.")
    preset = request.get("preset")
    if preset not in PRESETS:
        raise HubError("invalid_request", "Unknown connection preset.")
    name = identifier(request.get("id"), "profile ID")
    if len(name) > 64:
        raise HubError("invalid_request", "New profile IDs must be at most 64 characters.")
    profile = copy.deepcopy(PRESETS[preset])
    header = profile.pop("keyHeader", "auto")
    if "baseUrl" in request:
        if preset != "custom":
            raise HubError("invalid_request", "Use the custom preset for third-party endpoints.")
        profile["baseUrl"] = request["baseUrl"]
    profile["baseUrl"] = endpoint(profile["baseUrl"])
    if "protocol" in request:
        profile["protocol"] = request["protocol"]
    if "modelsFormat" in request:
        profile["modelsFormat"] = request["modelsFormat"]
    profile.update(model="set-your-model-id", credential=name + "-key")
    credential = {"kind": "api_key_store", "provider": profile["provider"], "allowedOrigins": [origin(profile["baseUrl"])], "keyHeader": header}
    return name, profile, credential


def wire_protocol(profile):
    if profile["protocol"] != "zen":
        return profile["protocol"]
    # Zen publishes a common catalog but serves these families on distinct endpoints.
    # The explicit protocol options remain available for new/nonstandard families.
    name = profile["model"].lower()
    if name.startswith(("claude-", "qwen")):
        return "messages"
    if name.startswith(("gpt-", "grok-", "muse-", "o1", "o3", "o4")):
        return "responses"
    if name.startswith("jev-"):
        raise HubError("model_unsupported", "Zen Jev uses the System One protocol, not text generation. Choose a text model.", 409)
    return "chat_completions"
