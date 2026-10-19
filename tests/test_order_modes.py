import copy
import json
from dataclasses import replace
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from bond_perpdex_client import (
    GALILEO_RPC_URL,
    Market,
    PriceBand,
    Quote,
    SafetyError,
    UnknownOutcome,
    decode_expiration,
    encode_expiration,
    fair_price_band,
    marketable_limit_price,
    read_endpoint_time,
)
from bond_perpdex_client.configuration import Settings
from bond_perpdex_client.models import PreparedRequest, units
from bond_perpdex_client.offline import (
    EXCHANGE_INFO,
    NOW_MS,
    OFFLINE_REGISTRY,
    PREMIUM_INDEX,
    demo_identity,
)
from bond_perpdex_client.setup import ensure_environment
from bond_perpdex_client.signing import RESERVED_MASK, signed_form
from bond_perpdex_client.transport import TestnetReadOnlyTransport as ReadOnlyTransport

EXPIRY = NOW_MS // 1000 + 60


# --- encode_expiration -------------------------------------------------------------


@pytest.mark.parametrize(
    "order_type,time_in_force,mode",
    [("LIMIT", "GTC", 0), ("LIMIT", "IOC", 1), ("LIMIT", "FOK", 2), ("POST_ONLY", "GTC", 3)],
)
def test_every_signed_mode_occupies_bits_62_and_63(order_type, time_in_force, mode):
    encoded = encode_expiration(order_type, time_in_force, False, EXPIRY)
    assert encoded == (mode << 62) | EXPIRY
    assert encoded & RESERVED_MASK == 0
    assert decode_expiration(encoded) == (order_type, time_in_force, False, EXPIRY)


def test_post_only_encoding_is_unchanged_for_existing_callers():
    assert encode_expiration("POST_ONLY", "GTC", False, EXPIRY) == EXPIRY | (3 << 62)


@pytest.mark.parametrize("time_in_force,mode", [("IOC", 1), ("FOK", 2)])
def test_reduce_only_sets_bit_61_for_ioc_and_fok(time_in_force, mode):
    encoded = encode_expiration("LIMIT", time_in_force, True, EXPIRY)
    assert encoded == (mode << 62) | (1 << 61) | EXPIRY
    assert decode_expiration(encoded) == ("LIMIT", time_in_force, True, EXPIRY)


@pytest.mark.parametrize("order_type", ["LIMIT", "POST_ONLY"])
def test_reduce_only_is_rejected_for_resting_gtc_orders(order_type):
    with pytest.raises(SafetyError, match="IOC or FOK"):
        encode_expiration(order_type, "GTC", True, EXPIRY)
    with pytest.raises(SafetyError, match="IOC or FOK"):
        decode_expiration(encode_expiration(order_type, "GTC", False, EXPIRY) | (1 << 61))


@pytest.mark.parametrize(
    "order_type,time_in_force",
    [("MARKET", "IOC"), ("MARKET", "GTC"), ("POST_ONLY", "IOC"), ("LIMIT", "GTX"), ("LIMIT", "")],
)
def test_unsupported_modes_including_signed_market_fail_closed(order_type, time_in_force):
    with pytest.raises(SafetyError, match="LIMIT/IOC"):
        encode_expiration(order_type, time_in_force, False, EXPIRY)


@pytest.mark.parametrize("bit", [58, 59, 60])
def test_reserved_bits_must_be_zero(bit):
    with pytest.raises(SafetyError, match="reserved"):
        decode_expiration(encode_expiration("LIMIT", "IOC", False, EXPIRY) | (1 << bit))


@pytest.mark.parametrize("expires_at", [0, -1, 2**58, 2**63, True, "1", 1.0])
def test_expiry_must_be_a_positive_58_bit_integer(expires_at):
    with pytest.raises(SafetyError, match="58"):
        encode_expiration("LIMIT", "IOC", False, expires_at)


def test_expiry_range_boundaries_and_flag_types():
    assert encode_expiration("LIMIT", "GTC", False, 2**58 - 1) == 2**58 - 1
    assert encode_expiration("LIMIT", "GTC", False, 1) == 1
    with pytest.raises(SafetyError, match="boolean"):
        encode_expiration("LIMIT", "IOC", 1, EXPIRY)
    for value in (-1, 2**64, "1"):
        with pytest.raises(SafetyError, match="uint64"):
            decode_expiration(value)
    with pytest.raises(SafetyError, match="58"):
        decode_expiration(3 << 62)


# --- marketable LIMIT/IOC price ----------------------------------------------------


def market_with(*filters):
    info = copy.deepcopy(EXCHANGE_INFO)
    info["symbols"][0]["filters"] += list(filters)
    return Market.from_exchange_info(info, "BTCUSDCPERP", OFFLINE_REGISTRY)


INDEX = Decimal("65005")  # 300 bps band: 63054.85 to 66955.15; tick 0.10


@pytest.mark.parametrize(
    "side,worst,expected",
    [
        ("BUY", None, "66955.1"),  # floor of the upper band edge
        ("BUY", "70000", "66955.1"),  # clamped into the band
        ("BUY", "65100.07", "65100.0"),  # floored to the tick, never above the limit
        ("SELL", None, "63054.9"),  # ceil of the lower band edge
        ("SELL", "60000", "63054.9"),
        ("SELL", "64900.03", "64900.1"),  # ceiled to the tick, never below the limit
    ],
)
def test_ioc_price_is_tick_aligned_and_clamped_inside_the_default_band(
    connected, side, worst, expected
):
    market = connected[3]
    price = marketable_limit_price(
        market, side, INDEX, worst_price=None if worst is None else Decimal(worst)
    )
    assert price == Decimal(expected)
    assert price % market.tick == 0
    assert INDEX * Decimal("0.97") <= price <= INDEX * Decimal("1.03")


@pytest.mark.parametrize("side,worst", [("BUY", "63000"), ("SELL", "67000")])
def test_worst_price_beyond_the_far_band_edge_is_rejected_not_moved(connected, side, worst):
    with pytest.raises(SafetyError, match="PRICE_OUTSIDE_FAIR_PRICE_BAND"):
        marketable_limit_price(connected[3], side, INDEX, worst_price=Decimal(worst))


def test_configured_band_and_its_bounds(connected):
    market = connected[3]
    assert marketable_limit_price(market, "BUY", INDEX, band_bps=100) == Decimal("65655.0")
    assert marketable_limit_price(market, "SELL", INDEX, band_bps=100) == Decimal("64355.0")
    for band in (0, 10_000, 1.5):
        with pytest.raises(SafetyError, match="basis points"):
            marketable_limit_price(market, "BUY", INDEX, band_bps=band)
    for bad in (Decimal("0"), Decimal("NaN"), 65005):
        with pytest.raises(SafetyError):
            marketable_limit_price(market, "BUY", bad)


# The exact BTC filter advertised by exchangeInfo.
BTC_PERCENT_PRICE = {
    "filterType": "PERCENT_PRICE",
    "multiplierUp": "1.05",
    "multiplierDown": "0.95",
    "multiplierDecimal": "4",
    "referencePrice": "INDEX",
    "restingDeviationBps": 500,
    "executionMultiplierUp": "1.03",
    "executionMultiplierDown": "0.97",
    "executionDeviationBps": 300,
}


def test_ioc_uses_the_advertised_execution_band_over_resting_and_configured_bands():
    market = market_with(BTC_PERCENT_PRICE)
    assert market.resting_band == PriceBand(Decimal("0.95"), Decimal("1.05"))
    assert market.execution_band == PriceBand(Decimal("0.97"), Decimal("1.03"))
    # The configured 100 bps is ignored when the venue advertises its band.
    assert marketable_limit_price(market, "BUY", INDEX, band_bps=100) == Decimal("66955.1")
    assert marketable_limit_price(market, "SELL", INDEX, band_bps=100) == Decimal("63054.9")
    # Inclusive edges: an exactly on-tick edge is admitted.
    assert marketable_limit_price(market, "BUY", Decimal("100")) == Decimal("103.0")
    assert marketable_limit_price(market, "SELL", Decimal("100")) == Decimal("97.0")
    with pytest.raises(SafetyError, match="PRICE_OUTSIDE_FAIR_PRICE_BAND"):
        marketable_limit_price(market, "BUY", INDEX, worst_price=Decimal("63054.8"))


def test_resting_orders_use_the_resting_band():
    market = market_with(BTC_PERCENT_PRICE)
    assert fair_price_band(market, crossing=False) == PriceBand(Decimal("0.95"), Decimal("1.05"))
    assert fair_price_band(market) == PriceBand(Decimal("0.97"), Decimal("1.03"))


def test_ioc_falls_back_to_the_resting_band_without_execution_fields():
    market = market_with(
        {"filterType": "PERCENT_PRICE", "multiplierUp": "1.05", "multiplierDown": "0.95"}
    )
    assert market.execution_band is None
    assert marketable_limit_price(market, "BUY", INDEX, band_bps=100) == Decimal("68255.2")
    assert marketable_limit_price(market, "SELL", INDEX, band_bps=100) == Decimal("61754.8")


def test_no_filter_uses_the_configured_band_for_both_order_kinds(connected):
    market = connected[3]
    assert market.resting_band is None and market.execution_band is None
    band = PriceBand(Decimal("0.975"), Decimal("1.025"))
    assert fair_price_band(market, 250) == fair_price_band(market, 250, crossing=False) == band


def test_execution_band_is_intersected_with_a_narrower_resting_band():
    market = market_with(
        {
            **BTC_PERCENT_PRICE,
            "multiplierUp": "1.02",
            "executionMultiplierDown": "0.90",
        }
    )
    assert fair_price_band(market) == PriceBand(Decimal("0.95"), Decimal("1.02"))


@pytest.mark.parametrize(
    "change",
    [
        {"referencePrice": "MARK"},
        {"executionMultiplierUp": None},
        {"executionMultiplierDown": "1.01"},
    ],
)
def test_unsupported_or_partial_percent_price_fails_market_selection(change):
    percent = {**BTC_PERCENT_PRICE, **change}
    percent = {key: value for key, value in percent.items() if value is not None}
    with pytest.raises(SafetyError):
        market_with(percent)


@pytest.mark.parametrize("down,up", [("1", "1.05"), ("0.95", "1"), ("0", "1.05"), ("x", "1.05")])
def test_invalid_advertised_band_fails_market_selection(down, up):
    with pytest.raises((SafetyError, ArithmeticError)):
        market_with({"filterType": "PERCENT_PRICE", "multiplierUp": up, "multiplierDown": down})


@pytest.mark.parametrize("reduce_only", [False, True])
def test_marketable_order_signs_limit_ioc_with_consistent_price(connected, reduce_only):
    client, venue, wallet, market = connected
    intent = client.prepare_marketable_order(
        wallet, market, "SELL", Decimal("0.002"), reduce_only=reduce_only
    )
    fields = {key: values[0] for key, values in parse_qs(intent.request.body).items()}
    order = json.loads(fields["signedOrder"])["order"]
    assert (fields["type"], fields["timeInForce"]) == ("LIMIT", "IOC")
    assert fields["price"] == "63054.9"  # from the offline premiumIndex indexPrice
    assert int(order["priceX18"]) == units(Decimal(fields["price"]), 18)
    assert fields.get("reduceOnly") == ("true" if reduce_only else None)
    assert decode_expiration(int(order["expiration"])) == (
        "LIMIT",
        "IOC",
        reduce_only,
        NOW_MS // 1000 + 60,
    )
    result = client.submit_offline(intent)
    assert result["status"] == "EXPIRED" and result["type"] == "LIMIT"
    assert client.reconcile_account(market.symbol).open_orders == []


@pytest.mark.parametrize("index_price", [None, "0", "-1", "abc", ""])
def test_missing_or_non_positive_index_price_fails_before_signing(
    connected, monkeypatch, index_price
):
    client, _, wallet, market = connected
    premium = {key: value for key, value in PREMIUM_INDEX.items() if key != "indexPrice"}
    if index_price is not None:
        premium["indexPrice"] = index_price
    monkeypatch.setattr("bond_perpdex_client.offline.PREMIUM_INDEX", premium)
    with pytest.raises(SafetyError, match="no positive indexPrice"):
        client.prepare_marketable_order(wallet, market, "BUY", Decimal("0.002"))
    assert not client._nonces


def test_default_prepare_order_request_keeps_post_only_fields(connected):
    client, _, wallet, market = connected
    intent = client.prepare_order(wallet, market, Quote("BUY", Decimal("65000"), Decimal("0.002")))
    fields = parse_qs(intent.request.body)
    assert set(fields) == {
        "symbol",
        "side",
        "type",
        "timeInForce",
        "price",
        "quantity",
        "newClientOrderId",
        "signedOrder",
        "timestamp",
        "recvWindow",
        "signature",
    }


def test_invalid_mode_is_rejected_before_a_nonce_is_consumed(connected):
    client, _, wallet, market = connected
    with pytest.raises(SafetyError, match="IOC or FOK"):
        client.prepare_order(
            wallet,
            market,
            Quote("BUY", Decimal("65000"), Decimal("0.002")),
            nonce=7,
            reduce_only=True,
        )
    assert 7 not in client._nonces


def test_venue_rejects_request_fields_that_differ_from_signed_bits(connected):
    client, venue, wallet, market = connected
    intent = client.prepare_marketable_order(
        wallet, market, "SELL", Decimal("0.002"), reduce_only=True
    )
    fields = {key: values[0] for key, values in parse_qs(intent.request.body).items()}
    del fields["reduceOnly"], fields["signature"]
    changed = replace(intent.request, body=signed_form(fields, demo_identity()[1]))
    with pytest.raises(UnknownOutcome):
        client.submit_offline(replace(intent, request=changed))
    assert not venue.orders


@pytest.mark.parametrize(
    "path", ["/fapi/v1/premiumIndex", "/fapi/v1/premiumIndex?symbol=X", "/fapi/v1/premiumIndex?a=1"]
)
def test_public_premium_index_read_is_narrowly_allowlisted(path):
    with pytest.raises(SafetyError, match="premiumIndex"):
        ReadOnlyTransport().request(PreparedRequest("GET", path, ""))


# --- RPC default -------------------------------------------------------------------


@pytest.mark.parametrize("configured_url", [None, ""])
def test_endpoint_time_defaults_to_the_public_galileo_rpc(monkeypatch, configured_url):
    urls = []
    real_client = httpx.Client

    def handler(request):
        urls.append(str(request.url))
        method = json.loads(request.content)["method"]
        result = hex(16602) if method == "eth_chainId" else f"0x{NOW_MS // 1000:064x}"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    monkeypatch.setattr(
        "bond_perpdex_client.chain.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    assert GALILEO_RPC_URL == "https://evmrpc-testnet.0g.ai"
    assert read_endpoint_time(configured_url, OFFLINE_REGISTRY) == NOW_MS // 1000
    assert urls == [GALILEO_RPC_URL] * 2


def test_settings_default_the_rpc_but_keep_it_overridable(tmp_path):
    path = tmp_path / ".env"
    ensure_environment(path)
    settings = Settings.load(path, environment={})
    assert settings.values["BOND_GALILEO_RPC_URL"] == GALILEO_RPC_URL
    assert json.loads(settings.redacted_json())["BOND_GALILEO_RPC_URL"] == GALILEO_RPC_URL
    custom = Settings.load(path, environment={"BOND_GALILEO_RPC_URL": "https://rpc.example/k"})
    assert custom.values["BOND_GALILEO_RPC_URL"] == "https://rpc.example/k"
    assert json.loads(custom.redacted_json())["BOND_GALILEO_RPC_URL"] == "<set>"
