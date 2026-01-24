from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..db import connect
from ..asof import resolve_asof_date
from .mark import get_close


def _sf(x: Any) -> float | None:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _days_between_sql(start_expr: str, end_expr: str) -> str:
    # integer day difference; safe even if time is missing
    return f"CAST((JULIANDAY({end_expr}) - JULIANDAY({start_expr})) AS INT)"


def fetch_open_lots(*, asof: str, symbol: Optional[str] = None) -> list[dict]:
    """
    Returns one row per open lot.
    Tries to infer entry timestamp from:
      - position_lots.opened_at if present
      - else executions.filled_at via entry_execution_id
      - else position_lots.created_at
    """
    sym_where = ""
    params: list[Any] = []
    if symbol:
        sym_where = "AND pl.symbol = ?"
        params.append(symbol.upper())

    with connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(position_lots);").fetchall()}
        has_opened_at = "opened_at" in cols
        has_created_at = "created_at" in cols

        entry_ts = "ex.filled_at"
        if has_opened_at:
            entry_ts = "COALESCE(pl.opened_at, ex.filled_at)"
        elif has_created_at:
            entry_ts = "COALESCE(ex.filled_at, pl.created_at)"
        else:
            entry_ts = "ex.filled_at"

        rows = conn.execute(
            f"""
            SELECT
              pl.id AS lot_id,
              pl.symbol,
              pl.qty_open,
              pl.entry_price,
              {entry_ts} AS entry_ts,
              {_days_between_sql(entry_ts, "DATE(?)")} AS age_days,
              pl.entry_execution_id
            FROM position_lots pl
            LEFT JOIN executions ex ON ex.id = pl.entry_execution_id
            WHERE pl.qty_open > 0
              {sym_where}
            ORDER BY pl.symbol ASC, entry_ts ASC;
            """,
            tuple([*params, asof]),
        ).fetchall()

    return [dict(r) for r in rows]


def fetch_closed_trades(*, since: Optional[str] = None, last: Optional[int] = None, symbol: Optional[str] = None) -> list[dict]:
    """
    Returns one row per lot closing (partial closes show as separate rows).
    Holding days inferred from:
      - position_lots.opened_at if present
      - else entry execution filled_at
      - close time from lot_closings.closed_at if present else close execution filled_at else created_at
    """
    where = []
    params: list[Any] = []
    if since:
        where.append("COALESCE(lc.closed_at, lc.created_at) >= ?")
        params.append(since)
    if symbol:
        where.append("lc.symbol = ?")
        params.append(symbol.upper())
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with connect() as conn:
        pl_cols = {r["name"] for r in conn.execute("PRAGMA table_info(position_lots);").fetchall()}
        lc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(lot_closings);").fetchall()}

        entry_ts = "ex_open.filled_at"
        if "opened_at" in pl_cols:
            entry_ts = "COALESCE(pl.opened_at, ex_open.filled_at)"
        elif "created_at" in pl_cols:
            entry_ts = "COALESCE(ex_open.filled_at, pl.created_at)"

        close_ts = "ex_close.filled_at"
        if "closed_at" in lc_cols:
            close_ts = "COALESCE(lc.closed_at, ex_close.filled_at, lc.created_at)"
        else:
            close_ts = "COALESCE(ex_close.filled_at, lc.created_at)"

        rows = conn.execute(
            f"""
            SELECT
              lc.id AS closing_id,
              lc.symbol,
              lc.qty_closed,
              lc.entry_price,
              lc.exit_price,
              lc.realized_pnl,

              {entry_ts} AS entry_ts,
              {close_ts} AS close_ts,
              {_days_between_sql(entry_ts, close_ts)} AS hold_days,

              lc.entry_execution_id,
              lc.exit_execution_id
            FROM lot_closings lc
            LEFT JOIN position_lots pl ON pl.id = lc.lot_id
            LEFT JOIN executions ex_open  ON ex_open.id  = lc.entry_execution_id
            LEFT JOIN executions ex_close ON ex_close.id = lc.exit_execution_id
            {where_sql}
            ORDER BY close_ts ASC, lc.symbol ASC;
            """,
            tuple(params),
        ).fetchall()

    out = [dict(r) for r in rows]
    if last is not None and last > 0 and len(out) > last:
        out = out[-last:]
    return out


def compute_open_trade_lines(*, asof: str, symbol: Optional[str] = None) -> list[dict]:
    lots = fetch_open_lots(asof=asof, symbol=symbol)
    out = []
    for r in lots:
        sym = r["symbol"]
        qty = float(r["qty_open"])
        entry = float(r["entry_price"])

        cost_basis = qty * entry

        last = get_close(sym, asof)
        last_f = float(last) if last is not None else None

        mkt_value = (qty * last_f) if last_f is not None else None
        u = (mkt_value - cost_basis) if mkt_value is not None else None
        ret_pct = ((last_f / entry) - 1.0) * 100.0 if (last_f is not None and entry > 0) else None

        out.append(
            {
                "symbol": sym,
                "lot_id": r["lot_id"],
                "qty": qty,
                "entry": entry,
                "last": last_f,
                "cost_basis": cost_basis,
                "mkt_value": mkt_value,
                "u_pnl": u,
                "ret_pct": ret_pct,
                "age_days": int(r["age_days"]) if r["age_days"] is not None else None,
                "entry_ts": r.get("entry_ts"),
            }
        )
    return out

def sort_open_rows(rows: list[dict], sort: str, desc: bool) -> list[dict]:
    key = sort.lower().strip()

    def _k(r: dict):
        # Put "unknown" values at the end
        if key in ("pnl", "upnl"):
            v = r.get("u_pnl")
        elif key in ("return", "ret", "pct"):
            v = r.get("ret_pct")
        elif key == "age":
            v = r.get("age_days")
        elif key in ("mv", "value"):
            v = r.get("mkt_value")
        elif key in ("cost", "basis"):
            v = r.get("cost_basis")
        else:  # default symbol
            v = r.get("symbol")

        if v is None:
            # ensure Nones sort last
            return (1, 0)
        return (0, v)

    return sorted(rows, key=_k, reverse=desc)
