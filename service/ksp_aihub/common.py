import json
import math
import re
from urllib.parse import urlsplit


class HubError(RuntimeError):
    def __init__(self, code, message, status=400, details=None):
        self.code, self.status = code, status
        self.details = details or {}
        super().__init__(message)


def identifier(value, label="identifier"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
        raise HubError("invalid_request", f"Invalid {label}.")
    return value


def loads(text):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    def reject(value):
        raise ValueError("Nonfinite JSON number")
    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=reject)
        json.dumps(value, allow_nan=False)
        return value
    except (ValueError, TypeError, RecursionError):
        raise HubError("invalid_json", "Expected finite, duplicate-free JSON.") from None


def endpoint(value):
    try:
        if not isinstance(value, str) or any(ord(c) <= 32 for c in value):
            raise ValueError
        url = urlsplit(value)
        port = url.port
        if (url.scheme not in ("https", "http") or not url.hostname or url.username is not None or
                url.password is not None or url.query or url.fragment or
                (url.scheme == "http" and url.hostname not in ("127.0.0.1", "localhost", "::1"))):
            raise ValueError
        return value.rstrip("/")
    except ValueError:
        raise HubError("invalid_configuration", "Endpoints require HTTPS or loopback HTTP, without credentials/query/fragment.") from None


def origin(value):
    url = urlsplit(endpoint(value))
    port = url.port or (443 if url.scheme == "https" else 80)
    return f"{url.scheme}://{url.hostname.lower()}:{port}"


def positive(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise HubError("invalid_configuration", f"{name} must be between {minimum} and {maximum}.")
    return value


def messages(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise HubError("invalid_request", "messages must contain 1-100 text messages.")
    for item in value:
        if (not isinstance(item, dict) or set(item) != {"role", "content"} or
                item["role"] not in ("system", "user", "assistant") or not isinstance(item["content"], str)):
            raise HubError("invalid_request", "Only system/user/assistant text messages are supported in v1.")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > 300000:
        raise HubError("request_too_large", "Message context exceeds 300000 bytes.", 413)
    return value
