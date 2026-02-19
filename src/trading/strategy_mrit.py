from __future__ import annotations

from typing import Optional
from dataclasses import dataclass

from .db import connect
from .strategy_sma import Signal  # reuse existing Signal dataclass:contentReference[oaicite:2]{index=2}

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

def _rsi_wilder(closes: list[float], period: int) -> list[Optional[float]]:
    """
    Wilder's RSI. Returns list aligned to closes (None until available).
    Works fine for RSI(2).
    """
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if period <= 0 or n < period + 1:
        return out

    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        ch = closes[i] - closes[i - 1]
        gains[i] = ch if ch > 0 else 0.0
        losses[i] = (-ch) if ch < 0 else 0.0

    # initial averages over first `period`
    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period

    def _rsi_from(avg_g: float, avg_l: float) -> float:
        if avg_l <= 0:
            return 100.0
        rs = avg_g / avg_l
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = _rsi_from(avg_gain, avg_loss)

    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = _rsi_from(avg_gain, avg_loss)

    return out

def generate_signals_mrit(
    *,
    universe: str = "sp500",
    asof: str | None = None,
    trend_sma: int = 50,
    mean_sma: int = 10,
    rsi_period: int = 2,
    rsi_max: float = 10.0,
    lookback_min: int = 120,
) -> list[Signal]:
    """
    Mean Reversion Inside Trend (daily bars).
      Trend: close > SMA(trend_sma)
      Trigger: RSI(rsi_period) <= rsi_max AND close < SMA(mean_sma)
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
        for sym in syms:
            rows = conn.execute(
                """
                SELECT t, c
                FROM bars_daily
                WHERE symbol=? AND t<=?
                ORDER BY t ASC;
                """,
                (sym, asof),
            ).fetchall()

            closes = [float(r["c"]) for r in rows if r["c"] is not None]
            if len(closes) < max(trend_sma + 2, mean_sma + 2, lookback_min):
                signals.append(Signal(sym, "hold", "Not enough data"))
                continue

            sma_trend = _sma(closes, trend_sma)
            sma_mean = _sma(closes, mean_sma)
            rsi = _rsi_wilder(closes, rsi_period)

            i = len(closes) - 1
            if sma_trend[i] is None or sma_mean[i] is None or rsi[i] is None:
                signals.append(Signal(sym, "hold", "Indicators not available"))
                continue

            c = closes[i]
            uptrend = c > float(sma_trend[i])
            oversold = float(rsi[i]) <= float(rsi_max)
            below_mean = c < float(sma_mean[i])

            if uptrend and oversold and below_mean:
                strength = (float(rsi_max) - float(rsi[i])) + 50.0 * ((float(sma_mean[i]) - c) / c)
                signals.append(
                    Signal(
                        sym,
                        "buy",
                        f"MRIT: RSI{rsi_period}<={rsi_max} pullback in uptrend (c>SMA{trend_sma})",
                        strength=max(0.0, float(strength)),
                    )
                )
            else:
                signals.append(Signal(sym, "hold", "No MRIT setup"))

    return signals
