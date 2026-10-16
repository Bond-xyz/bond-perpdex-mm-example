"""Restricted local credential files; private keys never enter a session cache."""

import json
import os
import stat
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .models import SafetyError


def _read_private(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise SafetyError("Credential files must be owner-only regular files (mode 0600)")
        value = stream.read(16_385)
        if len(value) > 16_384:
            raise SafetyError("Credential file exceeds size bound")
        return value


def load_ed25519_private_key(path: str | Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(_read_private(Path(path)), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SafetyError("Expected an Ed25519 PKCS#8 private key")
    return key


def read_session_cache(path: str | Path) -> dict | None:
    try:
        result = json.loads(_read_private(Path(path)))
    except FileNotFoundError:
        return None
    if not isinstance(result, dict):
        raise SafetyError("Invalid session cache")
    return result


def write_session_cache(path: str | Path, value: dict) -> None:
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
