from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Optional

from .db import connect

log = logging.getLogger(__name__)

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
    asof: str | None = None,
    market_sma: int = 50,
) -> list[Signal]:
    """
    SMA crossover strategy evaluated on a specific trading day.
    """
    from .asof import resolve_asof_date

    with connect() as conn:
        asof = resolve_asof_date(conn, asof)

        rows = conn.execute(
            """
            SELECT symbol
            FROM universe_daily
            WHERE asof_date=? AND universe=? AND include=1
            ORDER BY score DESC;
            """,
            (asof, universe),
        ).fetchall()

    syms = [r["symbol"] for r in rows]

    signals: list[Signal] = []
    with connect() as conn:
        spy_rows = conn.execute(
            "SELECT c FROM bars_daily WHERE symbol='SPY' AND t <= ? AND c IS NOT NULL ORDER BY t ASC",
            (asof,),
        ).fetchall()
        spy_closes = [float(r["c"]) for r in spy_rows]
        market_bullish = False
        if len(spy_closes) >= market_sma + 1:
            spy_sma_vals = _sma(spy_closes, market_sma)
            si = len(spy_closes) - 1
            if spy_sma_vals[si] is not None and spy_closes[si] > float(spy_sma_vals[si]):
                market_bullish = True
        if not market_bullish:
            log.info("SPY regime gate: SPY below SMA%d, suppressing new buy signals", market_sma)

        for sym in syms:
            rows = conn.execute(
                """
                SELECT t, c
                FROM bars_daily
                WHERE symbol=?
                ORDER BY t ASC;
                """,
                (sym,),
            ).fetchall()

            closes = [float(r["c"]) for r in rows if r["c"] is not None]
            if len(closes) < max(slow + 2, lookback_min):
                signals.append(Signal(sym, "hold", "Not enough data"))
                continue

            sma_fast = _sma(closes, fast)
            sma_slow = _sma(closes, slow)

            i = len(closes) - 1
            prev = i - 1

            if None in (sma_fast[i], sma_slow[i], sma_fast[prev], sma_slow[prev]):
                signals.append(Signal(sym, "hold", "SMA not available"))
                continue

            if sma_fast[prev] <= sma_slow[prev] and sma_fast[i] > sma_slow[i]:
                if market_bullish:
                    signals.append(Signal(sym, "buy", f"SMA{fast} crossed above SMA{slow}", abs(sma_fast[i] - sma_slow[i])))
            elif sma_fast[prev] >= sma_slow[prev] and sma_fast[i] < sma_slow[i]:
                signals.append(Signal(sym, "sell", f"SMA{fast} crossed below SMA{slow}", abs(sma_fast[i] - sma_slow[i])))
            else:
                signals.append(Signal(sym, "hold", "No crossover"))

    return signals