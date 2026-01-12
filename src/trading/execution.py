from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os

from .db import connect

@dataclass(frozen=True)
class ProposedOrder:
    symbol: str
    side: str   # buy/sell
    qty: float
    reason: str
    idempotency_key: str

def _today_key() -> str:
    return datetime.now(timezone.utc).date().isoformat()

def _make_idempotency_key(symbol: str, side: str, day: str) -> str:
    raw = f"{day}|{symbol}|{side}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def build_orders_from_intents(run_id: int, default_qty: float = 1.0) -> list[ProposedOrder]:
    """
    Build 0+ ProposedOrders from intents, deduping by (symbol, side) for the day.
    If multiple intents exist for same symbol+side, we keep the most recent one.
    """
    day = _today_key()

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT symbol, action, reason, created_at
            FROM intents
            WHERE run_id = ?
              AND action IN ('buy','sell')
            ORDER BY created_at ASC, id ASC;
            """,
            (run_id,),
        ).fetchall()

    # key = (symbol, side) -> keep last
    chosen: dict[tuple[str, str], ProposedOrder] = {}

    for r in rows:
        symbol = str(r["symbol"]).strip().upper()
        side = str(r["action"]).strip().lower()
        reason = r["reason"]

        idem = _make_idempotency_key(symbol, side, day)
        chosen[(symbol, side)] = ProposedOrder(
            symbol=symbol,
            side=side,
            qty=default_qty,
            reason=reason,
            idempotency_key=idem,
        )

    return list(chosen.values())


def persist_orders(run_id: int, orders: list[ProposedOrder]) -> int:
    """
    Insert into DB with idempotency. If already exists, skip.
    """
    if not orders:
        return 0

    inserted = 0
    with connect() as conn:
        for o in orders:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO orders(symbol, side, qty, status, reason, idempotency_key, requested_at)
                VALUES (?, ?, ?, 'created', ?, ?, datetime('now'));
                """,
                (o.symbol, o.side, o.qty, o.reason, o.idempotency_key),
            )
            if cur.rowcount == 1:
                inserted += 1
    return inserted

def is_paper_submit_allowed() -> bool:
    return os.getenv("TRADING_ALLOW_PAPER_ORDERS", "false").strip().lower() in ("1", "true", "yes", "y")
