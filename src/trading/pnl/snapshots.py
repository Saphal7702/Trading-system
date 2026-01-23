from __future__ import annotations

from dataclasses import asdict
from typing import Any

import logging

from ..db import connect

log = logging.getLogger("trading")


def upsert_account_snapshot(asof_date: str, account: Any, run_id: int | None = None) -> dict[str, Any]:
    """
    Store a daily snapshot for equity curve.
    `account` is whatever broker.get_account() returns (likely your AccountSummary dataclass).
    """
    # support dataclass or plain object
    data = asdict(account) if hasattr(account, "__dataclass_fields__") else account.__dict__

    def f(x):
        try:
            return None if x is None else float(x)
        except Exception:
            return None

    cash = f(data.get("cash"))
    equity = f(data.get("equity"))
    buying_power = f(data.get("buying_power"))
    long_mv = f(data.get("long_market_value"))

    if equity is None:
        return {"skipped": True, "reason": "missing_equity"}

    # If you don't have cash in AccountSummary yet, you can temporarily set it to 0
    # but best is to add cash/long_market_value into alpaca_broker.get_account().
    if cash is None:
        cash = 0.0

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO account_snapshots_daily(asof_date, cash, equity, buying_power, long_market_value, run_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(asof_date) DO UPDATE SET
              cash=excluded.cash,
              equity=excluded.equity,
              buying_power=excluded.buying_power,
              long_market_value=excluded.long_market_value,
              run_id=excluded.run_id,
              created_at=datetime('now');
            """,
            (asof_date, cash, equity, buying_power, long_mv, run_id),
        )

    return {"asof": asof_date, "cash": cash, "equity": equity, "buying_power": buying_power, "long_mv": long_mv, "run_id": run_id}
