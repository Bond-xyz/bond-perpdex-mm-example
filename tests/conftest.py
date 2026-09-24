import socket

import pytest

from bond_perpdex_client import BondPerpDexClient
from bond_perpdex_client.offline import NOW_MS, OfflineVenue, demo_identity


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Tests and offline smoke must not open sockets")

    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(socket, "getaddrinfo", fail)


@pytest.fixture
def connected():
    venue = OfflineVenue()
    client = BondPerpDexClient(venue, clock_ms=lambda: venue.now_ms)
    wallet, key = demo_identity()
    client.authenticate_offline(wallet, key)
    return client, venue, wallet, client.market("BTCUSDCPERP")


@pytest.fixture
def now():
    return NOW_MS
