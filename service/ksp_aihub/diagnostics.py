"""Bounded metadata-only generation records. No prompts, outputs or credentials."""
from datetime import datetime, timezone
from pathlib import Path
import re
import threading

from .store import atomic_json


def safe_details(value):
    if not isinstance(value, dict): return {}
    result = {}
    for key in ("outputLimit", "recoveryLimit", "inputTokens", "outputTokens", "reasoningTokens", "httpStatus"):
        if type(value.get(key)) is int: result[key] = value[key]
    for key in ("requestStreaming", "responseStreaming", "providerManagedOutput", "recoveryAllowed"):
        if type(value.get(key)) is bool: result[key] = value[key]
    reason = value.get("finishReason")
    if reason is None or isinstance(reason, str) and re.fullmatch(r"[A-Za-z0-9_:-]{1,64}", reason):
        result["finishReason"] = reason
    protocol = value.get("protocol")
    if protocol in ("chat_completions", "responses", "messages", "zen"): result["protocol"] = protocol
    return result


class GenerationDiagnostics:
    def __init__(self, directory, limit=100):
        self.directory = Path(directory)
        self.limit = limit
        self.lock = threading.RLock()

    def write(self, request_id, profile_id, profile, *, client_id, output_format, reasons, outcome, details, duration_ms):
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id): return False
        # This record is assembled from a whitelist, never by dumping a request,
        # exception, profile, provider response or model message wholesale.
        value = {"schemaVersion": 1, "requestId": request_id, "createdUtc": datetime.now(timezone.utc).isoformat(),
                 "profile": profile_id, "model": profile["model"], "clientId": client_id,
                 "format": output_format, "protocol": profile["protocol"], "outcome": outcome,
                 "durationMs": duration_ms, "recoveryReasons": reasons,
                 "settings": {k: profile[k] for k in ("maxOutputTokens", "recoveryMaxOutputTokens", "timeout", "stream", "reasoningEffort", "thinkingMode", "repetitionRecovery") if k in profile},
                 "response": safe_details(details)}
        try:
            with self.lock:
                atomic_json(self.directory / ("generation-" + request_id + ".json"), value)
                files = [p for p in self.directory.glob("generation-*.json") if p.is_file() and not p.is_symlink() and re.fullmatch(r"generation-[a-f0-9]{32}\.json", p.name)]
                for old in sorted(files, key=lambda p: p.stat().st_mtime_ns, reverse=True)[self.limit:]: old.unlink()
            return True
        except (OSError, ValueError, TypeError):
            return False  # Recording failure must not replace the generation result.
