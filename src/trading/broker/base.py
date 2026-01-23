from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol

@dataclass(frozen=True)
class AccountSummary:
    broker: str
    account_id: str
    status: str | None = None
    currency: str | None = None
    buying_power: float | None = None
    equity: float | None = None

    # Phase 3 additions (additive)
    cash: float | None = None
    long_market_value: float | None = None


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    qty: float
    avg_entry_price: float | None


class Broker(Protocol):
    name: str

    def get_account(self) -> AccountSummary: ...
    def list_positions(self) -> PositionSnapshot: ...