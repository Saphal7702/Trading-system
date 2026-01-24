from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..db import connect


def _safe_float(x: Any) -> float:
    try:
        return 0.0 if x is None else float(x)
    except Exception:
        return 0.0


def _event_ts_expr() -> str:
    """
    Use closed_at if present; otherwise fall back to created_at.
    """
    return "COALESCE(closed_at, created_at)"


@dataclass
class RealizedSummary:
    ok: bool
    reason: str | None = None
    since: str | None = None
    rows: int = 0
    pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    max_win: float = 0.0
    max_loss: float = 0.0


def _where_clause(since: Optional[str], symbol: Optional[str]) -> tuple[str, list[Any]]:
    parts = []
    params: list[Any] = []
    if since:
        parts.append(f"{_event_ts_expr()} >= ?")
        params.append(since)
    if symbol:
        parts.append("symbol = ?")
        params.append(symbol.upper())
    if parts:
        return "WHERE " + " AND ".join(parts), params
    return "", params


def realized_summary(*, since: Optional[str] = None, symbol: Optional[str] = None) -> RealizedSummary:
    where, params = _where_clause(since, symbol)

    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT
              COUNT(*) AS n,
              COALESCE(SUM(realized_pnl), 0.0) AS pnl,
              COALESCE(SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
              COALESCE(SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END), 0) AS losses,
              COALESCE(AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END), 0.0) AS avg_win,
              COALESCE(AVG(CASE WHEN realized_pnl < 0 THEN realized_pnl END), 0.0) AS avg_loss,
              COALESCE(MAX(CASE WHEN realized_pnl > 0 THEN realized_pnl END), 0.0) AS max_win,
              COALESCE(MIN(CASE WHEN realized_pnl < 0 THEN realized_pnl END), 0.0) AS max_loss
            FROM lot_closings
            {where};
            """,
            tuple(params),
        ).fetchone()

    n = int(row["n"]) if row else 0
    if n == 0:
        return RealizedSummary(ok=False, reason="no_realized_trades", since=since, rows=0)

    wins = int(row["wins"])
    losses = int(row["losses"])
    win_rate = (wins / (wins + losses)) if (wins + losses) > 0 else 0.0

    return RealizedSummary(
        ok=True,
        since=since,
        rows=n,
        pnl=_safe_float(row["pnl"]),
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        avg_win=_safe_float(row["avg_win"]),
        avg_loss=_safe_float(row["avg_loss"]),
        max_win=_safe_float(row["max_win"]),
        max_loss=_safe_float(row["max_loss"]),
    )


def realized_by_day(*, since: Optional[str] = None, last: Optional[int] = None, symbol: Optional[str] = None) -> list[dict]:
    where, params = _where_clause(since, symbol)

    q = f"""
    SELECT
      DATE({_event_ts_expr()}) AS day,
      COUNT(*) AS trades,
      COALESCE(SUM(realized_pnl), 0.0) AS pnl,
      COALESCE(SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
      COALESCE(SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END), 0) AS losses
    FROM lot_closings
    {where}
    GROUP BY DATE({_event_ts_expr()})
    ORDER BY day ASC;
    """

    with connect() as conn:
        rows = conn.execute(q, tuple(params)).fetchall()

    out = []
    for r in rows:
        wins = int(r["wins"])
        losses = int(r["losses"])
        wr = (wins / (wins + losses)) if (wins + losses) > 0 else 0.0
        out.append(
            {
                "day": r["day"],
                "trades": int(r["trades"]),
                "pnl": _safe_float(r["pnl"]),
                "win_rate": wr,
                "wins": wins,
                "losses": losses,
            }
        )

    if last is not None and last > 0 and len(out) > last:
        out = out[-last:]
    return out


def realized_by_month(*, since: Optional[str] = None, symbol: Optional[str] = None) -> list[dict]:
    where, params = _where_clause(since, symbol)

    q = f"""
    SELECT
      STRFTIME('%Y-%m', {_event_ts_expr()}) AS month,
      COUNT(*) AS trades,
      COALESCE(SUM(realized_pnl), 0.0) AS pnl,
      COALESCE(SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
      COALESCE(SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END), 0) AS losses
    FROM lot_closings
    {where}
    GROUP BY STRFTIME('%Y-%m', {_event_ts_expr()})
    ORDER BY month ASC;
    """

    with connect() as conn:
        rows = conn.execute(q, tuple(params)).fetchall()

    out = []
    for r in rows:
        wins = int(r["wins"])
        losses = int(r["losses"])
        wr = (wins / (wins + losses)) if (wins + losses) > 0 else 0.0
        out.append(
            {
                "month": r["month"],
                "trades": int(r["trades"]),
                "pnl": _safe_float(r["pnl"]),
                "win_rate": wr,
                "wins": wins,
                "losses": losses,
            }
        )
    return out
