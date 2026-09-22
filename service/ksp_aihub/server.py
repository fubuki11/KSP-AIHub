import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
import threading
from urllib.parse import parse_qs, urlsplit
import uuid

from . import __version__
from .common import HubError, loads
from .store import atomic_json


def connection_file(path, port):
    path = Path(path)
    endpoint = f"http://127.0.0.1:{port}"
    if path.exists():
        value = loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("endpoint") != endpoint:
            raise HubError("invalid_configuration", "Connection file does not match the configured endpoint.")
        token = value.get("token")
        if not isinstance(token, str) or len(token) < 32 or any(not 33 <= ord(c) <= 126 for c in token):
            raise HubError("invalid_configuration", "Invalid IPC token file.")
        return value
    value = {"endpoint": endpoint, "token": secrets.token_urlsafe(32)}
    atomic_json(path, value)
    return value


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, hub, token):
        self.hub, self.token = hub, token
        self.connections = threading.BoundedSemaphore(8)
        super().__init__(("127.0.0.1", hub.config.port), Handler)

    def process_request(self, request, address):
        if not self.connections.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.connections.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.connections.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)
        self.read_timer = threading.Timer(20, self._expire_read)
        self.read_timer.daemon = True
        self.read_timer.start()

    def _expire_read(self):
        try: self.connection.shutdown(socket.SHUT_RDWR)
        except OSError: pass

    def finish(self):
        self.read_timer.cancel()
        try: super().finish()
        except OSError: pass

    def log_message(self, *args):
        pass  # OAuth callback query strings and request contents must not be logged.

    def _reply(self, status, value):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self.close_connection = True
        self.connection.settimeout(10)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self): self._handle()
    def do_POST(self): self._handle()

    def _handle(self):
        request_id = uuid.uuid4().hex
        try:
            if not self.path.startswith("/") or self.path.startswith("//"):
                raise HubError("invalid_request", "Use origin-form request paths.")
            uri = urlsplit(self.path)
            host = self.headers.get_all("Host", [])
            if len(host) != 1 or host[0] != f"127.0.0.1:{self.server.server_port}":
                raise HubError("invalid_host", "Invalid loopback Host.", 403)
            if sum(len(k) + len(v) + 4 for k, v in self.headers.items()) > 8192:
                raise HubError("request_too_large", "Headers exceed the limit.", 431)
            if self.headers.get_all("Transfer-Encoding"):
                raise HubError("invalid_request", "Transfer-Encoding is not supported.")
            callback = self.command == "GET" and uri.path == "/oauth/callback"
            if not callback:
                if self.headers.get_all("Origin"):
                    raise HubError("invalid_origin", "Browser API requests are not accepted.", 403)
                authorization = self.headers.get_all("Authorization", [])
                if len(authorization) != 1 or not hmac.compare_digest(authorization[0], "Bearer " + self.server.token):
                    raise HubError("unauthorized", "Hub IPC authentication required.", 401)
            length_values = self.headers.get_all("Content-Length", [])
            if len(length_values) > 1 or (length_values and (not length_values[0].isascii() or not length_values[0].isdigit())):
                raise HubError("invalid_request", "Invalid Content-Length.")
            length = int(length_values[0]) if length_values else 0
            if length > 350000:
                raise HubError("request_too_large", "Request body exceeds the limit.", 413)
            if self.command == "GET" and length:
                raise HubError("invalid_request", "GET bodies are unsupported.")
            body = None
            if self.command == "POST":
                if not length_values or self.headers.get_content_type() != "application/json":
                    raise HubError("invalid_request", "POST requires JSON and Content-Length.")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise HubError("invalid_request", "Incomplete request body.")
                body = loads(raw)
            self.read_timer.cancel()
            query = parse_qs(uri.query, keep_blank_values=True)
            if any(len(values) != 1 for values in query.values()):
                raise HubError("invalid_request", "Duplicate query parameter.")
            hub = self.server.hub
            if callback:
                hub.auth.callback(query.get("state", [None])[0], query.get("code", [None])[0], query.get("error", [None])[0])
                result = {"ok": True, "message": "Sign-in completed. Return to KSP AI Hub."}
            elif self.command == "GET" and uri.path == "/v1/health":
                result = {"ok": True, "apiVersion": 1, "version": __version__}
            elif self.command == "GET" and uri.path == "/v1/profiles":
                result = hub.profiles()
            elif self.command == "POST" and uri.path == "/v1/profiles":
                result = hub.add_profile(body)
            elif self.command == "POST" and uri.path == "/v1/generation-settings":
                result = hub.set_generation(body)
            elif self.command == "GET" and uri.path == "/v1/models":
                if set(query) - {"profile", "refresh"} or query.get("refresh", ["false"])[0] not in ("true", "false"):
                    raise HubError("invalid_request", "Expected profile and optional refresh=true/false.")
                result = hub.models(query.get("profile", [None])[0], query.get("refresh", ["false"])[0] == "true")
            elif self.command == "GET" and uri.path == "/v1/ui":
                result = hub.ui(query.get("clientId", [None])[0])
            elif self.command == "POST" and uri.path == "/v1/select":
                result = hub.select(body)
            elif self.command == "POST" and uri.path == "/v1/generate":
                result = hub.generate(body)
            elif self.command == "POST" and uri.path == "/v1/auth/begin":
                if not isinstance(body, dict) or set(body) != {"credential"}: raise HubError("invalid_request", "Expected credential ID.")
                result = {"ok": True, **hub.auth.begin(body["credential"])}
            elif self.command == "POST" and uri.path == "/v1/auth/api-key":
                if not isinstance(body, dict) or set(body) != {"credential", "apiKey"}: raise HubError("invalid_request", "Expected credential ID and API key.")
                hub.set_api_key(body["credential"], body["apiKey"])
                result = {"ok": True}
            else:
                raise HubError("not_found", "Unsupported API route/method.", 404)
            self._reply(200, result)
        except HubError as error:
            try: self._reply(error.status, {"ok": False, "code": error.code, "message": str(error), "details": error.details, "requestId": request_id})
            except OSError: pass
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            try: self._reply(500, {"ok": False, "code": "internal_error", "message": "Gateway operation failed; sensitive details are not returned.", "requestId": request_id})
            except OSError: pass
        finally:
            self.read_timer.cancel()
