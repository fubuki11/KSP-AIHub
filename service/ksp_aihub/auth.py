import base64
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__
from .common import HubError, endpoint, loads, origin


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def header_secret(value):
    if not isinstance(value, str) or not value or len(value) > 24000 or any(not 33 <= ord(c) <= 126 for c in value):
        raise HubError("auth_missing", "A valid credential is required.", 503)
    return value


class AuthBroker:
    def __init__(self, config, store, clock=time.time):
        self.config, self.store, self.clock = config, store, clock
        self._locks = {key: threading.RLock() for key in config.credentials}
        self._pending = {}
        self._pending_lock = threading.RLock()
        self._opener = build_opener(NoRedirects())

    def credential(self, key):
        if key not in self.config.credentials:
            raise HubError("auth_missing", "Unknown credential.", 404)
        credential = self.config.credentials[key]
        if credential.get("provider") == "anthropic" and credential["kind"] not in ("api_key_env", "api_key_store"):
            raise HubError("auth_unsupported", "Claude subscription OAuth is not a supported third-party login path. Configure an Anthropic API key.", 409)
        if credential["kind"] == "opencode_oauth" and credential.get("provider") != "openai":
            raise HubError("auth_unsupported", "The migration adapter only supports the existing OpenAI OAuth login.", 409)
        return credential

    def extend_configuration(self, config):
        # Profile onboarding only adds credentials; active requests retain existing bindings.
        for key in config.credentials:
            self._locks.setdefault(key, threading.RLock())
        self.config = config

    def registration(self, credential):
        key = credential.get("registration")
        registration = self.config.registrations.get(key)
        if not isinstance(registration, dict) or registration.get("enabled") is not True:
            raise HubError("oauth_registration_missing", "Configure an enabled OAuth client registration approved for this service.", 409)
        if set(registration) - {"enabled", "clientId", "clientSecretEnv", "authorizationUrl", "tokenUrl", "redirectUri", "scopes", "allowedApiOrigins"}:
            raise HubError("invalid_configuration", "Unknown OAuth registration fields.")
        client_id = registration.get("clientId")
        if not isinstance(client_id, str) or not client_id.strip():
            raise HubError("oauth_registration_missing", "OAuth clientId is not configured.", 409)
        endpoint(registration.get("authorizationUrl")); endpoint(registration.get("tokenUrl"))
        expected = f"http://127.0.0.1:{self.config.port}/oauth/callback"
        if registration.get("redirectUri") != expected:
            raise HubError("invalid_configuration", "OAuth redirectUri must match the configured loopback callback.")
        scopes = registration.get("scopes", [])
        if not isinstance(scopes, list) or any(not isinstance(s, str) or not s or any(c.isspace() for c in s) for s in scopes):
            raise HubError("invalid_configuration", "OAuth scopes must be separate scope strings.")
        approved = registration.get("allowedApiOrigins", [])
        if not isinstance(approved, list) or not set(credential["allowedOrigins"]).issubset({origin(u) for u in approved}):
            raise HubError("invalid_configuration", "Credential origins must be approved by the OAuth registration.")
        return registration

    @staticmethod
    def fingerprint(registration):
        return hashlib.sha256(json.dumps(registration, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def begin(self, key):
        credential = self.credential(key)
        if credential["kind"] != "oauth_managed":
            raise HubError("auth_unsupported", "This credential does not use independent OAuth.", 409)
        registration = self.registration(credential)
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        with self._pending_lock:
            self._pending = {k: v for k, v in self._pending.items() if v["expires"] > self.clock()}
            if len(self._pending) >= 8:
                raise HubError("busy", "Too many pending login sessions.", 429)
            # One login per credential avoids refresh-token races with older browser tabs.
            self._pending = {k: v for k, v in self._pending.items() if v["credential"] != key}
            self._pending[state] = {"credential": key, "verifier": verifier, "expires": self.clock() + 600,
                                    "fingerprint": self.fingerprint(registration)}
        query = {"response_type": "code", "client_id": registration["clientId"], "redirect_uri": registration["redirectUri"],
                 "scope": " ".join(registration.get("scopes", [])), "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
        return {"authorizationUrl": registration["authorizationUrl"] + "?" + urlencode(query), "expiresIn": 600}

    def _token_request(self, registration, form):
        form = dict(form, client_id=registration["clientId"])
        secret_env = registration.get("clientSecretEnv")
        if secret_env:
            value = os.environ.get(secret_env)
            if not value:
                raise HubError("auth_missing", "OAuth client secret environment variable is not set.", 503)
            form["client_secret"] = value
        request = Request(registration["tokenUrl"], data=urlencode(form).encode("utf-8"), method="POST",
                          headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=30) as response:
                body = response.read(131073)
            if len(body) > 131072:
                raise ValueError
            result = loads(body)
            header_secret(result.get("access_token"))
            if str(result.get("token_type", "Bearer")).lower() != "bearer":
                raise ValueError
            lifetime = result.get("expires_in", 3600)
            if isinstance(lifetime, bool) or not isinstance(lifetime, (int, float)) or not math.isfinite(lifetime) or not 1 <= lifetime <= 31536000:
                raise ValueError
            return result, self.clock() + lifetime
        except HTTPError as error:
            error.close()
            raise HubError("oauth_token_error", "OAuth token exchange was rejected. Reconnect using the approved registration.", 502) from None
        except (URLError, OSError, ValueError, TypeError, AttributeError, HubError):
            raise HubError("oauth_token_error", "OAuth token exchange failed; token contents are not logged.", 502) from None

    def callback(self, state, code, error=None):
        if not isinstance(state, str):
            raise HubError("oauth_state_invalid", "Missing OAuth state.")
        with self._pending_lock:
            session = self._pending.pop(state, None)
        if session is None or session["expires"] <= self.clock():
            raise HubError("oauth_state_invalid", "OAuth state is expired, unknown or already consumed.")
        if error or not isinstance(code, str) or not code or len(code) > 16000:
            raise HubError("oauth_denied", "Authorization was not completed.")
        key = session["credential"]
        credential = self.credential(key)
        registration = self.registration(credential)
        if session["fingerprint"] != self.fingerprint(registration):
            raise HubError("oauth_state_invalid", "OAuth configuration changed during sign-in.")
        with self._locks[key]:
            tokens, expires = self._token_request(registration, {"grant_type": "authorization_code", "code": code,
                "redirect_uri": registration["redirectUri"], "code_verifier": session["verifier"]})
            self.store.put(key, {"access": tokens["access_token"], "refresh": tokens.get("refresh_token"),
                                 "expires": expires, "binding": self.fingerprint(registration)})

    def set_api_key(self, key, value):
        credential = self.credential(key)
        if credential["kind"] != "api_key_store":
            raise HubError("auth_unsupported", "Only stored API-key credentials can be updated here.", 409)
        with self._locks[key]:
            self.store.put(key, {"apiKey": header_secret(value)})

    def status(self, key):
        try:
            credential = self.credential(key)
            kind = credential["kind"]
            if kind == "api_key_env":
                header_secret(os.environ.get(credential["env"]))
            elif kind == "api_key_store":
                header_secret((self.store.get(key) or {}).get("apiKey"))
            elif kind == "oauth_managed":
                registration = self.registration(credential)
                value = self.store.get(key)
                if not value or value.get("binding") != self.fingerprint(registration):
                    return "login_required"
                if value.get("expires", 0) <= self.clock() + 60:
                    return "refresh_needed" if value.get("refresh") else "login_required"
            else:
                self._external(credential)
            return "ready"
        except HubError as error:
            return error.code

    def _external(self, credential):
        try:
            path = Path(credential.get("authFile", Path.home() / ".local/share/opencode/auth.json")).expanduser()
            value = loads(path.read_text(encoding="utf-8"))["openai"]
            if value.get("type") != "oauth" or value.get("expires", 0) <= (self.clock() + 30) * 1000:
                raise ValueError
            return header_secret(value["access"]), value.get("accountId")
        except (OSError, KeyError, TypeError, ValueError, HubError):
            raise HubError("auth_missing", "Refresh the external OpenCode OpenAI login, or configure independent credentials.", 503) from None

    def headers(self, profile):
        key = profile["credential"]
        credential = self.credential(key)
        if origin(profile["baseUrl"]) not in credential["allowedOrigins"]:
            raise HubError("auth_origin_mismatch", "Credential cannot be sent to this API origin.", 403)
        kind, account = credential["kind"], None
        if kind == "api_key_env":
            secret = header_secret(os.environ.get(credential["env"]))
        elif kind == "api_key_store":
            secret = header_secret((self.store.get(key) or {}).get("apiKey"))
        elif kind == "opencode_oauth":
            if urlsplit(profile["baseUrl"]).hostname not in ("api.openai.com", "chatgpt.com", "127.0.0.1", "localhost", "::1"):
                raise HubError("auth_origin_mismatch", "OpenAI migration tokens require an official origin or configured loopback proxy.", 403)
            secret, account = self._external(credential)
        else:
            registration = self.registration(credential)
            with self._locks[key]:
                value = self.store.get(key) or {}
                if value.get("binding") != self.fingerprint(registration):
                    raise HubError("auth_missing", "Complete independent OAuth sign-in first.", 503)
                if value.get("expires", 0) <= self.clock() + 60:
                    if not value.get("refresh"):
                        raise HubError("auth_missing", "OAuth login expired; sign in again.", 503)
                    tokens, expires = self._token_request(registration, {"grant_type": "refresh_token", "refresh_token": value["refresh"]})
                    value.update(access=tokens["access_token"], refresh=tokens.get("refresh_token", value["refresh"]), expires=expires)
                    self.store.put(key, value)
                secret = header_secret(value.get("access"))
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "KSPAIHub/" + __version__}
        header = credential.get("keyHeader", "auto")
        if profile["protocol"] == "messages":
            headers["anthropic-version"] = "2023-06-01"
        if header == "auto":
            header = "x-api-key" if profile["protocol"] == "messages" else "authorization"
        if header != "authorization":
            headers[header] = secret
        else:
            headers["Authorization"] = "Bearer " + secret
        if account:
            headers["ChatGPT-Account-Id"] = header_secret(account)
        if kind == "opencode_oauth" or profile.get("flavor") == "codex":
            headers.update({"originator": "opencode", "OpenAI-Beta": "responses=experimental"})
        return headers, secret
