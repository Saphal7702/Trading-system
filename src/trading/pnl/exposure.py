from __future__ import annotations

from typing import Any

from ..db import connect
from .lots import get_open_lots_by_symbol
from .mark import get_close


def _safe_float(x: Any) -> float | None:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def get_equity_for_asof(asof: str) -> float | None:
    """
    Get equity snapshot for this asof_date (preferred).
    Falls back to latest snapshot if exact date not found.
    """
    with connect() as conn:
        row = conn.execute(
            """
            SELECT equity
            FROM account_snapshots_daily
            WHERE asof_date = ?
            LIMIT 1;
            """,
            (asof,),
        ).fetchone()

        if row and row["equity"] is not None:
            return float(row["equity"])

        row2 = conn.execute(
            """
            SELECT equity
            FROM account_snapshots_daily
            ORDER BY asof_date DESC
            LIMIT 1;
            """
        ).fetchone()

    if row2 and row2["equity"] is not None:
        return float(row2["equity"])
    return None


def compute_exposure(asof: str) -> dict:
    """
    Exposure by symbol based on open lots and asof close.
    Returns:
      {
        "asof": "...",
        "equity": float|None,
        "rows": [ {symbol, qty, last, mkt_value, avg_cost, cost_basis, u_pnl, pct_equity}, ... ],
        "totals": {market_value, cost_basis, u_pnl}
      }
    """
    lots_by_sym = get_open_lots_by_symbol()
    equity = get_equity_for_asof(asof)

    rows = []
    tot_mv = 0.0
    tot_cost = 0.0
    tot_upnl = 0.0

    for sym in sorted(lots_by_sym.keys()):
        lots = lots_by_sym[sym]

        qty = 0.0
        cost_basis = 0.0
        for L in lots:
            q = float(L["qty_open"])
            p = float(L["entry_price"])
            qty += q
            cost_basis += q * p

        if qty <= 0:
            continue

        avg_cost = cost_basis / qty if qty > 0 else 0.0
        last = get_close(sym, asof)
        if last is None:
            # No price -> cannot value exposure; still show qty/cost
            rows.append(
                {
                    "symbol": sym,
                    "qty": qty,
                    "last": None,
                    "mkt_value": None,
                    "avg_cost": avg_cost,
                    "cost_basis": cost_basis,
                    "u_pnl": None,
                    "pct_equity": None,
                    "lots": len(lots),
                }
            )
            continue

        last_f = float(last)
        mkt_value = qty * last_f
        u_pnl = mkt_value - cost_basis
        pct_equity = (mkt_value / equity) if (equity is not None and equity > 0) else None

        tot_mv += mkt_value
        tot_cost += cost_basis
        tot_upnl += u_pnl

        rows.append(
            {
                "symbol": sym,
                "qty": qty,
                "last": last_f,
                "mkt_value": mkt_value,
                "avg_cost": avg_cost,
                "cost_basis": cost_basis,
                "u_pnl": u_pnl,
                "pct_equity": pct_equity,
                "lots": len(lots),
            }
        )

    # Sort by market value descending where possible
    rows.sort(key=lambda r: (r["mkt_value"] is not None, r["mkt_value"] or 0.0), reverse=True)

    return {
        "asof": asof,
        "equity": equity,
        "rows": rows,
        "totals": {"market_value": tot_mv, "cost_basis": tot_cost, "u_pnl": tot_upnl},
    }
