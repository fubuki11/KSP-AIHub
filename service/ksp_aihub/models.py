"""Authenticated, bounded model discovery. No inference or implicit model selection."""
import copy
import hashlib
from http.client import HTTPException
import json
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from .auth import NoRedirects
from .common import HubError, loads
from .config import Configuration
from .presets import wire_protocol


class ModelCatalog:
    def __init__(self, broker, clock=time.time):
        self.broker, self.clock = broker, clock
        self.opener = build_opener(NoRedirects())
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(2)
        self.cache, self.epochs = {}, {}

    def invalidate(self, credential):
        with self.lock:
            self.epochs[credential] = self.epochs.get(credential, 0) + 1
            self.cache.clear()

    def list(self, profile, refresh=False):
        if profile["modelsFormat"] == "none":
            raise HubError("models_unsupported", "Model discovery is disabled for this connection; enter a model ID manually.", 409)
        # Authenticate before serving a cache as well, so removed/expired keys are not hidden.
        headers, secret = self.broker.headers(profile)
        if profile["modelsFormat"] == "gemini":
            headers.pop("Authorization", None)
            headers["x-goog-api-key"] = secret
        with self.lock:
            key = (json.dumps({k: v for k, v in profile.items() if k != "model"}, sort_keys=True),
                   hashlib.sha256(secret.encode("utf-8")).digest(), self.epochs.get(profile["credential"], 0))
            cached = self.cache.get(key)
            if not refresh and cached and self.clock() - cached["fetchedAt"] < 300:
                return {**copy.deepcopy(cached), "cached": True}
        if not self.slots.acquire(blocking=False):
            raise HubError("busy", "Model discovery is already handling two requests.", 429)
        try:
            result = self._fetch(profile, headers, secret)
            with self.lock:
                if len(self.cache) >= 128: self.cache.clear()
                self.cache[key] = copy.deepcopy(result)
            return result
        finally:
            self.slots.release()

    def _fetch(self, profile, headers, secret):
        rows, cursors, query = {}, set(), {}
        kind, total = profile["modelsFormat"], 0
        if kind == "gemini": query["pageSize"] = 1000
        if kind == "anthropic": query["limit"] = 1000
        deadline = time.monotonic() + 60
        try:
            for page in range(20):
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise HubError("models_timeout", "Model listing timed out; manual model IDs remain available.", 504)
                url = profile["modelsUrl"] + (("?" + urlencode(query)) if query else "")
                with self.opener.open(Request(url, headers=headers, method="GET"), timeout=min(30, profile["timeout"], remaining)) as response:
                    raw = response.read(2000001)
                total += len(raw)
                if len(raw) > 2000000 or total > 8000000:
                    raise HubError("models_limit", "Model catalog exceeds the response limit.", 502)
                data = loads(raw)
                entries = data.get("models" if kind == "gemini" else "data")
                if not isinstance(entries, list): raise ValueError
                for entry in entries:
                    if not isinstance(entry, dict): raise ValueError
                    if kind == "gemini":
                        methods = entry.get("supportedGenerationMethods", [])
                        if not isinstance(methods, list): raise ValueError
                        if "generateContent" not in methods: continue
                        name = entry.get("name")
                        if not isinstance(name, str) or not name.startswith("models/"): raise ValueError
                        name = name[len("models/"):]
                    else:
                        name = entry.get("id")
                    Configuration.model(name)
                    label = entry.get("displayName", entry.get("display_name", name))
                    if not isinstance(label, str): label = name
                    label = " ".join(label.split())[:200]
                    if secret in name or secret in label: raise ValueError
                    if profile["protocol"] == "zen" and name.lower().startswith("jev-"): continue
                    protocol = wire_protocol(dict(profile, model=name))
                    rows[name] = {"id": name, "label": label, "protocol": protocol}
                    if len(rows) > 5000:
                        raise HubError("models_limit", "Model catalog contains more than 5000 entries.", 502)
                if kind == "gemini":
                    cursor = data.get("nextPageToken")
                    more = cursor not in (None, "")
                else:
                    more = data.get("has_more", False)
                    if type(more) is not bool: raise ValueError
                    cursor = data.get("last_id") or (entries[-1].get("id") if entries else None)
                if not more:
                    return {"models": [rows[name] for name in sorted(rows)], "fetchedAt": int(self.clock()), "cached": False}
                if not isinstance(cursor, str) or not cursor or len(cursor) > 4000 or cursor in cursors or secret in cursor:
                    raise ValueError
                cursors.add(cursor)
                query["pageToken" if kind == "gemini" else "after_id" if kind == "anthropic" else "after"] = cursor
            raise HubError("models_limit", "Model catalog exceeded 20 pages; no partial list was accepted.", 502)
        except HTTPError as error:
            status = error.code
            error.close()
            raise HubError("models_http_error", f"Model endpoint returned HTTP {status}. Check the API key/endpoint, or enter a model ID manually.", 502) from None
        except (URLError, OSError, HTTPException):
            raise HubError("models_connection_error", "Cannot reach the model endpoint; enter a model ID manually or retry Refresh models.", 502) from None
        except HubError as error:
            if error.code in ("invalid_json", "invalid_request"):
                raise HubError("models_invalid_response", "Model endpoint returned an unsupported catalog format.", 502) from None
            raise
        except (ValueError, TypeError, KeyError, AttributeError):
            raise HubError("models_invalid_response", "Model endpoint returned an unsupported catalog format.", 502) from None
