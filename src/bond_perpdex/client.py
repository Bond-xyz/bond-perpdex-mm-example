"""Small public facade combining session, account, and order operations."""

from .client_account import AccountQueries
from .client_orders import OrderCommands
from .client_session import Session, SessionClient


class BondPerpDexClient(OrderCommands, AccountQueries, SessionClient):
    """Testnet client with explicit permissions and no automatic mutation retry."""


__all__ = ["BondPerpDexClient", "Session"]
