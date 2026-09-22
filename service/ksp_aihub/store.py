"""Windows user-bound DPAPI secret storage. No plaintext fallback."""
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile
import threading

from .common import HubError, identifier, loads


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]


def _crypt(data, decrypt=False):
    if os.name != "nt":
        raise HubError("secret_store_unavailable", "This build uses Windows DPAPI; use environment credentials on other platforms.", 503)
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    result = _Blob()
    crypto = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypto.CryptUnprotectData if decrypt else crypto.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise HubError("secret_store_error", "Could not access the current user's encrypted credential store.", 503)
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".aihub-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class SecretStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self._lock = threading.RLock()

    def get(self, key):
        path = self.directory / (identifier(key, "credential ID") + ".dpapi.json")
        with self._lock:
            if not path.exists():
                return None
            try:
                envelope = loads(path.read_text(encoding="utf-8"))
                if envelope.get("version") != 1:
                    raise ValueError
                data = base64.b64decode(envelope["protected"], validate=True)
                return loads(_crypt(data, True).decode("utf-8"))
            except (OSError, KeyError, ValueError, TypeError):
                raise HubError("secret_store_error", "Credential data could not be decoded.", 503) from None

    def put(self, key, value):
        path = self.directory / (identifier(key, "credential ID") + ".dpapi.json")
        data = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
        with self._lock:
            atomic_json(path, {"version": 1, "protected": base64.b64encode(_crypt(data)).decode("ascii")})


class StateLock:
    """One owning gateway per state directory, including across different ports."""
    def __init__(self, directory):
        self.path = Path(directory) / "service.lock"
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"0"); self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close(); self.handle = None
            raise HubError("state_in_use", "Another gateway owns this state directory.", 409) from None
        return self

    def __exit__(self, *args):
        if self.handle is not None:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close(); self.handle = None
