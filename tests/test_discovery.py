import copy
from dataclasses import replace

import pytest

from bond_perpdex_client import BondPerpDexClient, SafetyError, SigningRegistry
from bond_perpdex_client import TestnetConfig as Config
from bond_perpdex_client.offline import EXCHANGE_INFO, OFFLINE_REGISTRY


class PublicVenue:
    offline = False

    def __init__(self):
        self.info = copy.deepcopy(EXCHANGE_INFO)
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        assert request.method == "GET" and request.path == "/fapi/v1/exchangeInfo"
        assert not request.headers and not request.body
        return copy.deepcopy(self.info)


def test_public_discovery_needs_no_credentials_or_registry_file():
    venue = PublicVenue()
    client = BondPerpDexClient(venue)
    market = client.market("BTCUSDCPERP")
    assert market.product_id == 2
    assert market.virtual_book == OFFLINE_REGISTRY.markets[market.symbol][1]
    assert client.signing_registry.endpoint == OFFLINE_REGISTRY.endpoint
    assert len(venue.requests) == 1


@pytest.mark.parametrize(
    "change",
    [
        "missing_chain",
        "wrong_chain",
        "bool_chain",
        "missing_endpoint",
        "zero_endpoint",
        "bad_endpoint",
        "missing_product",
        "bool_product",
        "negative_product",
        "large_product",
        "missing_book",
        "zero_book",
        "bad_book",
        "duplicate_symbol",
        "duplicate_product",
        "duplicate_book",
        "empty_symbols",
        "not_a_list",
        "not_an_object",
    ],
)
def test_incomplete_or_ambiguous_public_discovery_stops_before_signing(change):
    venue = PublicVenue()
    data = venue.info
    item = data["symbols"][0]
    if change == "missing_chain":
        del data["chainId"]
    elif change == "wrong_chain":
        data["chainId"] = 1
    elif change == "bool_chain":
        data["chainId"] = True
    elif change == "missing_endpoint":
        del data["endpoint"]
    elif change == "zero_endpoint":
        data["endpoint"] = "0x" + "00" * 20
    elif change == "bad_endpoint":
        data["endpoint"] = "invalid"
    elif change == "missing_product":
        del item["productId"]
    elif change == "bool_product":
        item["productId"] = True
    elif change == "negative_product":
        item["productId"] = -1
    elif change == "large_product":
        item["productId"] = 2**32
    elif change == "missing_book":
        del item["virtualBook"]
    elif change == "zero_book":
        item["virtualBook"] = "0x" + "00" * 20
    elif change == "bad_book":
        item["virtualBook"] = "invalid"
    elif change == "duplicate_symbol":
        data["symbols"].append(copy.deepcopy(item))
    elif change == "duplicate_product":
        data["symbols"][1]["productId"] = item["productId"]
    elif change == "duplicate_book":
        data["symbols"][1]["virtualBook"] = item["virtualBook"]
    elif change == "empty_symbols":
        data["symbols"] = []
    elif change == "not_a_list":
        data["symbols"] = {}
    elif change == "not_an_object":
        data["symbols"] = [None]
    client = BondPerpDexClient(venue)
    with pytest.raises(SafetyError, match="exchangeInfo signing context"):
        client.market("BTCUSDCPERP")
    assert client.signing_registry is None
    assert client.halted
    assert len(venue.requests) == 1


@pytest.mark.parametrize("field", ["endpoint", "virtualBook", "productId"])
def test_mapping_rotation_requires_explicit_reconciliation_and_new_client(field):
    venue = PublicVenue()
    client = BondPerpDexClient(venue)
    original = client.discover_signing_context()
    if field == "endpoint":
        venue.info[field] = "0x" + "77" * 20
    else:
        venue.info["symbols"][0][field] = 10 if field == "productId" else "0x" + "77" * 20
    with pytest.raises(SafetyError, match="changed"):
        client.market("BTCUSDCPERP")
    assert client.signing_registry is original
    assert client.halted


def test_optional_operator_pin_must_match_public_discovery():
    venue = PublicVenue()
    pin = replace(OFFLINE_REGISTRY, endpoint="0x" + "77" * 20)
    client = BondPerpDexClient(venue, config=Config(signing_registry=pin))
    with pytest.raises(SafetyError, match="configured pin"):
        client.discover_signing_context()
    assert client.signing_registry is pin
    assert SigningRegistry.from_exchange_info(EXCHANGE_INFO).markets == OFFLINE_REGISTRY.markets


def test_discovery_cli_prints_validated_public_fields(monkeypatch, capsys):
    import json

    from bond_perpdex_client.discovery import main

    monkeypatch.setattr(
        "bond_perpdex_client.discovery.BondPerpDexClient",
        lambda: BondPerpDexClient(PublicVenue()),
    )
    main()
    data = json.loads(capsys.readouterr().out)
    assert data["chainId"] == 16602
    assert data["endpoint"] == OFFLINE_REGISTRY.endpoint
    assert data["markets"]["BTCUSDCPERP"]["productId"] == 2


def test_failed_discovery_prevents_session_registration(monkeypatch, tmp_path):
    from bond_perpdex_client.workflows import open_account

    class Client:
        def __init__(self, transport):
            pass

        def discover_signing_context(self):
            raise SafetyError("Discovery unavailable")

        def authenticate(self, *args):
            pytest.fail("Must validate public discovery before replacing a session")

    monkeypatch.setattr("bond_perpdex_client.workflows.BondPerpDexClient", Client)
    monkeypatch.setattr(
        "bond_perpdex_client.workflows.WalletSigner.from_environment", lambda _: None
    )
    monkeypatch.setattr("bond_perpdex_client.workflows.load_ed25519_private_key", lambda _: None)
    with pytest.raises(SafetyError, match="Discovery unavailable"):
        open_account(
            Config(allow_live_private=True, allow_live_orders=True),
            subaccount="tradegods-z",
            key_file=tmp_path / "unused.pem",
        )
