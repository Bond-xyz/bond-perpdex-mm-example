import hashlib
import json

import pytest

from bond_perpdex_client import BondPerpDexClient, SafetyError, SigningRegistry
from bond_perpdex_client.models import Market
from bond_perpdex_client.offline import EXCHANGE_INFO, OFFLINE_REGISTRY, demo_identity
from bond_perpdex_client.signing import subaccount_sender


def manifest():
    return {
        "chainId": 16602,
        "contracts": {"endpointProxy": OFFLINE_REGISTRY.endpoint},
        "markets": {
            s: {"productId": p, "virtualBook": b} for s, (p, b) in OFFLINE_REGISTRY.markets.items()
        },
    }


def test_registry_loads_canonical_schema_and_tracks_exact_file_digest(tmp_path):
    data = manifest()
    data["markets"]["BTCUSDCPERP"]["virtualBook"] = "0x" + "66" * 20
    path = tmp_path / "perpdex.json"
    path.write_text(json.dumps(data))
    registry = SigningRegistry.from_file(path)
    assert registry.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    market = Market.from_exchange_info(EXCHANGE_INFO, "BTCUSDCPERP", registry)
    assert market.virtual_book == "0x" + "66" * 20
    with pytest.raises(TypeError):
        registry.markets["BTCUSDCPERP"] = (2, "0x" + "77" * 20)
    data["markets"]["BTCUSDCPERP"]["productId"] = 999
    assert registry.markets["BTCUSDCPERP"][0] == 2


@pytest.mark.parametrize(
    "change",
    [
        "chain",
        "bool_product",
        "zero_product",
        "zero_address",
        "bad_address",
        "duplicate_product",
        "duplicate_book",
        "no_markets",
    ],
)
def test_invalid_or_ambiguous_signing_context_is_rejected(change):
    data = manifest()
    btc = data["markets"]["BTCUSDCPERP"]
    if change == "chain":
        data["chainId"] = 1
    elif change == "bool_product":
        btc["productId"] = True
    elif change == "zero_product":
        btc["productId"] = 0
    elif change == "zero_address":
        btc["virtualBook"] = "0x" + "00" * 20
    elif change == "bad_address":
        btc["virtualBook"] = "garbage"
    elif change == "duplicate_product":
        btc["productId"] = 4
    elif change == "duplicate_book":
        btc["virtualBook"] = data["markets"]["ETHUSDCPERP"]["virtualBook"]
    elif change == "no_markets":
        data["markets"] = {}
    with pytest.raises(ValueError):
        SigningRegistry.from_dict(data)


def test_no_signing_context_is_guessed_from_exchange_info_or_live_defaults():
    with pytest.raises(SafetyError, match="verified signing registry"):
        BondPerpDexClient().market("BTCUSDCPERP")
    data = manifest()
    del data["markets"]["BTCUSDCPERP"]
    with pytest.raises(SafetyError, match="verified testnet registry"):
        Market.from_exchange_info(EXCHANGE_INFO, "BTCUSDCPERP", SigningRegistry.from_dict(data))


def test_named_subaccounts_support_branding_and_are_wallet_scoped():
    wallet, _ = demo_identity()
    a = subaccount_sender(wallet.address, "tradegods-z")
    assert a.endswith("7472616465676f64732d7a00")
    assert a != subaccount_sender("0x" + "12" * 20, "tradegods-z")
    assert len(subaccount_sender(wallet.address, "a" * 12)) == 66
    for invalid in ["", "a" * 13, "a b", "name\x00", "träd", "a\x7f"]:
        with pytest.raises(SafetyError):
            subaccount_sender(wallet.address, invalid)


def test_duplicate_json_fields_cannot_silently_select_a_signing_domain(tmp_path):
    path = tmp_path / "ambiguous.json"
    path.write_text('{"chainId":1,"chainId":16602,"markets":{},"contracts":{}}')
    with pytest.raises(ValueError, match="duplicate registry field"):
        SigningRegistry.from_file(path)
