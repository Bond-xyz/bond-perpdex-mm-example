import sys
from types import SimpleNamespace

import pytest

from bond_perpdex_client.configuration import BY_NAME, Settings
from bond_perpdex_client.models import SafetyError
from bond_perpdex_client.quickstart import main, run
from bond_perpdex_client.setup import ensure_environment


@pytest.fixture
def configured(tmp_path):
    path = tmp_path / ".env"
    ensure_environment(path)
    return Settings.load(path, environment={})


def test_first_run_creates_defaults_and_runs_maker_without_network(tmp_path, monkeypatch, capsys):
    for name in BY_NAME:
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / ".env"
    monkeypatch.setattr(sys, "argv", ["quickstart", "--env-file", str(path)])
    main()
    output = capsys.readouterr().out
    assert "subaccount: tradegods-z" in output
    assert "OFFLINE POST_ONLY BUY" in output and "OFFLINE POST_ONLY SELL" in output
    assert "zero network/transactions" in output
    assert path.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / ".local").exists()


def test_strategy_settings_change_the_actual_offline_quote(configured, capsys):
    configured.values["BOND_HALF_SPREAD_BPS"] = "20"
    configured.values["BOND_QUANTITY"] = "0.003"
    run(configured)
    output = capsys.readouterr().out
    assert "BUY 0.003 BTCUSDCPERP @ 64874.90" in output
    assert "SELL 0.003 BTCUSDCPERP @ 65135.10" in output


def test_public_mode_dispatches_without_loading_credentials(configured, monkeypatch):
    configured.values["BOND_MODE"] = "public"
    calls = []
    monkeypatch.setattr(
        "bond_perpdex_client.quickstart.run_demonstration",
        lambda settings, **kw: calls.append((settings, kw)),
    )
    run(configured)
    assert calls[0][1] == {"public_read_only": True}
    assert calls[0][0]["subaccount"] == "tradegods-z"


def test_trade_requires_an_explicit_command_flag_even_if_dotenv_selects_trade(configured):
    configured.values["BOND_MODE"] = "trade"
    with pytest.raises(SafetyError, match="--allow-live-orders"):
        run(configured)
    with pytest.raises(SafetyError, match="BOND_LIMIT_PRICE"):
        run(configured, allow_live_orders=True)


def test_private_mode_requires_maker_credentials(configured):
    configured.values["BOND_MODE"] = "private"
    with pytest.raises(SafetyError, match="BOND_TESTNET_WALLET_KEY"):
        run(configured)


def test_flag_cannot_enable_orders_in_other_modes(configured):
    with pytest.raises(SafetyError, match="only valid with trade"):
        run(configured, allow_live_orders=True)


def test_private_and_stream_modes_use_configured_identity_and_session_files(
    configured, monkeypatch, capsys
):
    calls = []

    class Client:
        def __init__(self, transport):
            calls.append(("config", transport.config))

        def authenticate_cached(self, wallet, key, name, path):
            calls.append(("session", name, path))

        def identity(self):
            return {"id": "backend-uuid", "name": "encoded-name"}

        def reconcile_account(self, symbol):
            calls.append(("reconcile", symbol))
            return SimpleNamespace(positions=[{}], open_orders=[])

        def user_events(self, **kwargs):
            calls.append(("events", kwargs))
            yield SimpleNamespace(kind="connected", reason="snapshot", requires_reconciliation=True)

    monkeypatch.setattr("bond_perpdex_client.quickstart.BondPerpDexClient", Client)
    monkeypatch.setattr(
        "bond_perpdex_client.quickstart.WalletSigner.from_environment", lambda values: object()
    )
    monkeypatch.setattr(
        "bond_perpdex_client.quickstart.load_ed25519_private_key",
        lambda path: calls.append(("key", path)),
    )
    for mode in ["private", "stream"]:
        configured.values["BOND_MODE"] = mode
        configured.values["BOND_MAX_EVENTS"] = "7"
        configured.values["BOND_STREAM_IDLE_TIMEOUT_SECONDS"] = "15"
        run(configured)
    assert ("session", "tradegods-z", configured.root / ".local/galileo.session.json") in calls
    assert ("events", {"max_events": 7, "max_reconnects": 0, "idle_timeout": 15}) in calls
    assert all(
        call[1].allow_live_private and not call[1].allow_live_orders
        for call in calls
        if call[0] == "config"
    )
    assert "backend-uuid" in capsys.readouterr().out


def test_show_config_does_not_authenticate_or_emit_wallet_secret(configured, monkeypatch, capsys):
    for name in BY_NAME:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOND_TESTNET_WALLET_KEY", "never-log-this")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "quickstart",
            "--env-file",
            str(configured.root / ".env"),
            "--mode",
            "private",
            "--show-config",
        ],
    )
    main()
    output = capsys.readouterr().out
    assert "<set>" in output and "never-log-this" not in output


def test_supervised_trade_wires_operator_values_and_risk_config(configured, monkeypatch):
    from decimal import Decimal

    from bond_perpdex_client.offline import NOW_MS, OFFLINE_REGISTRY

    calls = []

    class Client:
        def __init__(self, transport):
            calls.append(("config", transport.config))

        def authenticate_cached(self, *args):
            calls.append(("authenticate", args[2]))

        def identity(self):
            return {"id": "backend-uuid", "name": "encoded-name"}

        def reconcile_account(self, symbol):
            return SimpleNamespace(positions=[{}], open_orders=[])

        def market(self, symbol):
            return "verified-market"

        def prepare_order(self, wallet, market, quote, **kwargs):
            calls.append(("prepare", market, quote, kwargs))
            return SimpleNamespace(client_order_id="client-id", digest="digest")

        def submit_order(self, intent):
            calls.append(("submit", intent))
            return {"orderId": "order-id"}

        def cancel_order(self, market, order_id):
            calls.append(("cancel", market, order_id))

        def query_order(self, symbol, **kwargs):
            return {"status": "CANCELED"}

    configured.values.update(
        {
            "BOND_MODE": "trade",
            "BOND_LIMIT_PRICE": "65000",
            "BOND_ORDER_SIDE": "SELL",
            "BOND_QUANTITY": "0.003",
            "BOND_GALILEO_RPC_URL": "https://rpc.example",
        }
    )
    monkeypatch.setattr("bond_perpdex_client.quickstart.BondPerpDexClient", Client)
    monkeypatch.setattr(
        "bond_perpdex_client.quickstart.WalletSigner.from_environment", lambda values: object()
    )
    monkeypatch.setattr(
        "bond_perpdex_client.quickstart.load_ed25519_private_key", lambda path: object()
    )
    monkeypatch.setattr(
        "bond_perpdex_client.quickstart.SigningRegistry.from_file", lambda path: OFFLINE_REGISTRY
    )
    monkeypatch.setattr(
        "bond_perpdex_client.quickstart.read_endpoint_time", lambda url, registry: NOW_MS // 1000
    )
    run(configured, allow_live_orders=True)
    config = next(c[1] for c in calls if c[0] == "config")
    assert config.allow_live_private and config.allow_live_orders
    assert config.signing_registry is OFFLINE_REGISTRY
    prepared = next(c for c in calls if c[0] == "prepare")
    assert prepared[2].side == "SELL" and prepared[2].price == Decimal("65000")
    assert prepared[2].quantity == Decimal("0.003")
    assert prepared[3] == {"subaccount": "tradegods-z", "endpoint_time": NOW_MS // 1000}
    assert ("cancel", "verified-market", "order-id") in calls


def test_setup_cli_generates_a_key_and_preserves_edits(tmp_path, monkeypatch, capsys):
    from bond_perpdex_client.setup import main as setup_main

    for name in BY_NAME:
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "maker" / ".env"
    monkeypatch.setattr(
        sys,
        "argv",
        ["setup", "--env-file", str(path), "--subaccount", "team-z", "--generate-signing-key"],
    )
    setup_main()
    key = path.parent / ".local/galileo-ed25519.pem"
    saved = path.read_bytes(), key.read_bytes()
    setup_main()
    assert saved == (path.read_bytes(), key.read_bytes())
    assert "team-z" in path.read_text()
    assert "PRIVATE KEY" not in capsys.readouterr().out
