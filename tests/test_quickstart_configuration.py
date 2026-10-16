import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bond_perpdex_client.configuration import BY_NAME, SETTINGS, Settings, parse_dotenv, template
from bond_perpdex_client.credentials import load_ed25519_private_key
from bond_perpdex_client.models import SafetyError
from bond_perpdex_client.setup import ensure_environment, ensure_signing_key


@pytest.fixture
def configured(tmp_path):
    path = tmp_path / ".env"
    assert ensure_environment(path)
    return Settings.load(path, environment={})


def test_checked_in_template_matches_canonical_defaults():
    root = Path(__file__).parents[1]
    assert (root / ".env.example").read_text() == template()
    assert set(parse_dotenv(template())) == set(BY_NAME)
    assert parse_dotenv(template())["BOND_SUBACCOUNT"] == "tradegods-z"
    assert parse_dotenv(template())["BOND_TESTNET_WALLET_KEY"] == ""


def test_setup_is_owner_only_idempotent_and_never_copies_process_secrets(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setenv("BOND_TESTNET_WALLET_KEY", "must-not-be-written")
    assert ensure_environment(path, "maker-z")
    saved = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert b"BOND_SUBACCOUNT=maker-z" in saved
    assert b"must-not-be-written" not in saved
    assert not ensure_environment(path, "another-z")
    assert path.read_bytes() == saved


@pytest.mark.parametrize("name", ["'maker", '"maker', "'\"maker"])
def test_setup_preserves_printable_subaccount_names_with_leading_quotes(tmp_path, name):
    path = tmp_path / ".env"
    assert ensure_environment(path, name)
    settings = Settings.load(path, environment={})
    settings.validate()
    assert settings.values["BOND_SUBACCOUNT"] == name


def test_signing_key_generation_preserves_existing_key_and_rejects_symlinks(tmp_path):
    path = tmp_path / "private" / "ed25519.pem"
    assert ensure_signing_key(path)
    saved = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert isinstance(load_ed25519_private_key(path), Ed25519PrivateKey)
    assert not ensure_signing_key(path) and path.read_bytes() == saved
    link = tmp_path / "link.pem"
    link.symlink_to(path)
    with pytest.raises(OSError):
        ensure_signing_key(link)
    assert path.read_bytes() == saved


def test_existing_dotenv_permissions_and_symlinks_are_not_silently_changed(tmp_path):
    path = tmp_path / ".env"
    ensure_environment(path)
    path.chmod(0o644)
    assert not ensure_environment(path)
    with pytest.raises(SafetyError, match="owner-only"):
        Settings.load(path, environment={})
    assert path.stat().st_mode & 0o777 == 0o644
    path.chmod(0o600)
    link = tmp_path / "link.env"
    link.symlink_to(path)
    assert not ensure_environment(link)
    with pytest.raises(OSError):
        Settings.load(link, environment={})


def test_process_values_override_file_and_paths_resolve_beside_that_file(configured):
    path = configured.root / ".env"
    settings = Settings.load(path, {"BOND_SUBACCOUNT": "another-z", "BOND_QUANTITY": "0.003"})
    settings.validate()
    assert settings.values["BOND_QUANTITY"] == "0.003"
    assert settings.values["BOND_SUBACCOUNT"] == "another-z"
    assert settings.path("BOND_SESSION_FILE") == configured.root / ".local/galileo.session.json"
    assert settings.values["BOND_MODE"] == "offline"


def test_dotenv_quotes_comments_and_export_are_data_not_shell_code(tmp_path):
    marker = tmp_path / "not-created"
    content = f"""# comment
export BOND_SUBACCOUNT="tradegods-z"
BOND_QUANTITY=0.003 # base amount
BOND_TESTNET_WALLET_KEY='$(touch {marker})'
"""
    data = parse_dotenv(content)
    assert data["BOND_SUBACCOUNT"] == "tradegods-z" and data["BOND_QUANTITY"] == "0.003"
    assert data["BOND_TESTNET_WALLET_KEY"] == f"$(touch {marker})"
    assert not marker.exists()


@pytest.mark.parametrize(
    "content",
    [
        "UNKNOWN=secret",
        "bad secret line",
        "BOND_MODE=offline\nBOND_MODE=trade",
        'BOND_MODE="secret',
    ],
)
def test_dotenv_errors_do_not_echo_input_values(content):
    with pytest.raises(SafetyError) as error:
        parse_dotenv(content)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "name,value",
    [
        ("BOND_MODE", "unsupported"),
        ("BOND_SUBACCOUNT", "bad name"),
        ("BOND_SUBACCOUNT", "x" * 13),
        ("BOND_SYMBOL", "BADPERP"),
        ("BOND_CHAIN_ID", "1"),
        ("BOND_HTTP_URL", "https://example.com"),
        ("BOND_QUANTITY", "NaN"),
        ("BOND_QUANTITY", "-1"),
        ("BOND_MAX_POSITION", "0"),
        ("BOND_HALF_SPREAD_BPS", "0"),
        ("BOND_MAX_FEED_AGE_MS", "10001"),
        ("BOND_MAX_OPEN_ORDERS", "21"),
        ("BOND_MAX_EVENTS", "0"),
        ("BOND_MAX_RECONNECTS", "4"),
        ("BOND_STREAM_IDLE_TIMEOUT_SECONDS", "0"),
        ("BOND_STREAM_IDLE_TIMEOUT_SECONDS", "61"),
        ("BOND_ORDER_SIDE", "HOLD"),
        ("BOND_LIMIT_PRICE", "garbage"),
    ],
)
def test_invalid_settings_fail_before_execution(configured, name, value):
    configured.values[name] = value
    with pytest.raises(SafetyError):
        configured.validate()


def test_show_config_and_repr_redact_wallet_secrets(configured):
    configured.values["BOND_TESTNET_WALLET_KEY"] = "actual-wallet-secret"
    output = configured.redacted_json()
    assert json.loads(output)["BOND_TESTNET_WALLET_KEY"] == "<set>"
    assert "actual-wallet-secret" not in output + repr(configured)
    assert all(setting.name in output for setting in SETTINGS)


def test_variable_reference_matches_the_committed_documentation():
    from bond_perpdex_client.configuration import reference_table

    document = (Path(__file__).parents[1] / "docs/configuration.md").read_text()
    generated = document.split("<!-- BEGIN GENERATED VARIABLES -->\n", 1)[1].split(
        "<!-- END GENERATED VARIABLES -->", 1
    )[0]
    assert generated == reference_table()


def test_rpc_inputs_are_also_redacted(configured):
    configured.values["BOND_GALILEO_RPC_URL"] = "https://rpc.example?api_key=provider-secret"
    assert "provider-secret" not in configured.redacted_json()
    assert json.loads(configured.redacted_json())["BOND_GALILEO_RPC_URL"] == "<set>"
