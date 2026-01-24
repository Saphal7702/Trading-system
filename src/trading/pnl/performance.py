from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

from ..db import connect


@dataclass
class PerfResult:
    ok: bool
    reason: str | None = None

    start_date: str | None = None
    end_date: str | None = None
    days: int = 0

    start_equity: float | None = None
    end_equity: float | None = None

    cumulative_return: float | None = None
    max_drawdown: float | None = None

    avg_daily_return: float | None = None
    best_day: tuple[str, float] | None = None   # (date, return)
    worst_day: tuple[str, float] | None = None  # (date, return)


def _safe_float(x: Any) -> float | None:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def fetch_equity_series(*, since: Optional[str] = None, last: Optional[int] = None) -> list[dict]:
    """
    Returns snapshots ordered ASC by date.
    since: include rows where asof_date >= since
    last: take only last N rows (still returned in ASC order)
    """
    params: list[Any] = []
    where = ""
    if since:
        where = "WHERE asof_date >= ?"
        params.append(since)

    q = f"""
    SELECT asof_date, equity
    FROM account_snapshots_daily
    {where}
    ORDER BY asof_date ASC;
    """

    with connect() as conn:
        rows = conn.execute(q, tuple(params)).fetchall()

    series = [dict(r) for r in rows]

    if last is not None and last > 0 and len(series) > last:
        series = series[-last:]  # keep last N
    return series


def compute_performance(*, since: Optional[str] = None, last: Optional[int] = None) -> PerfResult:
    series = fetch_equity_series(since=since, last=last)

    if len(series) < 2:
        if len(series) == 0:
            return PerfResult(ok=False, reason="no_snapshots")
        return PerfResult(ok=False, reason="need_at_least_2_snapshots")

    dates = [str(r["asof_date"]) for r in series]
    equities = [_safe_float(r["equity"]) for r in series]
    if any(e is None for e in equities):
        return PerfResult(ok=False, reason="snapshot_missing_equity")

    eq = [float(e) for e in equities]
    start_eq = eq[0]
    end_eq = eq[-1]
    cum = (end_eq / start_eq) - 1.0 if start_eq > 0 else None

    rets: list[tuple[str, float]] = []
    for i in range(1, len(eq)):
        prev = eq[i - 1]
        cur = eq[i]
        r = (cur / prev) - 1.0 if prev > 0 else 0.0
        rets.append((dates[i], r))

    avg = sum(r for _, r in rets) / len(rets) if rets else 0.0

    peak = -math.inf
    max_dd = 0.0
    for v in eq:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (v - peak) / peak
            if dd < max_dd:
                max_dd = dd

    best = max(rets, key=lambda x: x[1]) if rets else None
    worst = min(rets, key=lambda x: x[1]) if rets else None

    return PerfResult(
        ok=True,
        start_date=dates[0],
        end_date=dates[-1],
        days=len(eq),
        start_equity=start_eq,
        end_equity=end_eq,
        cumulative_return=cum,
        max_drawdown=max_dd,
        avg_daily_return=avg,
        best_day=best,
        worst_day=worst,
    )


def compute_monthly_performance(*, since: Optional[str] = None) -> list[dict]:
    """
    Monthly performance computed from equity snapshots.

    For each month:
      - start_equity = equity on first snapshot date in that month
      - end_equity   = equity on last snapshot date in that month
      - pnl_dollars  = end - start
      - return       = end/start - 1
      - running_return = end / first_equity_overall - 1
    """
    series = fetch_equity_series(since=since, last=None)
    if len(series) == 0:
        return []

    # first equity overall in this series (after since-filter)
    first_equity = _safe_float(series[0].get("equity"))
    if first_equity is None or first_equity <= 0:
        first_equity = None
    else:
        first_equity = float(first_equity)

    months: dict[str, dict] = {}

    for r in series:
        d = str(r["asof_date"])
        e = _safe_float(r["equity"])
        if e is None:
            continue
        e = float(e)

        month = d[:7]  # YYYY-MM

        if month not in months:
            months[month] = {
                "month": month,
                "start_date": d,
                "end_date": d,
                "start_equity": e,
                "end_equity": e,
            }
        else:
            # series is ASC, so we update end as we go
            months[month]["end_date"] = d
            months[month]["end_equity"] = e

    out: list[dict] = []
    for m in sorted(months.keys()):
        row = months[m]
        start = float(row["start_equity"])
        end = float(row["end_equity"])

        pnl_dollars = end - start
        ret = (end / start) - 1.0 if start > 0 else 0.0

        running = None
        if first_equity is not None and first_equity > 0:
            running = (end / first_equity) - 1.0

        out.append(
            {
                "month": row["month"],
                "start_date": row["start_date"],
                "end_date": row["end_date"],
                "start_equity": start,
                "end_equity": end,
                "pnl_dollars": pnl_dollars,
                "return": ret,
                "running_return": running,
            }
        )

    return out
