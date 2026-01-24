from __future__ import annotations

import json
from typing import Any

from ..db import connect
from ..asof import resolve_asof_date
from .mark import get_close
from .lots import apply_new_executions


def _safe_float(x: Any) -> float:
    try:
        return 0.0 if x is None else float(x)
    except Exception:
        return 0.0


def _fetch_lots_qty(conn) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT symbol, SUM(qty_open) AS qty
        FROM position_lots
        GROUP BY symbol;
        """
    ).fetchall()
    return {str(r["symbol"]).upper(): _safe_float(r["qty"]) for r in rows}


def _fetch_pos_qty(conn) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT symbol, qty
        FROM positions;
        """
    ).fetchall()
    return {str(r["symbol"]).upper(): _safe_float(r["qty"]) for r in rows}


def lots_reconcile(asof: str | None = None, *, tolerance: float = 0.001, dry_run: bool = True) -> dict:
    """
    Reconcile open lots to broker-synced positions.

    If lots_qty > pos_qty by > tolerance:
      - create a synthetic SELL execution for the difference at asof close
      - apply lots engine to close FIFO lots

    If lots_qty < pos_qty by > tolerance:
      - warn only (missing history, cannot create lots without assumptions)

    Returns summary dict.
    """
    with connect() as conn:
        asof_resolved = resolve_asof_date(conn, asof)
        lots_qty = _fetch_lots_qty(conn)
        pos_qty = _fetch_pos_qty(conn)

    syms = sorted(set(lots_qty.keys()) | set(pos_qty.keys()))

    to_reduce = []
    missing = []

    for s in syms:
        lq = lots_qty.get(s, 0.0)
        pq = pos_qty.get(s, 0.0)
        diff = lq - pq
        if diff > tolerance:
            to_reduce.append((s, lq, pq, diff))
        elif diff < -tolerance:
            missing.append((s, lq, pq, -diff))

    out = {
        "asof": asof_resolved,
        "tolerance": tolerance,
        "dry_run": dry_run,
        "reduce_count": len(to_reduce),
        "missing_count": len(missing),
        "reductions": [],
        "missing": [],
    }

    for s, lq, pq, diff in to_reduce:
        px = get_close(s, asof_resolved)
        if px is None:
            out["reductions"].append({"symbol": s, "lots_qty": lq, "pos_qty": pq, "reduce": diff, "skipped": True, "reason": "no_close_price"})
            continue

        out["reductions"].append({"symbol": s, "lots_qty": lq, "pos_qty": pq, "reduce": diff, "price": float(px)})

        if dry_run:
            continue

        # Insert synthetic SELL execution, then apply lot engine
        broker = "internal"
        broker_order_id = f"RECONCILE:{asof_resolved}:{s}"
        filled_at = f"{asof_resolved} 16:00:00"

        raw = json.dumps(
            {
                "type": "lots_reconcile",
                "asof": asof_resolved,
                "symbol": s,
                "reduce_qty": diff,
                "note": "Synthetic SELL execution to reconcile lots to broker positions",
            },
            separators=(",", ":"),
        )

        with connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO executions(broker, broker_order_id, symbol, side, qty, filled_avg_price, filled_at, raw_json)
                VALUES (?, ?, ?, 'sell', ?, ?, ?, ?);
                """,
                (broker, broker_order_id, s, float(diff), float(px), filled_at, raw),
            )

        apply_new_executions()

    for s, lq, pq, diff in missing:
        out["missing"].append({"symbol": s, "lots_qty": lq, "pos_qty": pq, "missing_lots_qty": diff})

    return out
