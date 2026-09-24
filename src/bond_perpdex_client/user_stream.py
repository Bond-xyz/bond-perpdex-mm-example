"""Bounded session/subscription reconnect; never transmits or replays order commands."""

import base64
import json
import time
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from websockets.exceptions import ConnectionClosed

from .models import VIRTUAL_BOOKS, Json, SafetyError, UserStreamMessage, decimal
from .signing import canonical_form, compact

if TYPE_CHECKING:
    from .client import BondPerpDexClient


class _Reconnect(Exception):
    pass


def _receive(socket: Any, timeout: float = 5) -> Json:
    if timeout <= 0:
        raise TimeoutError("Private stream acknowledgement deadline exceeded")
    raw = socket.recv(timeout=timeout)
    if not isinstance(raw, str | bytes) or len(raw) > 1_000_000:
        raise SafetyError("Invalid or oversized private stream frame")
    frame = json.loads(raw)
    if not isinstance(frame, dict):
        raise SafetyError("Private stream frame must be an object")
    return frame


def _rpc(
    socket: Any, method: str, params: Json, *, buffer_events: bool = False
) -> tuple[Json, list[Json]]:
    request_id = str(uuid.uuid4())
    socket.send(compact({"id": request_id, "method": method, "params": params}))
    deadline = time.monotonic() + 5
    buffered = []
    for _ in range(33):
        frame = _receive(socket, deadline - time.monotonic())
        if frame.get("id") == request_id:
            if (
                frame.get("status") != 200
                or "error" in frame
                or not isinstance(frame.get("result"), dict)
            ):
                raise SafetyError("Private WebSocket RPC failed; no credential refresh or retry")
            return frame["result"], buffered
        if not buffer_events or "subscriptionId" not in frame or "event" not in frame:
            raise SafetyError("Uncorrelated private WebSocket response")
        buffered.append(frame)
    raise SafetyError("Too many private events before subscription acknowledgement")


def _event(frame: Json, subscription_id: int) -> Json:
    if type(frame.get("subscriptionId")) is not int or frame["subscriptionId"] != subscription_id:
        raise SafetyError("Private event belongs to a different subscription")
    event = frame.get("event")
    if not isinstance(event, dict):
        raise SafetyError("Missing private event object")
    kind = event.get("e")
    if kind not in {
        "executionReport",
        "positionUpdate",
        "balanceUpdate",
        "outboundAccountPosition",
        "eventStreamTerminated",
    }:
        raise SafetyError("Unsupported private event type; reconcile rather than silently ignore")
    if kind != "outboundAccountPosition" and (type(event.get("E")) is not int or event["E"] < 0):
        raise SafetyError("Missing private event timestamp")
    if kind in {"executionReport", "positionUpdate"}:
        if type(event.get("I")) is not int or event["I"] < 1:
            raise SafetyError("Invalid private event sequence")
        symbol = event.get("s") if kind == "executionReport" else event.get("symbol")
        if symbol not in VIRTUAL_BOOKS:
            raise SafetyError("Private event has an unsupported market")
    if kind == "positionUpdate":
        uuid.UUID(event["eventId"])
        if type(event.get("accountNonce")) is not int or event["accountNonce"] < 0:
            raise SafetyError("Invalid position account nonce")
        for key in (
            "positionAmt",
            "virtualQuoteBalance",
            "isolatedMargin",
            "reservedMargin",
            "perpWalletQuoteBalance",
        ):
            decimal(event[key])
    elif kind == "executionReport":
        uuid.UUID(event["i"])
        if event.get("x") not in {"TRADE", "CANCELED", "REJECTED"}:
            raise SafetyError("Unsupported execution transition")
        for key in ("q", "p", "l", "z", "L", "Z", "Y"):
            if key in event:
                decimal(event[key])
    elif kind == "balanceUpdate":
        decimal(event["d"])
    return event


def user_events(
    client: "BondPerpDexClient",
    *,
    max_events: int,
    max_reconnects: int,
) -> Iterator[UserStreamMessage]:
    if type(max_events) is not int or not 1 <= max_events <= 10_000:
        raise SafetyError("Private stream event count must be bounded")
    if type(max_reconnects) is not int or not 0 <= max_reconnects <= 3:
        raise SafetyError("At most three private stream reconnects are allowed")
    client._active_session()
    connect = getattr(client.transport, "private_socket", None)
    if connect is None:
        raise SafetyError("Transport does not implement a private WebSocket")
    delivered = 0
    last_sequence = None
    client.stream_requires_reconciliation = True
    try:
        for generation in range(max_reconnects + 1):
            subscription_id = None
            session = client._active_session()
            try:
                with connect() as socket:
                    signature = base64.b64encode(
                        session.key.sign(canonical_form({"recvWindow": 5000}).encode())
                    ).decode()
                    result, _ = _rpc(
                        socket,
                        "session.logon",
                        {
                            "apiKey": session.api_key,
                            "timestamp": client.clock_ms(),
                            "recv_window": "5000",
                            "signature": signature,
                        },
                    )
                    if result.get("apiKey") != session.api_key:
                        raise SafetyError("Private logon did not acknowledge the current session")
                    if client._active_session() is not session:
                        raise SafetyError("Session changed or expired during logon")
                    result, buffered = _rpc(
                        socket, "userDataStream.subscribe", {}, buffer_events=True
                    )
                    subscription_id = result.get("subscriptionId")
                    if type(subscription_id) is not int or subscription_id < 1:
                        raise SafetyError("Missing private subscription ID")
                    client._stream_connected = True
                    client.stream_requires_reconciliation = True
                    client._stream_revision += 1
                    yield UserStreamMessage(
                        "connected",
                        generation,
                        subscription_id,
                        True,
                        "rest_reconciliation_required",
                    )
                    while delivered < max_events:
                        if client._active_session() is not session:
                            raise SafetyError(
                                "Session changed during private stream; reconnect explicitly"
                            )
                        frame = buffered.pop(0) if buffered else _receive(socket)
                        if client._active_session() is not session:
                            raise SafetyError("Session changed or expired while receiving an event")
                        event = _event(frame, subscription_id)
                        reason = "notification"
                        requires_reconciliation = False
                        if "E" in event and not 0 <= client.clock_ms() - event["E"] <= 5000:
                            reason, requires_reconciliation = "stale_or_future_notification", True
                        if event["e"] in {"executionReport", "positionUpdate"}:
                            sequence = event["I"]
                            if last_sequence is not None and sequence <= last_sequence:
                                reason, requires_reconciliation = (
                                    "sequence_reset_or_duplicate",
                                    True,
                                )
                            elif last_sequence is not None and sequence != last_sequence + 1:
                                reason, requires_reconciliation = "global_sequence_jump", True
                            last_sequence = sequence
                        if event["e"] in {"eventStreamTerminated", "outboundAccountPosition"}:
                            reason, requires_reconciliation = event["e"], True
                        if requires_reconciliation:
                            client.stream_requires_reconciliation = True
                            client._stream_revision += 1
                        delivered += 1
                        yield UserStreamMessage(
                            "event",
                            generation,
                            subscription_id,
                            client.stream_requires_reconciliation,
                            reason,
                            event,
                        )
                        if delivered >= max_events:
                            return
                        if event["e"] == "eventStreamTerminated":
                            raise _Reconnect()
                    return
            except (ConnectionClosed, ConnectionError, OSError, TimeoutError, _Reconnect):
                client._stream_connected = False
                client.stream_requires_reconciliation = True
                client._stream_revision += 1
                yield UserStreamMessage(
                    "disconnected",
                    generation,
                    subscription_id,
                    True,
                    "no_replay_rest_reconciliation_required",
                )
                if generation == max_reconnects:
                    raise SafetyError("Private stream reconnect budget exhausted") from None
                time.sleep(min(0.25 * 2**generation, 1))
            finally:
                client._stream_connected = False
    finally:
        client.stream_requires_reconciliation = True
        client._stream_revision += 1
