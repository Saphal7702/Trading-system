from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

from .db import connect

@dataclass(frozen=True)
class Signal:
    symbol: str
    action: str          # 'buy' | 'sell' | 'hold'
    reason: str
    strength: float | None = None

def _sma(values: list[float], window: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if window <= 0:
        return out
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= window:
            s -= values[i - window]
        if i >= window - 1:
            out[i] = s / window
    return out

def generate_signals_sma(
    fast: int = 20,
    slow: int = 50,
    lookback_min: int = 120,
    universe: str = "sp500",
) -> list[Signal]:
    """
    SMA crossover:
      - BUY when fast crosses above slow (today above, yesterday below/equal)
      - SELL when fast crosses below slow
      - otherwise HOLD
    """
    # 1) Get selected symbols from latest universe snapshot
    with connect() as conn:
        r = conn.execute(
            "SELECT MAX(asof_date) AS d FROM universe_daily WHERE universe=?;",
            (universe,),
        ).fetchone()
        asof = r["d"] if r and r["d"] else None

        if not asof:
            raise RuntimeError("No universe_daily snapshot found. Run build-universe first.")

        rows = conn.execute(
            """
            SELECT symbol
            FROM universe_daily
            WHERE asof_date=? AND universe=? AND include=1
            ORDER BY score DESC;
            """,
            (asof, universe),
        ).fetchall()

    syms = [row["symbol"] for row in rows] 

    signals: list[Signal] = []
    with connect() as conn:
        for sym in syms:
            rows = conn.execute(
                """
                SELECT t, c
                FROM bars_daily
                WHERE symbol = ?
                ORDER BY t ASC;
                """,
                (sym,),
            ).fetchall()

            closes = [float(r["c"]) for r in rows if r["c"] is not None]
            if len(closes) < max(slow + 2, lookback_min):
                signals.append(Signal(sym, "hold", f"Not enough data ({len(closes)} closes)"))
                continue

            sma_fast = _sma(closes, fast)
            sma_slow = _sma(closes, slow)

            i = len(closes) - 1
            prev = i - 1

            f_now, s_now = sma_fast[i], sma_slow[i]
            f_prev, s_prev = sma_fast[prev], sma_slow[prev]

            if f_now is None or s_now is None or f_prev is None or s_prev is None:
                signals.append(Signal(sym, "hold", "SMA not available yet"))
                continue

            if f_prev <= s_prev and f_now > s_now:
                signals.append(Signal(sym, "buy", f"SMA{fast} crossed above SMA{slow}", strength=abs(f_now - s_now)))
            elif f_prev >= s_prev and f_now < s_now:
                signals.append(Signal(sym, "sell", f"SMA{fast} crossed below SMA{slow}", strength=abs(f_now - s_now)))
            else:
                signals.append(Signal(sym, "hold", "No crossover"))

    return signals
