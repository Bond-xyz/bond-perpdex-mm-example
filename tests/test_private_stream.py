import base64
import copy
import json

import pytest
from cryptography.exceptions import InvalidSignature

from bond_perpdex import (
    BondPerpDexClient,
    SafetyError,
)
from bond_perpdex import (
    TestnetConfig as Config,
)
from bond_perpdex import (
    TestnetTransport as LiveTransport,
)
from bond_perpdex.client import Session
from bond_perpdex.offline import NOW_MS, demo_identity

EXECUTION = {
    "e": "executionReport",
    "E": NOW_MS,
    "I": 10,
    "s": "BTCUSDCPERP",
    "i": "00000000-0000-4000-8000-000000000002",
    "x": "TRADE",
    "X": "PARTIALLY_FILLED",
    "S": "BUY",
    "q": "0.002",
    "p": "65000",
    "l": "0.001",
    "z": "0.001",
    "L": "65000",
    "Z": "65",
    "Y": "65",
    "T": NOW_MS,
    "t": 1,
    "w": True,
    "m": True,
}
POSITION = {
    "e": "positionUpdate",
    "E": NOW_MS,
    "I": 11,
    "eventId": "00000000-0000-4000-8000-000000000003",
    "accountNonce": 4,
    "symbol": "BTCUSDCPERP",
    "positionAmt": "0.001",
    "virtualQuoteBalance": "-65",
    "isolatedMargin": "10",
    "reservedMargin": "0",
    "leverage": 10,
    "perpWalletQuoteBalance": "200",
}


class Socket:
    def __init__(self, key, events, *, before_ack=False, rpc_error=False, subscription_id=1):
        self.key = key
        self.events = copy.deepcopy(events)
        self.frames = []
        self.sent = []
        self.before_ack = before_ack
        self.rpc_error = rpc_error
        self.subscription_id = subscription_id
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def send(self, raw):
        request = json.loads(raw)
        self.sent.append(request)
        method, params = request["method"], request["params"]
        if method == "session.logon":
            assert set(params) == {"apiKey", "timestamp", "recv_window", "signature"}
            assert params["recv_window"] == "5000" and params["timestamp"] == NOW_MS
            signature = base64.b64decode(params["signature"])
            self.key.public_key().verify(signature, b"recvWindow=5000")
            with pytest.raises(InvalidSignature):
                self.key.public_key().verify(signature, b"recvWindow=5000&timestamp=1770000000000")
            result = {"apiKey": params["apiKey"]}
        else:
            assert method == "userDataStream.subscribe" and params == {}
            result = {"subscriptionId": self.subscription_id}
            if self.before_ack:
                self.frames.append(
                    {"subscriptionId": self.subscription_id, "event": self.events.pop(0)}
                )
        self.frames.append(
            {"id": request["id"], "status": 401 if self.rpc_error else 200, "result": result}
        )
        if method == "userDataStream.subscribe":
            self.frames.extend(
                {"subscriptionId": self.subscription_id, "event": event} for event in self.events
            )

    def recv(self, timeout):
        assert 0 < timeout <= 5
        if not self.frames:
            raise TimeoutError("Mocked idle timeout")
        return json.dumps(self.frames.pop(0))


def setup_stream(monkeypatch, plans):
    config = Config(allow_live_private=True)
    client = BondPerpDexClient(LiveTransport(config), clock_ms=lambda: NOW_MS)
    wallet, key = demo_identity()
    client._session = Session("offline-stream-token", key, NOW_MS + 60_000, wallet.address, "bond")
    sockets = [Socket(key, **plan) for plan in plans]
    pending = iter(sockets)
    calls = []

    def connect(url, **kwargs):
        assert url == "wss://perpdex-testnet.bond.xyz/ws-fapi/v1"
        assert kwargs["proxy"] is None and kwargs["max_queue"] == 16
        calls.append(url)
        return next(pending)

    monkeypatch.setattr("bond_perpdex.transport.connect", connect)
    monkeypatch.setattr("bond_perpdex.user_stream.time.sleep", lambda _: None)
    return client, sockets, calls


def test_exact_logon_signature_subscription_and_source_event_variants(monkeypatch):
    events = [
        EXECUTION,
        POSITION,
        {"e": "outboundAccountPosition"},
        {"e": "balanceUpdate", "E": NOW_MS, "a": "USDC", "d": "2", "T": NOW_MS},
    ]
    client, sockets, _ = setup_stream(monkeypatch, [{"events": events, "before_ack": True}])
    result = list(client.user_events(max_events=4, max_reconnects=0))
    assert result[0].kind == "connected" and result[0].requires_reconciliation
    assert [message.event for message in result[1:]] == events
    assert sockets[0].closed and client.stream_requires_reconciliation
    assert all(
        request["method"] in {"session.logon", "userDataStream.subscribe"}
        for request in sockets[0].sent
    )


def test_termination_reconnect_relogon_and_process_sequence_reset_without_command_replay(
    monkeypatch,
):
    reset = {**POSITION, "I": 2}
    client, sockets, calls = setup_stream(
        monkeypatch,
        [
            {"events": [EXECUTION, {"e": "eventStreamTerminated", "E": NOW_MS}]},
            {"events": [reset]},
        ],
    )
    client.halted = True
    messages = list(client.user_events(max_events=3, max_reconnects=1))
    assert len(calls) == 2 and client.halted
    assert any(message.kind == "disconnected" for message in messages)
    assert messages[-1].reason == "sequence_reset_or_duplicate"
    assert messages[-1].requires_reconciliation
    for socket in sockets:
        assert [request["method"] for request in socket.sent] == [
            "session.logon",
            "userDataStream.subscribe",
        ]


def test_global_sequence_jump_is_not_mislabeled_as_proven_per_account_loss(monkeypatch):
    client, _, _ = setup_stream(monkeypatch, [{"events": [EXECUTION, {**POSITION, "I": 40}]}])
    messages = list(client.user_events(max_events=2, max_reconnects=0))
    assert messages[-1].reason == "global_sequence_jump" and messages[-1].requires_reconciliation


def test_bounded_idle_reconnect_exhaustion_and_no_silent_refresh(monkeypatch):
    client, _, calls = setup_stream(monkeypatch, [{"events": []}] * 3)
    with pytest.raises(SafetyError, match="budget exhausted"):
        list(client.user_events(max_events=1, max_reconnects=2))
    assert len(calls) == 3 and client.stream_requires_reconciliation


def test_logon_error_does_not_retry_or_echo_session_token(monkeypatch):
    client, sockets, calls = setup_stream(monkeypatch, [{"events": [], "rpc_error": True}])
    with pytest.raises(SafetyError) as error:
        list(client.user_events(max_events=1))
    assert len(calls) == 1 and len(sockets[0].sent) == 1
    assert "offline-stream-token" not in str(error.value)


def test_expired_session_fails_before_websocket_io(monkeypatch):
    client, _, calls = setup_stream(monkeypatch, [{"events": []}])
    client.clock_ms = lambda: NOW_MS + 60_001
    with pytest.raises(SafetyError, match="current session"):
        list(client.user_events())
    assert not calls


def test_session_expiring_during_receive_stops_before_delivering_event(monkeypatch):
    client, sockets, calls = setup_stream(monkeypatch, [{"events": [EXECUTION]}])
    clock = [NOW_MS]
    client.clock_ms = lambda: clock[0]
    receive = sockets[0].recv

    def recv(timeout):
        frame = receive(timeout)
        if "event" in json.loads(frame):
            clock[0] = NOW_MS + 60_001
        return frame

    sockets[0].recv = recv
    with pytest.raises(SafetyError, match="current session"):
        list(client.user_events(max_events=1))
    assert len(calls) == 1 and sockets[0].closed


def test_stale_private_event_requires_reconciliation(monkeypatch):
    client, _, _ = setup_stream(monkeypatch, [{"events": [{**EXECUTION, "E": NOW_MS - 6000}]}])
    messages = list(client.user_events(max_events=1))
    assert messages[-1].reason == "stale_or_future_notification"
    assert messages[-1].requires_reconciliation


def test_terminal_event_at_requested_bound_does_not_open_an_extra_connection(monkeypatch):
    client, _, calls = setup_stream(
        monkeypatch, [{"events": [{"e": "eventStreamTerminated", "E": NOW_MS}]}]
    )
    list(client.user_events(max_events=1, max_reconnects=2))
    assert len(calls) == 1


def test_private_stream_bounds_fail_before_connect(monkeypatch):
    client, _, calls = setup_stream(monkeypatch, [{"events": []}])
    for params in [{"max_events": 0}, {"max_reconnects": 4}, {"max_events": True}]:
        with pytest.raises(SafetyError):
            list(client.user_events(**params))
    assert not calls


@pytest.mark.parametrize(
    "event",
    [
        {"e": "ACCOUNT_UPDATE", "E": NOW_MS},
        {**POSITION, "I": "11"},
        {
            "e": "positionUpdate",
            "E": NOW_MS,
            "I": 11,
            "symbol": "BTCUSDCPERP",
            "event_id": "not-wire-shape",
        },
    ],
)
def test_wrong_event_variants_fail_closed_without_reconnect(monkeypatch, event):
    client, _, calls = setup_stream(monkeypatch, [{"events": [event]}])
    with pytest.raises((SafetyError, KeyError)):
        list(client.user_events(max_events=1))
    assert len(calls) == 1 and client.stream_requires_reconciliation
