from __future__ import annotations

import bisect
import json
import logging
import math
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger("trading")

from .db import connect_backtest, init_backtest_db
from .sim_broker import SimBroker, SimPosition


# ---------------------------------------------------------------------------
# Technical indicator helpers (self-contained, no external imports)
# ---------------------------------------------------------------------------

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
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if period <= 0 or n < period + 1:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        ch = closes[i] - closes[i - 1]
        gains[i] = ch if ch > 0 else 0.0
        losses[i] = -ch if ch < 0 else 0.0
    avg_g = sum(gains[1 : period + 1]) / period
    avg_l = sum(losses[1 : period + 1]) / period

    def _rsi(g: float, l: float) -> float:
        return 100.0 if l <= 0 else 100.0 - (100.0 / (1.0 + g / l))

    out[period] = _rsi(avg_g, avg_l)
    for i in range(period + 1, n):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        out[i] = _rsi(avg_g, avg_l)
    return out


# ---------------------------------------------------------------------------
# Data access helpers
# ---------------------------------------------------------------------------

def _closes_up_to(
    bars: list[tuple[str, float]],
    dates: list[str],
    asof: str,
) -> list[float]:
    """Return closes from sorted (date, close) list where date <= asof."""
    idx = bisect.bisect_right(dates, asof) - 1
    if idx < 0:
        return []
    return [b[1] for b in bars[: idx + 1]]


def _price_on_or_before(
    bars: list[tuple[str, float]],
    dates: list[str],
    asof: str,
) -> Optional[float]:
    idx = bisect.bisect_right(dates, asof) - 1
    return bars[idx][1] if idx >= 0 else None


def _ohlcv_up_to(
    ohlcv: list[tuple[str, float, float, float, float]],
    dates: list[str],
    asof: str,
) -> list[tuple[str, float, float, float, float]]:
    """Return OHLCV rows (date, o, h, l, c) where date <= asof."""
    idx = bisect.bisect_right(dates, asof) - 1
    if idx < 0:
        return []
    return ohlcv[: idx + 1]


# ---------------------------------------------------------------------------
# ATR computation (mirrors _fetch_atr in exits_advisor.py)
# ---------------------------------------------------------------------------

def _compute_atr(
    ohlcv: list[tuple[str, float, float, float, float]],
    period: int = 14,
) -> float | None:
    """
    Compute ATR(period) from OHLCV rows: (date, open, high, low, close).
    TR = max(H-L, |H-prevC|, |L-prevC|); ATR = SMA(TR, period).
    """
    if len(ohlcv) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(ohlcv)):
        _, _, h, l, _ = ohlcv[i]
        prev_c = ohlcv[i - 1][4]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        if tr <= 0 or math.isnan(tr):
            continue
        trs.append(tr)
    if len(trs) < period:
        return None
    atr = sum(trs[-period:]) / period
    return None if (atr <= 0 or math.isnan(atr)) else atr


# ---------------------------------------------------------------------------
# Regime detection from backtest bars (mirrors detect_regime() in regime.py)
# ---------------------------------------------------------------------------

def _compute_regime(
    spy_closes: list[float],
    *,
    pullback_day_pct: float = -0.010,
    crash_day_pct: float = -0.020,
    crash_20d_dd_pct: float = -0.080,
) -> dict:
    """
    Compute observable regime facts from a list of SPY closes.
    Mirrors the logic in regime.detect_regime() so the backtest gates
    strategies the same way the paper environment does.
    """
    if len(spy_closes) < 200:
        return {
            "regime": "defensive",
            "trending_up": False,
            "momentum_positive": False,
            "is_crash": False,
            "buy_notional_mult": 0.75,
            "exposure_cap_mult": 0.75,
        }

    close = spy_closes[-1]
    prev_close = spy_closes[-2] if len(spy_closes) >= 2 else None

    sma50 = _sma(spy_closes, 50)[-1]
    sma200 = _sma(spy_closes, 200)[-1]

    day_return = ((close / prev_close) - 1.0) if prev_close and prev_close != 0 else None

    trailing_20 = spy_closes[-20:]
    peak20 = max(trailing_20) if trailing_20 else None
    drawdown_20d = ((close / peak20) - 1.0) if peak20 and peak20 != 0 else None

    trending_up = sma200 is not None and close > sma200
    momentum_positive = sma50 is not None and close > sma50

    is_crash = False
    if day_return is not None and day_return <= crash_day_pct:
        is_crash = True
    if drawdown_20d is not None and drawdown_20d <= crash_20d_dd_pct:
        is_crash = True

    if is_crash:
        return {
            "regime": "crash",
            "trending_up": trending_up,
            "momentum_positive": momentum_positive,
            "is_crash": True,
            "buy_notional_mult": 0.50,
            "exposure_cap_mult": 0.50,
        }

    if sma200 is not None and close < sma200:
        return {
            "regime": "defensive",
            "trending_up": False,
            "momentum_positive": momentum_positive,
            "is_crash": False,
            "buy_notional_mult": 0.75,
            "exposure_cap_mult": 0.75,
        }

    if (sma50 is not None and close < sma50) or (
        day_return is not None and day_return <= pullback_day_pct
    ):
        return {
            "regime": "pullback",
            "trending_up": True,
            "momentum_positive": False,
            "is_crash": False,
            "buy_notional_mult": 0.85,
            "exposure_cap_mult": 0.85,
        }

    return {
        "regime": "bull",
        "trending_up": True,
        "momentum_positive": True,
        "is_crash": False,
        "buy_notional_mult": 1.0,
        "exposure_cap_mult": 1.0,
    }


# ---------------------------------------------------------------------------
# Signal generators — gated by regime_facts, not internal SPY checks
# ---------------------------------------------------------------------------

def _sma_signals(
    symbols: list[str],
    bars_by_sym: dict[str, list[tuple[str, float]]],
    dates_by_sym: dict[str, list[str]],
    asof: str,
    regime_facts: dict,
    *,
    fast: int = 20,
    slow: int = 50,
    lookback_min: int = 120,
) -> list[dict]:
    """
    SMA crossover signals. Buys suppressed in defensive/crash regime.
    Sells always fire (we never suppress exits).
    """
    market_bullish = regime_facts["regime"] not in ("defensive", "crash")

    signals: list[dict] = []
    min_bars = max(slow + 2, lookback_min)
    for sym in symbols:
        bars = bars_by_sym.get(sym)
        if not bars:
            continue
        dates = dates_by_sym[sym]
        closes = _closes_up_to(bars, dates, asof)
        if len(closes) < min_bars:
            continue
        sma_f = _sma(closes, fast)
        sma_s = _sma(closes, slow)
        i = len(closes) - 1
        p = i - 1
        if None in (sma_f[i], sma_s[i], sma_f[p], sma_s[p]):
            continue
        if sma_f[p] <= sma_s[p] and sma_f[i] > sma_s[i]:
            if market_bullish:
                signals.append({
                    "symbol": sym,
                    "action": "buy",
                    "reason": f"SMA{fast} crossed above SMA{slow}",
                    "strength": abs(float(sma_f[i]) - float(sma_s[i])),
                    "signal_key": "sma20_cross_up_sma50",
                })
        elif sma_f[p] >= sma_s[p] and sma_f[i] < sma_s[i]:
            signals.append({
                "symbol": sym,
                "action": "sell",
                "reason": f"SMA{fast} crossed below SMA{slow}",
                "strength": abs(float(sma_f[i]) - float(sma_s[i])),
            })
    return signals


def _mrit_signals(
    symbols: list[str],
    bars_by_sym: dict[str, list[tuple[str, float]]],
    dates_by_sym: dict[str, list[str]],
    asof: str,
    regime_facts: dict,
    *,
    trend_sma: int = 50,
    mean_sma: int = 10,
    rsi_period: int = 2,
    rsi_max: float = 10.0,
    lookback_min: int = 120,
) -> list[dict]:
    """
    MRIT signals. Suppressed entirely when market is not trending up or in crash.
    """
    if not regime_facts["trending_up"] or regime_facts["is_crash"]:
        return []

    signals: list[dict] = []
    min_bars = max(trend_sma + 2, mean_sma + 2, lookback_min)
    for sym in symbols:
        if sym == "SPY":
            continue
        bars = bars_by_sym.get(sym)
        if not bars:
            continue
        dates = dates_by_sym[sym]
        closes = _closes_up_to(bars, dates, asof)
        if len(closes) < min_bars:
            continue
        s_trend = _sma(closes, trend_sma)
        s_mean = _sma(closes, mean_sma)
        rsi = _rsi_wilder(closes, rsi_period)
        i = len(closes) - 1
        if s_trend[i] is None or s_mean[i] is None or rsi[i] is None:
            continue
        c = closes[i]
        if c > float(s_trend[i]) and float(rsi[i]) <= rsi_max and c < float(s_mean[i]):
            strength = (rsi_max - float(rsi[i])) + 50.0 * ((float(s_mean[i]) - c) / c)
            signals.append({
                "symbol": sym,
                "action": "buy",
                "reason": f"MRIT: RSI{rsi_period}<={rsi_max} pullback in uptrend (c>SMA{trend_sma})",
                "strength": max(0.0, float(strength)),
                "signal_key": "mrit_rsi2_pullback_uptrend",
            })
    return signals


# ---------------------------------------------------------------------------
# Portfolio momentum signal generator — mirrors strategy_portfolio.py
# Self-contained: uses pre-loaded bars only, no DB calls.
# ---------------------------------------------------------------------------

def _portfolio_signals(
    symbols: list[str],
    bars_by_sym: dict[str, list[tuple[str, float]]],
    dates_by_sym: dict[str, list[str]],
    spy_closes: list[float],    # SPY closes up to and including asof
    asof: str,
    regime_facts: dict,
    *,
    sma_long: int = 200,
    sma_mid: int = 50,
    ext_days: int = 20,
    ext_max: float = 0.20,
    return_days: int = 126,
    top_pct: float = 0.25,
    lookback_min: int = 210,
) -> list[dict]:
    """
    Portfolio momentum signals for the backtest engine.

    Mirrors strategy_portfolio.generate_signals_portfolio() exactly but
    operates on pre-loaded bars (no live DB calls) for performance.

    Entry conditions — ALL must pass:
      a) price > SMA200   (long-term trend)
      b) price > SMA50    (medium-term trend)
      c) top 25% of universe by 6-month return  (P75 percentile)
      d) RS vs SPY positive: sym_6m_return > spy_6m_return
      e) not extended: (price / price_20d_ago - 1) < 20%

    Emits buy signals only; no sell signals (exits handled by _check_exits).
    Suppressed entirely in defensive/crash regime (same gate as MRIT).
    """
    # Regime gate: suppress in defensive/crash
    if regime_facts["regime"] in ("defensive", "crash"):
        return []

    # SPY 6m return for RS check (None if insufficient history → RS check skipped)
    spy_6m_return: float | None = None
    if len(spy_closes) >= return_days + 1:
        spy_6m_return = (spy_closes[-1] / spy_closes[-(return_days + 1)] - 1) * 100.0

    # ── Pass 1: collect per-symbol metrics ───────────────────────────────────
    sym_metrics: dict[str, dict] = {}
    sym_6m_returns: dict[str, float] = {}

    for sym in symbols:
        if sym == "SPY":
            continue
        bars = bars_by_sym.get(sym)
        if not bars:
            continue
        dates = dates_by_sym[sym]
        closes = _closes_up_to(bars, dates, asof)
        if len(closes) < lookback_min or len(closes) <= return_days:
            continue

        sma200_vals = _sma(closes, sma_long)
        sma50_vals = _sma(closes, sma_mid)
        i = len(closes) - 1

        if sma200_vals[i] is None or sma50_vals[i] is None:
            continue

        price = closes[i]
        s200 = float(sma200_vals[i])
        s50 = float(sma50_vals[i])

        # 6-month return (return_days trading days)
        ret_6m = (closes[i] / closes[i - return_days] - 1) * 100.0

        # 20-day extension check (fraction, not %)
        if i < ext_days:
            continue
        ext_20d = closes[i] / closes[i - ext_days] - 1

        sym_metrics[sym] = {
            "price": price,
            "sma200": s200,
            "sma50": s50,
            "ret_6m": ret_6m,
            "ext_20d": ext_20d,
        }
        sym_6m_returns[sym] = ret_6m

    # ── Compute P75 threshold (top top_pct% cutoff) ───────────────────────────
    if sym_6m_returns:
        returns_sorted = sorted(sym_6m_returns.values())
        n = len(returns_sorted)
        p75_idx = int(n * (1.0 - top_pct))
        p75_threshold = returns_sorted[min(p75_idx, n - 1)]
    else:
        p75_threshold = float("inf")  # no candidates → no signals

    # ── Pass 2: emit signals ──────────────────────────────────────────────────
    signals: list[dict] = []
    for sym, m in sym_metrics.items():
        price = m["price"]
        s200 = m["sma200"]
        s50 = m["sma50"]
        ret_6m = m["ret_6m"]
        ext_20d = m["ext_20d"]

        # a) Long-term trend
        if price <= s200:
            continue
        # b) Medium-term trend
        if price <= s50:
            continue
        # c) Top top_pct% by 6m return
        if ret_6m < p75_threshold:
            continue
        # d) RS vs SPY positive (skipped when spy_6m_return is None)
        if spy_6m_return is not None and (ret_6m - spy_6m_return) <= 0:
            continue
        # e) Not extended
        if ext_20d >= ext_max:
            continue

        signals.append({
            "symbol": sym,
            "action": "buy",
            "reason": "PORTFOLIO: momentum + trend + RS leader, not extended",
            "strength": max(0.01, ret_6m),   # clamp positive; planner sorts DESC
            "signal_key": "portfolio_momentum_strength",
        })

    return signals


# ---------------------------------------------------------------------------
# Exit rule checks — priority chain matches exits_advisor.evaluate_exit_advice()
# ---------------------------------------------------------------------------

def _portfolio_pyramid_check(
    pos: SimPosition,
    current_price: float,
    pyramid_rungs_hit: set[int],
    ladder: list[float],
    sizes: list[float],
    max_mult: float,
) -> Optional[tuple[int, float]]:
    """
    Check if a portfolio position has crossed a new pyramid rung.

    Returns (rung_index, add_notional) if a rung is newly crossed and within the cap.
    Returns None otherwise (already hit, gain too low, cap hit, no original price).

    Gain is always measured from original_entry_price (not avg cost basis) so the
    ladder thresholds stay fixed regardless of how many adds have occurred.
    """
    if pos.original_entry_price <= 0 or pos.entry_notional_original <= 0:
        return None
    gain_pct = (current_price / pos.original_entry_price - 1.0) * 100.0
    for i, threshold in enumerate(ladder):
        if i in pyramid_rungs_hit:
            continue
        if gain_pct >= threshold - 1e-9:  # epsilon guards float rounding (e.g. 12/10*100=19.9999...)
            add_notional = pos.entry_notional_original * sizes[i]
            # Enforce total exposure cap relative to original position size
            if pos.entry_notional + add_notional > pos.entry_notional_original * max_mult:
                continue  # would breach cap; check remaining rungs
            return (i, add_notional)
    return None


def _check_exits(
    broker: SimBroker,
    prices: dict[str, float],
    holding_days: dict[str, int],
    bars_by_sym: dict[str, list[tuple[str, float]]],
    dates_by_sym: dict[str, list[str]],
    ohlcv_by_sym: dict[str, list[tuple[str, float, float, float, float]]],
    ohlcv_dates_by_sym: dict[str, list[str]],
    asof: str,
    *,
    # DEFAULT profile parameters
    stop_loss_pct: float = 5.0,
    trail_activate_pct: float = 8.0,
    trail_dd_pct: float = 4.0,
    take_profit_pct: float = 15.0,
    time_stop_days: int = 30,
    time_stop_min_ret: float = 2.0,
    early_fail_days: int = 5,
    early_fail_max_ret: float = 0.0,
    enable_sma_reversal: bool = True,
    break_even_peak_pct: float = 10.0,
    break_even_floor_pct: float = 2.0,
    # MRIT profile: ATR exits (fire before percentage rules)
    mrit_atr_period: int = 14,
    mrit_tp_atr_mult: float | None = None,
    mrit_sl_atr_mult: float | None = None,
    # MRIT profile: time/failure overrides
    mrit_early_fail_days: int = 5,
    mrit_early_fail_max_ret: float = 0.0,
    mrit_time_stop_days: int = 30,
    mrit_time_stop_min_ret: float = 2.0,
    mrit_enable_sma_reversal: bool = True,
    # PORTFOLIO profile exit params
    portfolio_stop_loss_pct: float = 15.0,
    portfolio_trail_activate_pct: float = 20.0,
    portfolio_trail_dd_pct: float = 10.0,
    portfolio_take_profit_pct: float = 99999.0,
    portfolio_time_stop_days: int = 60,
    portfolio_time_stop_min_ret: float = 0.0,
    portfolio_early_fail_days: int = 999,
    portfolio_early_fail_max_ret: float = 0.0,
    portfolio_enable_sma_reversal: bool = False,
    portfolio_break_even_peak_pct: float = 15.0,
    portfolio_break_even_floor_pct: float = 3.0,
    portfolio_stop_dollar_pct: float = 0.0,
) -> list[tuple[str, str]]:
    """
    Return (symbol, exit_reason) for positions that should be closed.
    Priority chain mirrors exits_advisor.evaluate_exit_advice():
      0) ATR stop-loss / take-profit  (MRIT profile only, if configured)
      1) Hard stop loss
      2) Trailing stop
      3) Break-even floor
      4) SMA reversal  (per-profile enable flag)
      5) Early failure stop
      6) Time stop
      7) Take profit
    """
    exits: list[tuple[str, str]] = []
    for sym, pos in broker.positions.items():
        price = prices.get(sym)
        if price is None:
            continue
        # Use original_entry_price as the fixed reference for ret and peak_gain so
        # that pyramid adds (which raise the avg cost basis) don't shift the trailing
        # stop's reference point. For non-pyramid positions original_entry_price ==
        # entry_price, so behavior is byte-identical.
        ref_price = pos.original_entry_price if pos.original_entry_price > 0 else pos.entry_price
        ret = ((price - ref_price) / ref_price) * 100.0
        peak_gain = ((pos.peak_price - ref_price) / ref_price) * 100.0
        dd = ((price - pos.peak_price) / pos.peak_price) * 100.0
        days = holding_days.get(sym, 0)

        is_mrit = pos.signal_key.startswith("mrit_")
        is_portfolio = pos.signal_key.startswith("portfolio_")

        # Resolve per-profile params once; rules 1-7 use these local vars.
        if is_portfolio:
            sl_pct     = portfolio_stop_loss_pct
            trail_peak = portfolio_trail_activate_pct
            trail_dd   = portfolio_trail_dd_pct
            tp_pct     = portfolio_take_profit_pct
            ts_days    = portfolio_time_stop_days
            ts_min     = portfolio_time_stop_min_ret
            ef_days    = portfolio_early_fail_days
            ef_max     = portfolio_early_fail_max_ret
            sma_rev    = portfolio_enable_sma_reversal
            be_peak    = portfolio_break_even_peak_pct
            be_floor   = portfolio_break_even_floor_pct
        elif is_mrit:
            sl_pct     = stop_loss_pct
            trail_peak = trail_activate_pct
            trail_dd   = trail_dd_pct
            tp_pct     = take_profit_pct
            ts_days    = mrit_time_stop_days
            ts_min     = mrit_time_stop_min_ret
            ef_days    = mrit_early_fail_days
            ef_max     = mrit_early_fail_max_ret
            sma_rev    = mrit_enable_sma_reversal
            be_peak    = break_even_peak_pct
            be_floor   = break_even_floor_pct
        else:
            sl_pct     = stop_loss_pct
            trail_peak = trail_activate_pct
            trail_dd   = trail_dd_pct
            tp_pct     = take_profit_pct
            ts_days    = time_stop_days
            ts_min     = time_stop_min_ret
            ef_days    = early_fail_days
            ef_max     = early_fail_max_ret
            sma_rev    = enable_sma_reversal
            be_peak    = break_even_peak_pct
            be_floor   = break_even_floor_pct

        exit_reason: str | None = None

        # 0) ATR exits (MRIT only, fires before percentage rules)
        if is_mrit and (mrit_tp_atr_mult is not None or mrit_sl_atr_mult is not None):
            ohlcv = _ohlcv_up_to(
                ohlcv_by_sym.get(sym, []),
                ohlcv_dates_by_sym.get(sym, []),
                asof,
            )
            atr = _compute_atr(ohlcv, mrit_atr_period)
            if atr is not None:
                sl_px = (pos.entry_price - mrit_sl_atr_mult * atr) if mrit_sl_atr_mult is not None else None
                tp_px = (pos.entry_price + mrit_tp_atr_mult * atr) if mrit_tp_atr_mult is not None else None
                if sl_px is not None and price <= sl_px:
                    exit_reason = f"atr_stop_loss ret={ret:.2f}%"
                elif tp_px is not None and price >= tp_px:
                    exit_reason = f"atr_take_profit ret={ret:.2f}%"

        # 0.5) Portfolio-level dollar stop (PORTFOLIO only; fires before hard stop)
        # Mirrors exits_advisor priority-95 rule.
        if exit_reason is None and is_portfolio and portfolio_stop_dollar_pct > 0:
            current_equity = broker.equity(prices)
            if current_equity > 0:
                unrealized_pnl_dollars = (price - pos.entry_price) * pos.qty
                if unrealized_pnl_dollars < 0:
                    loss_pct_of_port = abs(unrealized_pnl_dollars) / current_equity * 100
                    if loss_pct_of_port >= portfolio_stop_dollar_pct:
                        exit_reason = f"portfolio_stop_dollar:{loss_pct_of_port:.2f}%_of_equity"

        # 1) Hard stop loss
        if exit_reason is None and ret <= -abs(sl_pct):
            exit_reason = f"stop_loss ret={ret:.2f}%"

        # 2) Trailing stop
        if exit_reason is None and peak_gain >= trail_peak and dd <= -abs(trail_dd):
            exit_reason = f"trailing_stop peak={peak_gain:.2f}% dd={dd:.2f}%"

        # 3) Break-even floor
        if exit_reason is None and peak_gain >= be_peak and ret < be_floor:
            exit_reason = f"break_even_floor ret={ret:.2f}%"

        # 4) SMA reversal (per-profile enable flag)
        if exit_reason is None and sma_rev:
            closes = _closes_up_to(bars_by_sym.get(sym, []), dates_by_sym.get(sym, []), asof)
            if len(closes) >= 50:
                tail = closes[-50:]
                sma20_v = sum(tail[-20:]) / 20.0
                sma50_v = sum(tail) / 50.0
                if sma20_v < sma50_v:
                    exit_reason = f"sma_reversal sma20={sma20_v:.4f} sma50={sma50_v:.4f}"

        # 5) Early failure stop
        if exit_reason is None and days >= ef_days and ret <= ef_max:
            exit_reason = f"early_fail days={days} ret={ret:.2f}%"

        # 6) Time stop
        if exit_reason is None and days >= ts_days and ret < ts_min:
            exit_reason = f"time_stop days={days} ret={ret:.2f}%"

        # 7) Take profit
        if exit_reason is None and ret >= tp_pct:
            exit_reason = f"take_profit ret={ret:.2f}%"

        if exit_reason:
            exits.append((sym, exit_reason))
    return exits


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------

def _compute_metrics(
    trades: list[dict],
    equity_curve: list[dict],
    initial_capital: float,
) -> dict:
    if not equity_curve:
        return {"total_return_pct": 0.0, "trades": 0}

    final_equity = equity_curve[-1]["equity"]
    total_return = ((final_equity / initial_capital) - 1.0) * 100.0

    n_days = len(equity_curve)
    years = n_days / 252.0
    ann_return = (((final_equity / initial_capital) ** (1.0 / max(years, 0.001))) - 1.0) * 100.0 if years > 0 else 0.0

    daily_returns = []
    for i in range(1, len(equity_curve)):
        e0 = equity_curve[i - 1]["equity"]
        e1 = equity_curve[i]["equity"]
        if e0 > 0:
            daily_returns.append((e1 / e0) - 1.0)

    sharpe = 0.0
    if len(daily_returns) >= 2:
        avg_r = sum(daily_returns) / len(daily_returns)
        std_r = statistics.stdev(daily_returns) if len(daily_returns) > 1 else 0.0
        if std_r > 0:
            sharpe = (avg_r / std_r) * math.sqrt(252)

    max_dd = 0.0
    peak = equity_curve[0]["equity"]
    for row in equity_curve:
        e = row["equity"]
        if e > peak:
            peak = e
        if peak > 0:
            dd = (e - peak) / peak * 100.0
            if dd < max_dd:
                max_dd = dd

    closed = [t for t in trades if t.get("exit_reason") != "end_of_backtest"]
    wins = [t for t in closed if (t.get("realized_pnl") or 0.0) > 0]
    win_rate = len(wins) / len(closed) * 100.0 if closed else 0.0
    avg_ret = sum(t.get("return_pct") or 0.0 for t in closed) / len(closed) if closed else 0.0

    # Order counts and P&L breakdown by signal_key (all trades, including open)
    orders_by_signal: dict[str, dict] = {}
    for t in trades:
        sk = t.get("signal_key") or "unknown"
        if sk not in orders_by_signal:
            orders_by_signal[sk] = {"orders": 0, "closed": 0, "wins": 0, "pnl": 0.0}
        orders_by_signal[sk]["orders"] += 1
        if t.get("exit_reason") != "end_of_backtest":
            orders_by_signal[sk]["closed"] += 1
            if (t.get("realized_pnl") or 0.0) > 0:
                orders_by_signal[sk]["wins"] += 1
            orders_by_signal[sk]["pnl"] += t.get("realized_pnl") or 0.0

    return {
        "final_equity": round(final_equity, 2),
        "total_return_pct": round(total_return, 3),
        "annualized_return_pct": round(ann_return, 3),
        "sharpe_ratio": round(sharpe, 3),
        "max_drawdown_pct": round(max_dd, 3),
        "win_rate_pct": round(win_rate, 2),
        "total_trades": len(closed),
        "wins": len(wins),
        "avg_trade_return_pct": round(avg_ret, 3),
        "trading_days": n_days,
        "orders_by_signal": orders_by_signal,
    }


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _save_results(
    *,
    backtest_db_path: str | None,
    strategy: str,
    start: str,
    end: str,
    universe: str,
    initial_capital: float,
    max_positions: int,
    per_position_notional: float,
    compound: bool,
    trades: list[dict],
    equity_curve: list[dict],
    summary: dict,
) -> int:
    conn = connect_backtest(backtest_db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO backtest_runs
                (strategy, start_date, end_date, universe, initial_capital,
                 max_positions, per_position_notional, compound, summary_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (strategy, start, end, universe, initial_capital,
             max_positions, per_position_notional, int(compound), json.dumps(summary)),
        )
        run_id = cur.lastrowid

        conn.executemany(
            """
            INSERT INTO backtest_trades
                (run_id, symbol, entry_date, exit_date, entry_price, exit_price,
                 qty, realized_pnl, return_pct, exit_reason, signal_key,
                 pyramid_adds, total_cost_basis, avg_entry_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (run_id, t["symbol"], t["entry_date"], t.get("exit_date"),
                 t["entry_price"], t.get("exit_price"), t["qty"],
                 t.get("realized_pnl"), t.get("return_pct"), t.get("exit_reason"),
                 t.get("signal_key"),
                 t.get("pyramid_adds", 0), t.get("total_cost_basis"), t.get("avg_entry_price"))
                for t in trades
            ],
        )

        conn.executemany(
            "INSERT INTO backtest_equity_curve (run_id, date, equity, cash) VALUES (?, ?, ?, ?)",
            [(run_id, row["date"], row["equity"], row["cash"]) for row in equity_curve],
        )

        conn.commit()
        return run_id
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_backtest(
    strategy: str = "both",
    *,
    start: str,
    end: str,
    universe: str = "sp500",
    initial_capital: float = 100_000.0,
    max_positions: int = 20,
    per_position_notional: float | None = None,
    backtest_db_path: str | None = None,
    fast: int = 20,
    slow: int = 50,
    cooldown_days: int = 0,
    atr_stop_cooldown_days: int = 0,
    # DEFAULT profile exit params
    stop_loss_pct: float = 5.0,
    trail_activate_pct: float = 8.0,
    trail_dd_pct: float = 4.0,
    take_profit_pct: float = 15.0,
    time_stop_days: int = 30,
    time_stop_min_ret: float = 2.0,
    early_fail_days: int = 5,
    early_fail_max_ret: float = 0.0,
    enable_sma_reversal: bool = True,
    break_even_peak_pct: float = 10.0,
    break_even_floor_pct: float = 2.0,
    # MRIT profile exit params
    mrit_atr_period: int = 14,
    mrit_tp_atr_mult: float | None = None,
    mrit_sl_atr_mult: float | None = None,
    mrit_early_fail_days: int = 5,
    mrit_early_fail_max_ret: float = 0.0,
    mrit_time_stop_days: int = 30,
    mrit_time_stop_min_ret: float = 2.0,
    mrit_enable_sma_reversal: bool = False,  # matches live .env TRADING_EXIT_PROFILE_MRIT_ENABLE_SMA_REVERSAL=0
    # Simulate-live mode
    simulate_live: bool = False,
    # Compound position sizing
    compound: bool = False,
    # PORTFOLIO profile exit params (defaults match live .env TRADING_EXIT_PROFILE_PORTFOLIO_*)
    portfolio_stop_loss_pct: float = 15.0,
    portfolio_trail_activate_pct: float = 20.0,
    portfolio_trail_dd_pct: float = 10.0,
    portfolio_take_profit_pct: float = 99999.0,
    portfolio_time_stop_days: int = 60,
    portfolio_time_stop_min_ret: float = 0.0,
    portfolio_early_fail_days: int = 999,
    portfolio_early_fail_max_ret: float = 0.0,
    portfolio_enable_sma_reversal: bool = False,
    portfolio_break_even_peak_pct: float = 15.0,
    portfolio_break_even_floor_pct: float = 3.0,
    portfolio_stop_dollar_pct: float = 0.0,
) -> dict:
    """
    Run a backtest over [start, end].

    strategy: "sma" | "mrit" | "both" (default "both").
      Regime is computed each day from backtest SPY bars and gates which
      strategies generate buy signals — mirroring the paper environment.

    simulate_live=True: Read all exit parameters from the same env vars the
      live system uses (.env via dotenv), so results reflect what the live
      system would have done with the current configuration.

    Fills model: signals on day T are filled at day T+1's close.
    """
    if strategy not in ("sma", "mrit", "both", "portfolio"):
        raise ValueError(f"Unknown strategy: {strategy!r}. Use 'sma', 'mrit', 'both', or 'portfolio'.")

    if per_position_notional is None:
        per_position_notional = initial_capital / max_positions

    # -----------------------------------------------------------------------
    # simulate_live: override all exit params from .env (same vars live uses)
    # -----------------------------------------------------------------------
    if simulate_live:
        def _ef(k: str, d: float) -> float:
            v = os.environ.get(k)
            return float(v) if v is not None else d

        def _ei(k: str, d: int) -> int:
            v = os.environ.get(k)
            return int(v) if v is not None else d

        def _eb(k: str, d: bool) -> bool:
            v = os.environ.get(k)
            return v.strip() in ("1", "true", "t", "yes", "y", "on") if v is not None else d

        stop_loss_pct = _ef("TRADING_EXIT_STOP_LOSS_PCT", stop_loss_pct)
        take_profit_pct = _ef("TRADING_EXIT_TAKE_PROFIT_PCT", take_profit_pct)
        trail_activate_pct = _ef("TRADING_EXIT_TRAIL_PEAK_PCT", trail_activate_pct)
        trail_dd_pct = _ef("TRADING_EXIT_TRAIL_DD_PCT", trail_dd_pct)
        break_even_peak_pct = _ef("TRADING_EXIT_BREAKEVEN_PEAK_PCT", break_even_peak_pct)
        break_even_floor_pct = _ef("TRADING_EXIT_BREAKEVEN_FLOOR_PCT", break_even_floor_pct)
        early_fail_days = _ei("TRADING_EXIT_EARLY_FAIL_DAYS", early_fail_days)
        early_fail_max_ret = _ef("TRADING_EXIT_EARLY_FAIL_MAX_RET_PCT", early_fail_max_ret)
        time_stop_days = _ei("TRADING_EXIT_TIME_STOP_DAYS", time_stop_days)
        time_stop_min_ret = _ef("TRADING_EXIT_TIME_STOP_MIN_RET_PCT", time_stop_min_ret)
        enable_sma_reversal = _eb("TRADING_EXIT_ENABLE_SMA_REVERSAL", enable_sma_reversal)

        mrit_atr_period = _ei("TRADING_EXIT_PROFILE_MRIT_ATR_PERIOD", mrit_atr_period)
        _tp = os.environ.get("TRADING_EXIT_PROFILE_MRIT_TP_ATR_MULT")
        mrit_tp_atr_mult = float(_tp) if _tp is not None else mrit_tp_atr_mult
        _sl = os.environ.get("TRADING_EXIT_PROFILE_MRIT_SL_ATR_MULT")
        mrit_sl_atr_mult = float(_sl) if _sl is not None else mrit_sl_atr_mult
        mrit_early_fail_days = _ei("TRADING_EXIT_PROFILE_MRIT_EARLY_FAIL_DAYS", mrit_early_fail_days)
        mrit_early_fail_max_ret = _ef("TRADING_EXIT_PROFILE_MRIT_EARLY_FAIL_MAX_RET_PCT", mrit_early_fail_max_ret)
        mrit_time_stop_days = _ei("TRADING_EXIT_PROFILE_MRIT_TIME_STOP_DAYS", mrit_time_stop_days)
        mrit_time_stop_min_ret = _ef("TRADING_EXIT_PROFILE_MRIT_TIME_STOP_MIN_RET_PCT", mrit_time_stop_min_ret)
        mrit_enable_sma_reversal = _eb("TRADING_EXIT_PROFILE_MRIT_ENABLE_SMA_REVERSAL", mrit_enable_sma_reversal)

        if strategy == "portfolio":
            portfolio_stop_loss_pct = _ef("TRADING_EXIT_PROFILE_PORTFOLIO_STOP_LOSS_PCT", portfolio_stop_loss_pct)
            portfolio_trail_activate_pct = _ef("TRADING_EXIT_PROFILE_PORTFOLIO_TRAIL_PEAK_PCT", portfolio_trail_activate_pct)
            portfolio_trail_dd_pct = _ef("TRADING_EXIT_PROFILE_PORTFOLIO_TRAIL_DD_PCT", portfolio_trail_dd_pct)
            portfolio_take_profit_pct = _ef("TRADING_EXIT_PROFILE_PORTFOLIO_TAKE_PROFIT_PCT", portfolio_take_profit_pct)
            portfolio_time_stop_days = _ei("TRADING_EXIT_PROFILE_PORTFOLIO_TIME_STOP_DAYS", portfolio_time_stop_days)
            portfolio_time_stop_min_ret = _ef("TRADING_EXIT_PROFILE_PORTFOLIO_TIME_STOP_MIN_RET_PCT", portfolio_time_stop_min_ret)
            portfolio_early_fail_days = _ei("TRADING_EXIT_PROFILE_PORTFOLIO_EARLY_FAIL_DAYS", portfolio_early_fail_days)
            portfolio_early_fail_max_ret = _ef("TRADING_EXIT_PROFILE_PORTFOLIO_EARLY_FAIL_MAX_RET_PCT", portfolio_early_fail_max_ret)
            portfolio_enable_sma_reversal = _eb("TRADING_EXIT_PROFILE_PORTFOLIO_ENABLE_SMA_REVERSAL", portfolio_enable_sma_reversal)
            portfolio_break_even_peak_pct = _ef("TRADING_EXIT_PROFILE_PORTFOLIO_BREAKEVEN_PEAK_PCT", portfolio_break_even_peak_pct)
            portfolio_break_even_floor_pct = _ef("TRADING_EXIT_PROFILE_PORTFOLIO_BREAKEVEN_FLOOR_PCT", portfolio_break_even_floor_pct)
            _ps = os.environ.get("TRADING_EXIT_PROFILE_PORTFOLIO_PORTFOLIO_STOP_PCT")
            portfolio_stop_dollar_pct = float(_ps) if _ps is not None else portfolio_stop_dollar_pct

        if not compound:  # CLI --compound takes priority; only override if not explicitly set
            compound = _eb("TRADING_COMPOUND_SIZING", compound)
        if atr_stop_cooldown_days == 0:
            atr_stop_cooldown_days = _ei("TRADING_ATR_STOP_COOLDOWN_DAYS", atr_stop_cooldown_days)

    # -----------------------------------------------------------------------
    # Pyramid ladder config (portfolio mode only; OFF by default)
    # -----------------------------------------------------------------------
    pyramid_enabled = False
    pyramid_ladder: list[float] = []
    pyramid_sizes: list[float] = []
    pyramid_max_mult: float = 2.0

    if strategy == "portfolio":
        _penv = os.environ.get("TRADING_PORTFOLIO_PYRAMID_ENABLED", "0").strip().lower()
        pyramid_enabled = _penv in ("1", "true", "yes", "on")
        if pyramid_enabled:
            try:
                pyramid_ladder = [
                    float(x) for x in
                    os.environ.get("TRADING_PORTFOLIO_PYRAMID_LADDER", "20,50,100").split(",")
                ]
                pyramid_sizes = [
                    float(x) for x in
                    os.environ.get("TRADING_PORTFOLIO_PYRAMID_SIZES", "0.5,0.33,0.17").split(",")
                ]
                pyramid_max_mult = float(
                    os.environ.get("TRADING_PORTFOLIO_PYRAMID_MAX_MULT", "2.0")
                )
                if len(pyramid_ladder) != len(pyramid_sizes):
                    raise ValueError(
                        f"PYRAMID_LADDER length ({len(pyramid_ladder)}) != "
                        f"PYRAMID_SIZES length ({len(pyramid_sizes)})"
                    )
                if pyramid_ladder != sorted(pyramid_ladder) or len(set(pyramid_ladder)) != len(pyramid_ladder):
                    raise ValueError(f"PYRAMID_LADDER must be strictly ascending: {pyramid_ladder}")
                if any(x <= 0 for x in pyramid_ladder) or any(x <= 0 for x in pyramid_sizes):
                    raise ValueError("All PYRAMID_LADDER and PYRAMID_SIZES values must be positive")
                if 1.0 + sum(pyramid_sizes) > pyramid_max_mult:
                    log.warning(
                        "PYRAMID: 1.0+sum(SIZES)=%.2f exceeds MAX_MULT=%.2f; "
                        "higher rungs may be skipped by the cap",
                        1.0 + sum(pyramid_sizes), pyramid_max_mult,
                    )
                log.info(
                    "PYRAMID enabled: ladder=%s sizes=%s max_mult=%.1f",
                    pyramid_ladder, pyramid_sizes, pyramid_max_mult,
                )
            except (ValueError, TypeError) as e:
                log.error("PYRAMID config error: %s — pyramiding disabled for this run", e)
                pyramid_enabled = False

    init_backtest_db(backtest_db_path)

    # -----------------------------------------------------------------------
    # Load universe + bars
    # -----------------------------------------------------------------------
    lookback_start = (
        datetime.strptime(start, "%Y-%m-%d") - timedelta(days=400)
    ).strftime("%Y-%m-%d")

    from .db import connect_backtest
    from ..db import connect as live_connect

    with live_connect() as live_conn:
        sym_rows = live_conn.execute(
            "SELECT DISTINCT symbol FROM universe_membership WHERE universe=?",
            (universe,),
        ).fetchall()
    from ..exclusions import get_active_exclusions
    if strategy == "portfolio":
        excluded = get_active_exclusions("portfolio")
    elif strategy == "mrit":
        excluded = get_active_exclusions("mrit")
    elif strategy == "sma":
        excluded = get_active_exclusions("sma")
    elif strategy == "both":
        excluded = get_active_exclusions("mrit") | get_active_exclusions("sma")
    else:
        excluded = get_active_exclusions("all")
    pool_syms = {r["symbol"] for r in sym_rows}
    symbols = [r["symbol"] for r in sym_rows if r["symbol"] not in excluded]
    if excluded:
        removed_from_pool = len(sym_rows) - len(symbols)
        already_absent = len(excluded - pool_syms)
        if already_absent:
            print(
                f"Backtest exclusions filter: {removed_from_pool} removed from pool, "
                f"{already_absent} already absent ({len(excluded)} total in exclusion set) "
                f"— {strategy} scope"
            )
        else:
            print(
                f"Backtest exclusions filter: removed {removed_from_pool} of "
                f"{len(sym_rows)} symbols — {strategy} scope"
            )
    if not symbols:
        raise RuntimeError(
            f"No symbols in universe '{universe}'. "
            "Run: trading load-universe --file data/sp500.csv"
        )

    with connect_backtest(backtest_db_path) as bt_conn:
        day_rows = bt_conn.execute(
            """
            SELECT DISTINCT t FROM bars_daily
            WHERE t >= ? AND t <= ? AND c IS NOT NULL
            ORDER BY t ASC
            """,
            (start, end),
        ).fetchall()
        trading_days: list[str] = [r["t"] for r in day_rows]

        if not trading_days:
            raise RuntimeError(
                f"No bars found in backtest DB for {start} to {end}.\n"
                f"Fetch split-adjusted data first:\n"
                f"  trading backtest fetch-bars --start {lookback_start} --end {end}"
            )

        sym_placeholders = ",".join("?" * len(symbols))
        bar_rows = bt_conn.execute(
            f"""
            SELECT symbol, t, o, h, l, c
            FROM bars_daily
            WHERE symbol IN ({sym_placeholders})
              AND t >= ?
              AND c IS NOT NULL
            ORDER BY symbol, t ASC
            """,
            (*symbols, lookback_start),
        ).fetchall()

        spy_rows = bt_conn.execute(
            """
            SELECT t, c FROM bars_daily
            WHERE symbol = 'SPY' AND t >= ? AND c IS NOT NULL
            ORDER BY t ASC
            """,
            (lookback_start,),
        ).fetchall()

    if not bar_rows:
        raise RuntimeError(
            f"Backtest DB has no price data for universe symbols.\n"
            f"Run: trading backtest fetch-bars --start {lookback_start} --end {end}"
        )

    # Build close-only bars (for signals/SMA) and OHLCV bars (for ATR) from one pass
    bars_by_sym: dict[str, list[tuple[str, float]]] = defaultdict(list)
    ohlcv_by_sym: dict[str, list[tuple[str, float, float, float, float]]] = defaultdict(list)
    for r in bar_rows:
        c = float(r["c"])
        bars_by_sym[r["symbol"]].append((r["t"], c))
        o = float(r["o"]) if r["o"] is not None else c
        h = float(r["h"]) if r["h"] is not None else c
        l_ = float(r["l"]) if r["l"] is not None else c
        ohlcv_by_sym[r["symbol"]].append((r["t"], o, h, l_, c))

    dates_by_sym: dict[str, list[str]] = {
        sym: [b[0] for b in bars] for sym, bars in bars_by_sym.items()
    }
    ohlcv_dates_by_sym: dict[str, list[str]] = {
        sym: [b[0] for b in bars] for sym, bars in ohlcv_by_sym.items()
    }

    spy_bars = [(r["t"], float(r["c"])) for r in spy_rows]
    spy_dates = [b[0] for b in spy_bars]

    # -----------------------------------------------------------------------
    # Simulation loop
    # -----------------------------------------------------------------------
    broker = SimBroker(cash=initial_capital)
    holding_days: dict[str, int] = {}
    cooldown_until: dict[str, str] = {}
    pending_orders: list[dict] = []
    trades: list[dict] = []
    equity_curve: list[dict] = []

    for day in trading_days:
        prices: dict[str, float] = {}
        for sym, bars in bars_by_sym.items():
            p = _price_on_or_before(bars, dates_by_sym[sym], day)
            if p is not None:
                prices[sym] = p
        spy_p = _price_on_or_before(spy_bars, spy_dates, day)
        if spy_p is not None:
            prices["SPY"] = spy_p

        # 1. Fill pending orders from previous day at today's close
        filled_buys: set[str] = set()
        filled_sells: set[str] = set()

        for order in pending_orders:
            sym = order["symbol"]
            price = prices.get(sym)
            if price is None:
                continue

            if order["action"] == "sell" and sym in broker.positions:
                pos = broker.positions[sym]  # capture before fill_sell pops it
                qty, avg_ep, pnl = broker.fill_sell(sym, price)
                # Use original_entry_price as reference (consistent with _check_exits).
                # For non-pyramid positions original_entry_price == avg_ep.
                ref_ep = pos.original_entry_price if pos.original_entry_price > 0 else avg_ep
                ret_pct = ((price - ref_ep) / ref_ep) * 100.0
                trades.append({
                    "symbol": sym,
                    "entry_date": pos.entry_date,
                    "exit_date": day,
                    "entry_price": ref_ep,
                    "exit_price": price,
                    "qty": qty,
                    "realized_pnl": pnl,
                    "return_pct": ret_pct,
                    "exit_reason": order.get("reason", "signal"),
                    "signal_key": pos.signal_key,
                    "pyramid_adds": len(pos.pyramid_rungs_hit),
                    "total_cost_basis": pos.entry_notional,
                    "avg_entry_price": avg_ep,
                })
                holding_days.pop(sym, None)
                filled_sells.add(sym)
                if cooldown_days > 0 and "stop_loss" in order.get("reason", ""):
                    cooldown_until[sym] = (
                        datetime.strptime(day, "%Y-%m-%d") + timedelta(days=cooldown_days)
                    ).strftime("%Y-%m-%d")
                if atr_stop_cooldown_days > 0 and order.get("reason", "").startswith("atr_stop_loss"):
                    atr_cd = (
                        datetime.strptime(day, "%Y-%m-%d") + timedelta(days=atr_stop_cooldown_days)
                    ).strftime("%Y-%m-%d")
                    if atr_cd > cooldown_until.get(sym, ""):
                        cooldown_until[sym] = atr_cd

            elif order["action"] == "buy" and sym not in broker.positions and sym not in filled_buys:
                sig_key = order.get("signal_key", "")
                qty = broker.fill_buy(sym, price, order.get("notional", per_position_notional), day, signal_key=sig_key)
                if qty > 0:
                    holding_days[sym] = 0
                    filled_buys.add(sym)

        pending_orders = []

        # 2. Update peak prices and increment holding day counters
        for sym in list(broker.positions.keys()):
            p = prices.get(sym)
            if p:
                broker.update_peak(sym, p)
            holding_days[sym] = holding_days.get(sym, 0) + 1

        # 3. Check exit rules for open positions
        exits = _check_exits(
            broker, prices, holding_days,
            bars_by_sym, dates_by_sym,
            ohlcv_by_sym, ohlcv_dates_by_sym,
            day,
            stop_loss_pct=stop_loss_pct,
            trail_activate_pct=trail_activate_pct,
            trail_dd_pct=trail_dd_pct,
            take_profit_pct=take_profit_pct,
            time_stop_days=time_stop_days,
            time_stop_min_ret=time_stop_min_ret,
            early_fail_days=early_fail_days,
            early_fail_max_ret=early_fail_max_ret,
            enable_sma_reversal=enable_sma_reversal,
            break_even_peak_pct=break_even_peak_pct,
            break_even_floor_pct=break_even_floor_pct,
            mrit_atr_period=mrit_atr_period,
            mrit_tp_atr_mult=mrit_tp_atr_mult,
            mrit_sl_atr_mult=mrit_sl_atr_mult,
            mrit_early_fail_days=mrit_early_fail_days,
            mrit_early_fail_max_ret=mrit_early_fail_max_ret,
            mrit_time_stop_days=mrit_time_stop_days,
            mrit_time_stop_min_ret=mrit_time_stop_min_ret,
            mrit_enable_sma_reversal=mrit_enable_sma_reversal,
            portfolio_stop_loss_pct=portfolio_stop_loss_pct,
            portfolio_trail_activate_pct=portfolio_trail_activate_pct,
            portfolio_trail_dd_pct=portfolio_trail_dd_pct,
            portfolio_take_profit_pct=portfolio_take_profit_pct,
            portfolio_time_stop_days=portfolio_time_stop_days,
            portfolio_time_stop_min_ret=portfolio_time_stop_min_ret,
            portfolio_early_fail_days=portfolio_early_fail_days,
            portfolio_early_fail_max_ret=portfolio_early_fail_max_ret,
            portfolio_enable_sma_reversal=portfolio_enable_sma_reversal,
            portfolio_break_even_peak_pct=portfolio_break_even_peak_pct,
            portfolio_break_even_floor_pct=portfolio_break_even_floor_pct,
            portfolio_stop_dollar_pct=portfolio_stop_dollar_pct,
        )
        exit_syms = {sym for sym, _ in exits}
        for sym, reason in exits:
            pending_orders.append({"action": "sell", "symbol": sym, "reason": reason})

        # 3.5) Pyramid adds: add capital to winning portfolio positions
        # Immediate fills at today's close (not T+1) — threshold crossed is known today.
        if pyramid_enabled and strategy == "portfolio":
            for sym in list(broker.positions.keys()):
                if sym in exit_syms:
                    continue  # position already slated for exit
                pos = broker.positions[sym]
                if not pos.signal_key.startswith("portfolio_"):
                    continue
                price_today = prices.get(sym)
                if price_today is None:
                    continue
                result = _portfolio_pyramid_check(
                    pos, price_today,
                    set(pos.pyramid_rungs_hit),
                    pyramid_ladder, pyramid_sizes, pyramid_max_mult,
                )
                if result is None:
                    continue
                rung_idx, add_notional = result
                qty_added = broker.fill_buy(
                    sym, price_today, add_notional, day, pos.signal_key,
                    _pyramid_rung=rung_idx,
                )
                if qty_added > 0:
                    log.debug(
                        "PYRAMID add #%d on %s: $%.2f at $%.2f (gain=%.1f%% from orig $%.2f)",
                        rung_idx + 1, sym, add_notional, price_today,
                        (price_today / pos.original_entry_price - 1.0) * 100.0,
                        pos.original_entry_price,
                    )

        # 4. Compute regime for today, then generate signals
        spy_closes_today = _closes_up_to(spy_bars, spy_dates, day)
        regime_facts = _compute_regime(spy_closes_today)
        if compound:
            position_pct = per_position_notional / initial_capital
            current_equity = broker.equity(prices)
            day_notional = current_equity * position_pct * regime_facts["buy_notional_mult"]
        else:
            day_notional = per_position_notional * regime_facts["buy_notional_mult"]
        day_notional = max(1.0, day_notional)

        raw_signals: list[dict] = []
        if strategy in ("sma", "both"):
            raw_signals += _sma_signals(
                symbols, bars_by_sym, dates_by_sym, day, regime_facts,
                fast=fast, slow=slow,
            )
        if strategy in ("mrit", "both"):
            raw_signals += _mrit_signals(
                symbols, bars_by_sym, dates_by_sym, day, regime_facts,
            )
        if strategy == "portfolio":
            raw_signals += _portfolio_signals(
                symbols, bars_by_sym, dates_by_sym,
                spy_closes_today, day, regime_facts,
            )

        # Dedup: keep strongest buy per symbol (in "both" mode two strategies may fire)
        if strategy == "both":
            seen: dict[str, dict] = {}
            deduped: list[dict] = []
            for sig in sorted(raw_signals, key=lambda s: (s["action"] != "buy", -(s.get("strength") or 0.0))):
                sym = sig["symbol"]
                if sig["action"] == "buy":
                    if sym not in seen:
                        seen[sym] = sig
                        deduped.append(sig)
                else:
                    deduped.append(sig)
            raw_signals = deduped

        # Queue sell signals for held positions not already captured by exit rules
        for sig in raw_signals:
            sym = sig["symbol"]
            if sig["action"] == "sell" and sym in broker.positions and sym not in exit_syms:
                pending_orders.append({"action": "sell", "symbol": sym, "reason": sig["reason"]})
                exit_syms.add(sym)

        # 5. Plan buys: sort by strength, pick top candidates
        pending_buy_syms = {o["symbol"] for o in pending_orders if o["action"] == "buy"}
        open_count = len(broker.positions) + len(pending_buy_syms)
        slots = max(0, max_positions - open_count)

        buy_candidates = [
            s for s in raw_signals
            if s["action"] == "buy"
            and s["symbol"] not in broker.positions
            and s["symbol"] not in pending_buy_syms
            and s["symbol"] not in exit_syms
            and cooldown_until.get(s["symbol"], "") < day
        ]
        buy_candidates.sort(key=lambda s: float(s.get("strength") or 0.0), reverse=True)

        notional_queued = 0.0
        for sig in buy_candidates[:slots]:
            if broker.cash - notional_queued < day_notional * 0.5:
                break
            sig_key = sig.get("signal_key", "sma20_cross_up_sma50")
            pending_orders.append({
                "action": "buy",
                "symbol": sig["symbol"],
                "reason": sig["reason"],
                "notional": day_notional,
                "signal_key": sig_key,
            })
            pending_buy_syms.add(sig["symbol"])
            notional_queued += day_notional

        # 6. Record daily equity snapshot
        equity_curve.append({"date": day, "equity": broker.equity(prices), "cash": broker.cash})

    # -----------------------------------------------------------------------
    # Close remaining open positions at final-day close (marked as open)
    # -----------------------------------------------------------------------
    last_day = trading_days[-1]
    for sym in list(broker.positions.keys()):
        pos = broker.positions[sym]
        price = prices.get(sym, pos.entry_price)
        qty, avg_ep, pnl = broker.fill_sell(sym, price)
        ref_ep = pos.original_entry_price if pos.original_entry_price > 0 else avg_ep
        ret_pct = ((price - ref_ep) / ref_ep) * 100.0
        trades.append({
            "symbol": sym,
            "entry_date": pos.entry_date,
            "exit_date": last_day,
            "entry_price": ref_ep,
            "exit_price": price,
            "qty": qty,
            "realized_pnl": pnl,
            "return_pct": ret_pct,
            "exit_reason": "end_of_backtest",
            "signal_key": pos.signal_key,
            "pyramid_adds": len(pos.pyramid_rungs_hit),
            "total_cost_basis": pos.entry_notional,
            "avg_entry_price": avg_ep,
        })

    # -----------------------------------------------------------------------
    # Compute metrics and persist
    # -----------------------------------------------------------------------
    summary = _compute_metrics(trades, equity_curve, initial_capital)

    run_id = _save_results(
        backtest_db_path=backtest_db_path,
        strategy=strategy,
        start=start,
        end=end,
        universe=universe,
        initial_capital=initial_capital,
        max_positions=max_positions,
        per_position_notional=per_position_notional,
        compound=compound,
        trades=trades,
        equity_curve=equity_curve,
        summary=summary,
    )

    summary["run_id"] = run_id
    summary["trades_detail"] = trades
    return summary