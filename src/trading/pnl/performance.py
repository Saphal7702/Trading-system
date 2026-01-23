from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

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


def compute_performance() -> PerfResult:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT asof_date, equity
            FROM account_snapshots_daily
            ORDER BY asof_date ASC;
            """
        ).fetchall()

    if len(rows) < 2:
        if len(rows) == 0:
            return PerfResult(ok=False, reason="no_snapshots")
        return PerfResult(ok=False, reason="need_at_least_2_snapshots")

    dates = [str(r["asof_date"]) for r in rows]
    equities = [_safe_float(r["equity"]) for r in rows]

    if any(e is None for e in equities):
        return PerfResult(ok=False, reason="snapshot_missing_equity")

    eq = [float(e) for e in equities]
    start_eq = eq[0]
    end_eq = eq[-1]

    cum = (end_eq / start_eq) - 1.0 if start_eq > 0 else None

    # daily returns
    rets: list[tuple[str, float]] = []
    for i in range(1, len(eq)):
        prev = eq[i - 1]
        cur = eq[i]
        r = (cur / prev) - 1.0 if prev > 0 else 0.0
        rets.append((dates[i], r))

    # avg daily return
    avg = sum(r for _, r in rets) / len(rets) if rets else 0.0

    # max drawdown
    peak = -math.inf
    max_dd = 0.0  # negative
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
