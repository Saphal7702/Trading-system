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
    if not s:
        return "unknown"
    return s

def sync_orders(limit: int = 200) -> SyncResult:
    """
    Pull recent Alpaca orders and reconcile into local 'orders' table by broker_order_id.

    Local mapping:
      alpaca.status -> orders.status
      - filled -> filled
      - partially_filled -> partially_filled
      - canceled -> canceled
      - rejected -> rejected
      - new/accepted/submitted/pending_* -> submitted (or keep existing)
    """
    b = AlpacaPaperBroker()
    broker_orders = b.list_recent_orders(status="all", limit=limit)

    scanned = 0
    matched = 0
    updated = 0

    with connect() as conn:
        for bo in broker_orders:
            scanned += 1

            broker_order_id = str(getattr(bo, "id", "") or "").strip()
            if not broker_order_id:
                continue

            row = conn.execute(
                "SELECT id, status FROM orders WHERE broker_order_id=?;",
                (broker_order_id,),
            ).fetchone()

            if not row:
                continue

            matched += 1

            local_id = row["id"]
            prev_status = (row["status"] or "").lower().strip()

            alp_status = _norm_status(getattr(bo, "status", None))

            # Map Alpaca → local
            if alp_status in ("filled", "partially_filled", "canceled", "rejected", "expired"):
                new_status = alp_status
            elif alp_status in ("new", "accepted", "pending_new", "pending_replace", "pending_cancel", "submitted"):
                # keep local as submitted if already there
                new_status = "submitted" if prev_status in ("submitted", "submitting", "created") else prev_status
            else:
                new_status = prev_status or "submitted"

            # Optional extras (if you add columns later)
            filled_qty = getattr(bo, "filled_qty", None)
            filled_avg_price = getattr(bo, "filled_avg_price", None)

            if new_status != prev_status:
                conn.execute("UPDATE orders SET status=? WHERE id=?;", (new_status, local_id))
                updated += 1

            # Best-effort: save fills into orders if columns exist (safe)
            try:
                if filled_qty is not None:
                    conn.execute("UPDATE orders SET filled_qty=? WHERE id=?;", (str(filled_qty), local_id))
            except Exception:
                pass

            try:
                if filled_avg_price is not None:
                    conn.execute("UPDATE orders SET filled_avg_price=? WHERE id=?;", (str(filled_avg_price), local_id))
            except Exception:
                pass

    return SyncResult(scanned=scanned, matched=matched, updated=updated)
