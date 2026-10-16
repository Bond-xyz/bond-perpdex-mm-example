"""Restricted local credential files; private keys never enter a session cache."""

import json
import os
import stat
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .models import Json, SafetyError
from .signing import subaccount_sender


@dataclass(repr=False)
class Session:
    api_key: str = field(repr=False)
    key: Ed25519PrivateKey = field(repr=False)
    expires_at_ms: int
    owner: str
    subaccount: str
    account_id: str | None = None

    def needs_renewal(self, now_ms: int) -> bool:
        return self.expires_at_ms - now_ms <= 86_400_000


@dataclass(frozen=True, repr=False)
class SessionCache:
    """Bind local token serialization to an explicit venue, wallet, name and key."""

    path: str | Path
    http_url: str
    chain_id: int
    owner: str
    subaccount: str
    key: Ed25519PrivateKey

    def __post_init__(self) -> None:
        subaccount_sender(self.owner, self.subaccount)

    def identity_fields(self) -> Json:
        return {
            "version": 1,
            "http_url": self.http_url,
            "chain_id": self.chain_id,
            "owner": self.owner.lower(),
            "subaccount": self.subaccount,
            "public_key": self.key.public_key().public_bytes_raw().hex(),
        }

    def load(self) -> Session | None:
        cached = read_session_cache(self.path)
        if cached is None:
            return None
        if any(cached.get(field) != value for field, value in self.identity_fields().items()):
            raise SafetyError("Session cache belongs to another wallet, subaccount, key or venue")
        expiry = cached.get("expires_at_ms")
        if (
            type(expiry) is not int
            or not isinstance(cached.get("api_key"), str)
            or not cached["api_key"]
        ):
            raise SafetyError("Invalid cached session token or expiry")
        account_id = cached.get("account_id")
        try:
            uuid.UUID(account_id)
        except (ValueError, TypeError, AttributeError):
            raise SafetyError("Invalid cached account identity") from None
        return Session(cached["api_key"], self.key, expiry, self.owner, self.subaccount, account_id)

    def validate_identity(self, me: Json, session: Session) -> None:
        if (
            str(me.get("address", "")).removeprefix("0x").lower() != self.owner[2:].lower()
            or str(me.get("name", "")).removeprefix("0x").lower()
            != subaccount_sender(self.owner, self.subaccount)[-24:]
            or me.get("id") != session.account_id
        ):
            raise SafetyError("Cached session identity does not match the configured account")

    def save(self, session: Session) -> None:
        write_session_cache(
            self.path,
            {
                **self.identity_fields(),
                "api_key": session.api_key,
                "expires_at_ms": session.expires_at_ms,
                "account_id": session.account_id,
            },
        )


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
