"""Sign the newer provisioned bot-key HTTP transport without making a request.

Bot-key headers are a separate layer from the account's form ``signature`` and
the wallet's Vertex ``signedOrder``. This module only prepares the outer layer;
callers must still supply the other signatures for order commands.
"""

import base64
import hashlib
import os
import time
import uuid
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .models import SafetyError


@dataclass(frozen=True, repr=False)
class BotKeyCredentials:
    api_key: str = field(repr=False)
    private_key: Ed25519PrivateKey = field(repr=False)
    subaccount_id: str | None = None

    @classmethod
    def from_environment(cls) -> "BotKeyCredentials":
        """Read an already provisioned key; never create or persist credentials."""
        api_key = os.environ.get("BOND_BOT_API_KEY", "")
        pem = os.environ.get("BOND_BOT_ED25519_PRIVATE_KEY_PEM", "")
        if not api_key or not pem:
            raise SafetyError("BOND_BOT_API_KEY and BOND_BOT_ED25519_PRIVATE_KEY_PEM are required")
        try:
            private_key = serialization.load_pem_private_key(pem.encode(), password=None)
        except (TypeError, ValueError):
            raise SafetyError("Invalid bot Ed25519 private key PEM") from None
        if not isinstance(private_key, Ed25519PrivateKey):
            raise SafetyError("Bot signing key must be Ed25519")
        subaccount_id = os.environ.get("BOND_BOT_SUBACCOUNT_ID") or None
        if subaccount_id is not None:
            try:
                uuid.UUID(subaccount_id)
            except ValueError:
                raise SafetyError("BOND_BOT_SUBACCOUNT_ID must be a UUID") from None
        return cls(api_key, private_key, subaccount_id)

    def signed_headers(
        self,
        method: str,
        path_and_query: str,
        body: bytes = b"",
        *,
        timestamp_ms: int | None = None,
        nonce: str | None = None,
    ) -> dict[str, str]:
        """Bind the exact HTTP method, path/query, selected account, and body bytes.

        Supply a fresh nonce for every request. The optional timestamp and nonce
        arguments make the wire vector deterministic in tests.
        """
        if method != method.upper() or not path_and_query.startswith("/"):
            raise SafetyError("Sign an uppercase method and an absolute path/query")
        if not isinstance(body, bytes):
            raise SafetyError("Sign the exact raw request body bytes")
        timestamp_ms = timestamp_ms if timestamp_ms is not None else time.time_ns() // 1_000_000
        nonce = nonce or str(uuid.uuid4())
        if type(timestamp_ms) is not int or timestamp_ms <= 0 or not nonce:
            raise SafetyError("Invalid bot request timestamp or nonce")
        account = self.subaccount_id or "default"
        digest = hashlib.sha256(body).hexdigest()
        canonical = f"{timestamp_ms}\n{nonce}\n{method}\n{path_and_query}\n{account}\n{digest}"
        signature = base64.b64encode(self.private_key.sign(canonical.encode())).decode()
        headers = {
            "x-bond-api-key": self.api_key,
            "x-bond-timestamp": str(timestamp_ms),
            "x-bond-nonce": nonce,
            "x-bond-signature": signature,
        }
        if self.subaccount_id:
            headers["x-bond-subaccount-id"] = self.subaccount_id
        return headers
