from __future__ import annotations

from typing import Optional, TYPE_CHECKING
from dataclasses import dataclass
import logging
import os

from .db import connect
from .strategy_sma import Signal

if TYPE_CHECKING:
    from .regime import RegimeState

log = logging.getLogger("trading")

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

def _market_regime_gate(conn, *, asof: str) -> tuple[bool, str]:
    """
    MRIT market gate: allow buys only when market proxy is in up regime.
    Up regime default: PROXY close > PROXY SMA200.
    """
    enabled = os.getenv("TRADING_MRIT_MARKET_GATE", "1").strip().lower() not in ("0", "false", "no")
    proxy = os.getenv("TRADING_MRIT_MARKET_PROXY", "SPY").strip().upper()
    sma_n = int(float(os.getenv("TRADING_MRIT_MARKET_SMA", "200")))

    if not enabled:
        return True, "MRIT market gate disabled"

    rows = conn.execute(
        """
        SELECT t, c
        FROM bars_daily
        WHERE symbol=? AND t<=?
        ORDER BY t ASC;
        """,
        (proxy, asof),
    ).fetchall()

    closes = [float(r["c"]) for r in rows if r["c"] is not None]

    # Safer default: fail-closed (block MRIT) if we can't compute the gate reliably.
    if len(closes) < sma_n + 5:
        return False, f"MRIT gated OFF: insufficient {proxy} bars for SMA{sma_n} (have {len(closes)})"

    sma = _sma(closes, sma_n)
    i = len(closes) - 1
    if sma[i] is None:
        return False, f"MRIT gated OFF: {proxy} SMA{sma_n} unavailable"

    c = closes[i]
    s = float(sma[i])

    if c > s:
        return True, f"MRIT gate OK: {proxy} c({c:.2f}) > SMA{sma_n}({s:.2f})"
    return False, f"MRIT gated OFF: {proxy} c({c:.2f}) <= SMA{sma_n}({s:.2f})"

def generate_signals_mrit(
    *,
    universe: str = "sp500",
    asof: str | None = None,
    trend_sma: int = 50,
    mean_sma: int = 10,
    rsi_period: int = 2,
    rsi_max: float = 10.0,
    lookback_min: int = 120,
    regime: "RegimeState | None" = None,
) -> list[Signal]:
    """
    Mean Reversion Inside Trend (daily bars).
      Trend: close > SMA(trend_sma)
      Trigger: RSI(rsi_period) <= rsi_max AND close < SMA(mean_sma)

    When regime is passed, buy suppression is decided from RegimeState facts.
    When regime is None, the internal _market_regime_gate() runs as fallback.
    """
    from .asof import resolve_asof_date

    proxy = os.getenv("TRADING_MRIT_MARKET_PROXY", "SPY").strip().upper()

    with connect() as conn:
        asof = resolve_asof_date(conn, asof)

        # Determine gate from RegimeState when provided; fall back to internal check
        if regime is not None:
            if not regime.trending_up:
                log.info("MRIT suppressed: market not trending up (regime=%s)", regime.regime)
                gate_ok, gate_note = False, f"MRIT suppressed: market not trending up (regime={regime.regime})"
            elif regime.is_crash:
                log.info("MRIT suppressed: crash regime")
                gate_ok, gate_note = False, "MRIT suppressed: crash regime"
            else:
                gate_ok, gate_note = True, "MRIT regime gate OK"
        else:
            gate_ok, gate_note = _market_regime_gate(conn, asof=asof)
            log.info("MRIT market gate | %s", gate_note)

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

        from .exclusions import get_active_exclusions
        excluded = get_active_exclusions("mrit")
        if excluded:
            before = len(syms)
            syms = [s for s in syms if s not in excluded]
            log.info("MRIT exclusions filter: %d -> %d symbols", before, len(syms))

        signals: list[Signal] = []

        # If gated off, don't waste time computing indicators for every symbol
        if not gate_ok:
            for sym in syms:
                if sym == proxy:
                    signals.append(Signal(sym, "hold", "Skip market proxy symbol"))
                else:
                    signals.append(Signal(sym, "hold", gate_note))
            return signals

        # Gate is ON: do the normal per-symbol work
        for sym in syms:
            if sym == proxy:
                signals.append(Signal(sym, "hold", "Skip market proxy symbol"))
                continue

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
