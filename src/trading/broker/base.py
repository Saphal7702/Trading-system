from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol

@dataclass(frozen=True)
class AccountSummary:
    broker: str
    account_id: str | None
    status: str | None
    currency: str | None
    buying_power: float | None
    equity: float | None

@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    quantity: float
    average_entry_price: float | None

class Broker(Protocol):
    name: str

    def get_account(self) -> AccountSummary: ...
    def list_positions(self) -> PositionSnapshot: ...