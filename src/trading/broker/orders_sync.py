# src/trading/broker/orders_sync.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..db import connect
from .alpaca_broker import AlpacaPaperBroker


@dataclass(frozen=True)
class SyncResult:
    scanned: int
    matched: int
    updated: int


def _norm_status(s: str | None) -> str:
    s = (s or "").strip().lower()
    return s or "unknown"


def _to_iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v or None
    iso = getattr(v, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except Exception:
            return None
    try:
        return str(v)
    except Exception:
        return None


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        s = str(v).strip()
        if s == "":
            return None
        return float(s)
    except Exception:
        return None


def sync_orders(limit: int = 200) -> SyncResult:
    """
    Pull recent Alpaca orders and reconcile into local 'orders' table by broker_order_id.
    Also enrich:
      - orders.filled_qty / orders.filled_avg_price / orders.filled_at (if columns exist)
      - executions.filled_avg_price / executions.filled_at + raw_json snapshot
    """
    b = AlpacaPaperBroker()
    broker_orders = b.list_recent_orders(status="all", limit=limit)

    scanned = 0
    matched = 0
    updated = 0

    terminal = {"filled", "partially_filled", "canceled", "rejected", "expired"}
    inflight = {"new", "accepted", "pending_new", "pending_replace", "pending_cancel", "submitted"}

    with connect() as conn:
        for bo in broker_orders:
            scanned += 1

            broker_order_id = str(getattr(bo, "id", "") or "").strip()
            if not broker_order_id:
                continue

            row = conn.execute(
                "SELECT id, status, symbol, side, qty FROM orders WHERE broker_order_id=?;",
                (broker_order_id,),
            ).fetchone()
            if not row:
                continue

            matched += 1
            local_id = row["id"]
            prev_status = (row["status"] or "").lower().strip()

            alp_status = _norm_status(getattr(bo, "status", None))

            # --- status mapping ---
            if alp_status in terminal:
                new_status = alp_status
            elif alp_status in inflight:
                new_status = "submitted"
            else:
                new_status = prev_status or "submitted"

            if new_status != prev_status:
                conn.execute("UPDATE orders SET status=? WHERE id=?;", (new_status, local_id))
                updated += 1

            # --- fill metadata from Alpaca ---
            filled_at = _to_iso(getattr(bo, "filled_at", None))
            filled_qty = _to_float(getattr(bo, "filled_qty", None))
            filled_avg_price = _to_float(getattr(bo, "filled_avg_price", None))

            # --- update orders fill fields (if columns exist) ---
            # (safe best-effort, doesn’t break if you haven’t migrated yet)
            try:
                if filled_at is not None:
                    conn.execute("UPDATE orders SET filled_at=? WHERE id=?;", (filled_at, local_id))
                if filled_qty is not None:
                    conn.execute("UPDATE orders SET filled_qty=? WHERE id=?;", (filled_qty, local_id))
                if filled_avg_price is not None:
                    conn.execute("UPDATE orders SET filled_avg_price=? WHERE id=?;", (filled_avg_price, local_id))
            except Exception:
                pass

            # --- ensure executions row exists (future-proof) ---
            # If you later stop inserting executions on submit, sync will still create/update it.
            try:
                sym = (row["symbol"] or "").strip().upper()
                side = (row["side"] or "").strip().lower()
                qty = float(row["qty"]) if row["qty"] is not None else 0.0

                raw_json = None
                try:
                    raw_json = bo.model_dump_json()  # pydantic v2 convenience if available
                except Exception:
                    try:
                        raw_json = str(getattr(bo, "model_dump", lambda: bo)())
                    except Exception:
                        raw_json = None

                conn.execute(
                    """
                    INSERT OR IGNORE INTO executions(broker, broker_order_id, symbol, side, qty, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    ("alpaca", broker_order_id, sym, side, qty, raw_json),
                )
            except Exception:
                pass

            # --- update executions fill fields (schema supports these) ---
            sets = []
            params: list[Any] = []

            if filled_at is not None:
                sets.append("filled_at=?")
                params.append(filled_at)
            if filled_avg_price is not None:
                sets.append("filled_avg_price=?")
                params.append(filled_avg_price)

            # Keep a fresh raw_json snapshot too (optional)
            try:
                raw_json = bo.model_dump_json()
                sets.append("raw_json=?")
                params.append(raw_json)
            except Exception:
                pass

            if sets:
                params.extend(["alpaca", broker_order_id])
                conn.execute(
                    f"""
                    UPDATE executions
                    SET {", ".join(sets)}
                    WHERE broker=? AND broker_order_id=?;
                    """,
                    tuple(params),
                )

    return SyncResult(scanned=scanned, matched=matched, updated=updated)
