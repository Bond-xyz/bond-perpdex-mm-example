"""Small, side-effect-free views of an already signed order request."""

import json
import uuid
from urllib.parse import parse_qs

from .models import Json, OrderIntent, SafetyError


def websocket_order_place(intent: OrderIntent, *, request_id: str | None = None) -> Json:
    """Build ``order.place`` params from the exact signed REST order fields.

    The exchange accepts a structured ``signedOrder`` object on WebSocket, but
    the Ed25519 ``signature`` still covers the compact JSON form value. Build
    that signature once in ``prepare_order``; do not sign a second, differently
    serialized object. This function does not send or retry the command.
    """
    request = intent.request
    if (request.method, request.path) != ("POST", "/fapi/v1/order"):
        raise SafetyError("Expected a prepared perpetual order")
    fields = parse_qs(request.body, strict_parsing=True)
    if any(len(values) != 1 for values in fields.values()):
        raise SafetyError("Duplicate order form field")
    params = {key: values[0] for key, values in fields.items()}
    if "signedOrder" not in params or "signature" not in params:
        raise SafetyError("Both order signatures are required")
    api_key = request.headers.get("x-api-key")
    if not api_key:
        raise SafetyError("Prepared order has no wallet session")
    try:
        envelope = json.loads(params["signedOrder"])
        timestamp = int(params["timestamp"])
    except (KeyError, ValueError, TypeError):
        raise SafetyError("Invalid prepared order envelope or timestamp") from None
    if (
        not isinstance(envelope, dict)
        or not isinstance(envelope.get("hashes"), dict)
        or envelope["hashes"].get("digest") != intent.digest
        or params.get("newClientOrderId") != intent.client_order_id
    ):
        raise SafetyError("Prepared order identity changed")
    params["signedOrder"] = envelope
    params["timestamp"] = timestamp
    params["apiKey"] = api_key
    return {
        "id": request_id or str(uuid.uuid4()),
        "method": "order.place",
        "params": params,
    }
