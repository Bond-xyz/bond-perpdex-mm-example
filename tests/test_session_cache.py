import json

import pytest
from cryptography.hazmat.primitives import serialization

from bond_perpdex_client import BondPerpDexClient, SafetyError, load_ed25519_private_key
from bond_perpdex_client import TestnetConfig as Config
from bond_perpdex_client.client import Session
from bond_perpdex_client.offline import NOW_MS, demo_identity
from bond_perpdex_client.signing import subaccount_sender

ACCOUNT_ID = "00000000-0000-4000-8000-000000000001"
DAY = 86_400_000


class SessionVenue:
    offline = False
    private_enabled = True
    mutations_enabled = False

    def __init__(self, wallet):
        self.wallet = wallet
        self.signins = 0
        self.paths = []
        self.read_error = False
        self.wrong_name = False
        self.wrong_address = False
        self.wrong_id = False

    def request(self, request):
        self.paths.append(request.path)
        if request.path == "/auth/nonce":
            return {"nonce": "offlineNonce0001"}
        if request.path == "/auth/signin":
            body = json.loads(request.body)
            assert "Request ID: tradegods-z" in body["message"]
            assert "BEGIN PUBLIC KEY" in body["secret_key"]
            self.signins += 1
            return {
                "api_key": f"test-token-{self.signins}",
                "id": ACCOUNT_ID,
                "expires_at": "2026-03-04T02:40:00+00:00",
            }
        assert request.path == "/auth/me" and request.method == "GET"
        assert not request.body and set(request.headers) == {"x-api-key"}
        if self.read_error:
            raise SafetyError("HTTP response status 503; no retry")
        if request.headers["x-api-key"] != f"test-token-{self.signins}":
            raise SafetyError("HTTP response status 401; no retry")
        return {
            "id": "00000000-0000-4000-8000-000000000002" if self.wrong_id else ACCOUNT_ID,
            "address": "00" * 20 if self.wrong_address else self.wallet.address[2:],
            "name": subaccount_sender(
                self.wallet.address, "wrong" if self.wrong_name else "tradegods-z"
            )[-24:],
        }


def setup_cache(tmp_path):
    wallet, key = demo_identity()
    venue = SessionVenue(wallet)
    client = BondPerpDexClient(
        venue, config=Config(allow_live_private=True), clock_ms=lambda: NOW_MS
    )
    path = tmp_path / "test.session.json"
    client.authenticate_cached(wallet, key, "tradegods-z", path)
    return client, venue, wallet, key, path


def test_first_signin_reuses_matching_session_on_restart_without_replacing_token(tmp_path):
    client, venue, wallet, key, path = setup_cache(tmp_path)
    saved = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert b"PRIVATE KEY" not in saved
    restored = BondPerpDexClient(venue, config=client.config, clock_ms=lambda: NOW_MS)
    restored.authenticate_cached(wallet, key, "tradegods-z", path)
    assert venue.signins == 1 and venue.paths[-1] == "/auth/me"
    assert restored._session.api_key == client._session.api_key
    assert restored._session.account_id == ACCOUNT_ID
    assert path.read_bytes() == saved


@pytest.mark.parametrize("remaining", [DAY, DAY - 1, 0, -1])
def test_renewal_at_24_hours_or_expired_replaces_cached_token_atomically(tmp_path, remaining):
    client, venue, wallet, key, path = setup_cache(tmp_path)
    saved = json.loads(path.read_text())
    saved["expires_at_ms"] = NOW_MS + remaining
    path.write_text(json.dumps(saved))
    client.authenticate_cached(wallet, key, "tradegods-z", path)
    assert venue.signins == 2
    assert json.loads(path.read_text())["api_key"] == "test-token-2"
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [path]


def test_session_threshold_does_not_renew_early():
    wallet, key = demo_identity()
    assert not Session(
        "fixture", key, NOW_MS + DAY + 1, wallet.address, "tradegods-z"
    ).needs_renewal(NOW_MS)
    assert Session("fixture", key, NOW_MS + DAY, wallet.address, "tradegods-z").needs_renewal(
        NOW_MS
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner", "another"),
        ("subaccount", "another"),
        ("public_key", "another"),
        ("http_url", "https://example.com"),
        ("chain_id", 1),
    ],
)
def test_mismatched_cache_fails_before_network_and_preserves_file(tmp_path, field, value):
    client, venue, wallet, key, path = setup_cache(tmp_path)
    saved = json.loads(path.read_text())
    saved[field] = value
    path.write_text(json.dumps(saved))
    before = path.read_bytes(), len(venue.paths)
    with pytest.raises(SafetyError, match="another wallet"):
        client.authenticate_cached(wallet, key, "tradegods-z", path)
    assert (path.read_bytes(), len(venue.paths)) == before


@pytest.mark.parametrize("failure", ["read_error", "wrong_name", "wrong_address", "wrong_id"])
def test_cached_read_failure_never_silently_replaces_credentials(tmp_path, failure):
    client, venue, wallet, key, path = setup_cache(tmp_path)
    saved = path.read_bytes()
    setattr(venue, failure, True)
    with pytest.raises(SafetyError):
        client.authenticate_cached(wallet, key, "tradegods-z", path)
    assert venue.signins == 1 and path.read_bytes() == saved


@pytest.mark.parametrize(
    "field,value",
    [
        ("api_key", ""),
        ("api_key", 5),
        ("expires_at_ms", True),
        ("expires_at_ms", "invalid"),
        ("account_id", None),
        ("account_id", "malformed"),
    ],
)
def test_malformed_cache_never_signs_in_or_overwrites_credentials(tmp_path, field, value):
    client, venue, wallet, key, path = setup_cache(tmp_path)
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    before = path.read_bytes(), len(venue.paths)
    with pytest.raises(SafetyError):
        client.authenticate_cached(wallet, key, "tradegods-z", path)
    assert (path.read_bytes(), len(venue.paths)) == before
    assert venue.signins == 1


def test_session_renewal_requires_stream_and_pending_command_quiescence(tmp_path):
    client, venue, wallet, key, path = setup_cache(tmp_path)
    for field, value in [
        ("_stream_connected", True),
        ("uncertain_commands", {"intent": object()}),
        ("_pending_cancels", {"order": "BTCUSDCPERP"}),
    ]:
        previous = getattr(client, field)
        setattr(client, field, value)
        with pytest.raises(SafetyError, match="reconcile pending"):
            client.authenticate_cached(wallet, key, "tradegods-z", path)
        setattr(client, field, previous)
    assert venue.signins == 1


def test_live_signin_requires_explicit_name_and_persistent_key_before_io():
    wallet, key = demo_identity()
    venue = SessionVenue(wallet)
    client = BondPerpDexClient(venue, config=Config(allow_live_private=True))
    for kwargs in [
        {"key": key},
        {"subaccount": "tradegods-z"},
        {"key": key, "subaccount": "bad name"},
    ]:
        with pytest.raises(SafetyError):
            client.authenticate(wallet, **kwargs)
    assert not venue.paths


def test_owner_only_key_and_session_files_reject_symlinks_and_public_permissions(tmp_path):
    _, key = demo_identity()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    path = tmp_path / "key.pem"
    path.write_bytes(pem)
    path.chmod(0o600)
    assert (
        load_ed25519_private_key(path).public_key().public_bytes_raw()
        == key.public_key().public_bytes_raw()
    )
    path.chmod(0o644)
    with pytest.raises(SafetyError, match="owner-only"):
        load_ed25519_private_key(path)
    path.chmod(0o600)
    link = tmp_path / "link.pem"
    link.symlink_to(path)
    with pytest.raises(OSError):
        load_ed25519_private_key(link)
    client, venue, wallet, key, cache = setup_cache(tmp_path)
    cache.chmod(0o644)
    with pytest.raises(SafetyError, match="owner-only"):
        client.authenticate_cached(wallet, key, "tradegods-z", cache)
    assert venue.signins == 1


def test_identity_exposes_backend_uuid_and_padded_name_without_token(tmp_path):
    client, _, wallet, _, _ = setup_cache(tmp_path)
    identity = client.identity()
    assert identity["id"] == ACCOUNT_ID
    assert identity["name"] == subaccount_sender(wallet.address, "tradegods-z")[-24:]
    assert "api_key" not in identity
