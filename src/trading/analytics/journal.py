from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
import sqlite3
from datetime import date

JOURNAL_SQL = """
WITH journal AS (
  SELECT
    lc.id                AS closing_id,
    lc.symbol            AS symbol,
    lc.qty_closed        AS qty_closed,

    lc.entry_execution_id AS entry_execution_id,
    lc.entry_filled_at    AS entry_filled_at,
    lc.entry_price        AS entry_price,

    lc.exit_execution_id  AS exit_execution_id,
    lc.exit_filled_at     AS exit_filled_at,
    lc.exit_price         AS exit_price,

    lc.realized_pnl       AS realized_pnl,

    CASE
      WHEN lc.entry_price IS NULL OR lc.entry_price = 0 OR lc.qty_closed IS NULL
        THEN NULL
      ELSE (lc.realized_pnl / (lc.entry_price * lc.qty_closed))
    END                  AS realized_ret,

    CAST(julianday(date(lc.exit_filled_at)) - julianday(date(lc.entry_filled_at)) AS INTEGER)
                         AS holding_days,

    i1.signal_key         AS entry_signal_key,
    i2.signal_key         AS exit_signal_key

  FROM lot_closings lc

  -- Entry attribution chain: lot_closings.entry_execution_id -> executions.order_id -> orders.intent_id -> intents.signal_key
  LEFT JOIN executions e1 ON e1.id = lc.entry_execution_id
  LEFT JOIN orders    o1 ON o1.id = e1.order_id
  LEFT JOIN intents   i1 ON i1.id = o1.intent_id

  -- Exit attribution chain: lot_closings.exit_execution_id -> executions.order_id -> orders.intent_id -> intents.signal_key
  LEFT JOIN executions e2 ON e2.id = lc.exit_execution_id
  LEFT JOIN orders    o2 ON o2.id = e2.order_id
  LEFT JOIN intents   i2 ON i2.id = o2.intent_id
)
SELECT *
FROM journal
WHERE 1=1
  AND (:since IS NULL OR date(exit_filled_at) >= date(:since))
ORDER BY date(exit_filled_at) DESC, closing_id DESC
LIMIT COALESCE(:limit, 1000000);
"""

@dataclass(frozen=True)
class JournalRow:
    closing_id: int
    symbol: str
    qty_closed: float
    entry_filled_at: str | None
    entry_price: float | None
    exit_filled_at: str | None
    exit_price: float | None
    realized_pnl: float | None
    realized_ret: float | None
    holding_days: int | None
    entry_signal_key: str | None
    exit_signal_key: str | None


def fetch_trade_journal(conn: sqlite3.Connection, *, since: str | None, limit: int | None) -> list[JournalRow]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(JOURNAL_SQL, {"since": since, "limit": limit}).fetchall()
    out: list[JournalRow] = []
    for r in rows:
        out.append(JournalRow(
            closing_id=r["closing_id"],
            symbol=r["symbol"],
            qty_closed=r["qty_closed"],
            entry_filled_at=r["entry_filled_at"],
            entry_price=r["entry_price"],
            exit_filled_at=r["exit_filled_at"],
            exit_price=r["exit_price"],
            realized_pnl=r["realized_pnl"],
            realized_ret=r["realized_ret"],
            holding_days=r["holding_days"],
            entry_signal_key=r["entry_signal_key"],
            exit_signal_key=r["exit_signal_key"],
        ))
    return out


def _agg(rows: Iterable[JournalRow], key_fn):
    # returns dict[key] -> metrics dict
    buckets: dict[str, list[JournalRow]] = {}
    for r in rows:
        k = key_fn(r) or "UNKNOWN"
        buckets.setdefault(k, []).append(r)

    def safe(x):  # ignore None
        return [v for v in x if v is not None]

    out: dict[str, dict[str, Any]] = {}
    for k, rs in buckets.items():
        pnls = safe([r.realized_pnl for r in rs])
        rets = safe([r.realized_ret for r in rs])
        holds = safe([r.holding_days for r in rs])

        wins = sum(1 for r in rs if (r.realized_pnl or 0.0) > 0)
        n = len(rs)

        out[k] = {
            "trades": n,
            "wins": wins,
            "win_rate": (wins / n) if n else None,
            "total_pnl": sum(pnls) if pnls else 0.0,
            "avg_pnl": (sum(pnls) / len(pnls)) if pnls else None,
            "avg_ret": (sum(rets) / len(rets)) if rets else None,
            "avg_hold_days": (sum(holds) / len(holds)) if holds else None,
        }

    # sort by total_pnl desc, then trades desc
    ordered = dict(sorted(out.items(), key=lambda kv: (kv[1]["total_pnl"], kv[1]["trades"]), reverse=True))
    return ordered


def attrib_by_entry(rows: list[JournalRow]):
    return _agg(rows, lambda r: r.entry_signal_key)

def attrib_by_exit(rows: list[JournalRow]):
    return _agg(rows, lambda r: r.exit_signal_key)

def attrib_by_combo(rows: list[JournalRow]):
    return _agg(rows, lambda r: f"{(r.entry_signal_key or 'UNKNOWN')} × {(r.exit_signal_key or 'UNKNOWN')}")
