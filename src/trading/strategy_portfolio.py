from __future__ import annotations

import logging
import os
from typing import Optional, TYPE_CHECKING

from .db import connect
from .strategy_sma import Signal

if TYPE_CHECKING:
    from .regime import RegimeState

log = logging.getLogger("trading")


def _sma(values: list[float], window: int) -> list[Optional[float]]:
    """Simple moving average aligned to the input list (None until sufficient data)."""
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


def _market_gate(conn, *, asof: str, proxy: str, sma_n: int) -> tuple[bool, str]:
    """
    Portfolio market gate: allow buys only when proxy close > proxy SMA(sma_n).
    Fail-closed: insufficient data → gate OFF.
    """
    rows = conn.execute(
        "SELECT c FROM bars_daily WHERE symbol=? AND t<=? AND c IS NOT NULL ORDER BY t ASC;",
        (proxy, asof),
    ).fetchall()
    closes = [float(r["c"]) for r in rows]

    if len(closes) < sma_n + 5:
        return False, (
            f"PORTFOLIO gated OFF: insufficient {proxy} bars for SMA{sma_n} "
            f"(have {len(closes)})"
        )

    sma = _sma(closes, sma_n)
    i = len(closes) - 1
    if sma[i] is None:
        return False, f"PORTFOLIO gated OFF: {proxy} SMA{sma_n} unavailable"

    c, s = closes[i], float(sma[i])
    if c > s:
        return True, f"PORTFOLIO gate OK: {proxy} c({c:.2f}) > SMA{sma_n}({s:.2f})"
    return False, f"PORTFOLIO gated OFF: {proxy} c({c:.2f}) <= SMA{sma_n}({s:.2f})"


def generate_signals_portfolio(
    *,
    universe: str = "sp500",
    asof: str | None = None,
    regime: "RegimeState | None" = None,
    lookback_min: int = 210,
    sma_long: int = 200,
    sma_mid: int = 50,
    ext_days: int = 20,
    ext_max: float = 0.20,
    return_days: int = 126,
    top_pct: float = 0.25,
) -> list[Signal]:
    """
    Portfolio momentum strategy (daily bars).

    Entry conditions — ALL must pass for a buy signal:
      a) price > SMA(sma_long=200)     long-term trend filter
      b) price > SMA(sma_mid=50)       medium-term trend filter
      c) top top_pct=25% of universe by 6-month (return_days=126) return
         Percentile computed against the active filtered universe snapshot,
         not the full constituent list.
      d) RS vs proxy positive: (sym_6m_return - spy_6m_return) > 0
      e) not extended: (price / price_ext_days=20 ago - 1) < ext_max=20%

    Signal output:
      buy  → reason="PORTFOLIO: momentum + trend + RS leader, not extended"
              strength=6m_return (planner ranks stronger movers first)
      hold → reason explains which condition failed

    This strategy emits NO sell signals. Exits are handled entirely by
    exits_advisor.evaluate_exit_advice() using the PORTFOLIO exit profile,
    dispatched via entry_signal_key prefix "portfolio_".

    Regime gating (identical pattern to SMA crossover):
      defensive / crash → suppress all buys; return all holds
      bull / pullback   → proceed with entry logic
      regime=None       → internal check: proxy close > SMA200 (fail-closed)

    Market proxy uses TRADING_MRIT_MARKET_PROXY env var (default SPY);
    market gate SMA is always 200 regardless of TRADING_MRIT_MARKET_SMA
    (portfolio entry condition a defines the long-term trend as SMA200).
    """
    from .asof import resolve_asof_date
    from .exclusions import get_active_exclusions

    proxy = os.getenv("TRADING_MRIT_MARKET_PROXY", "SPY").strip().upper()
    # Market gate for portfolio always uses SMA200 (matches entry condition a)
    market_gate_sma = 200

    with connect() as conn:
        asof = resolve_asof_date(conn, asof)

        # ── Regime / market gate ──────────────────────────────────────────────
        if regime is not None:
            if regime.regime in ("defensive", "crash"):
                market_bullish = False
                gate_note = f"PORTFOLIO suppressed: regime={regime.regime}"
                log.info(gate_note)
            else:
                market_bullish = True
                gate_note = f"PORTFOLIO gate OK: regime={regime.regime}"
        else:
            market_bullish, gate_note = _market_gate(
                conn, asof=asof, proxy=proxy, sma_n=market_gate_sma
            )
            log.info("PORTFOLIO market gate | %s", gate_note)

        # ── Universe ──────────────────────────────────────────────────────────
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

        excluded = get_active_exclusions("portfolio")
        if excluded:
            before = len(syms)
            syms = [s for s in syms if s not in excluded]
            log.info("PORTFOLIO exclusions filter: %d -> %d symbols", before, len(syms))

        signals: list[Signal] = []

        # ── Short-circuit when gated ──────────────────────────────────────────
        if not market_bullish:
            for sym in syms:
                if sym == proxy:
                    signals.append(Signal(sym, "hold", "Skip market proxy symbol"))
                else:
                    signals.append(Signal(sym, "hold", gate_note))
            return signals

        # ── SPY reference bars: 6m return for RS calculation ─────────────────
        spy_rows = conn.execute(
            "SELECT c FROM bars_daily WHERE symbol=? AND t<=? AND c IS NOT NULL ORDER BY t ASC;",
            (proxy, asof),
        ).fetchall()
        spy_closes = [float(r["c"]) for r in spy_rows]
        spy_6m_return: float | None = None
        if len(spy_closes) >= return_days + 1:
            spy_6m_return = (spy_closes[-1] / spy_closes[-(return_days + 1)] - 1) * 100.0

        if spy_6m_return is None:
            log.warning(
                "PORTFOLIO: %s 6m return unavailable (have %d bars); RS check skipped",
                proxy, len(spy_closes),
            )

        # ── Pass 1: collect per-symbol metrics ────────────────────────────────
        # We need all 6m returns before we can compute the P75 threshold (condition c).
        sym_metrics: dict[str, dict] = {}
        sym_6m_returns: dict[str, float] = {}

        for sym in syms:
            if sym == proxy:
                continue  # proxy handled in pass 2

            bar_rows = conn.execute(
                "SELECT c FROM bars_daily WHERE symbol=? AND t<=? AND c IS NOT NULL ORDER BY t ASC;",
                (sym, asof),
            ).fetchall()
            closes = [float(r["c"]) for r in bar_rows]

            if len(closes) < lookback_min:
                sym_metrics[sym] = {"skip": True, "reason": "Not enough data"}
                continue

            sma_long_vals = _sma(closes, sma_long)
            sma_mid_vals = _sma(closes, sma_mid)
            i = len(closes) - 1

            if sma_long_vals[i] is None or sma_mid_vals[i] is None:
                sym_metrics[sym] = {"skip": True, "reason": "Indicators not available"}
                continue

            price = closes[i]
            s200 = float(sma_long_vals[i])
            s50 = float(sma_mid_vals[i])

            # 6-month return (return_days trading days)
            if i < return_days:
                sym_metrics[sym] = {"skip": True, "reason": "Insufficient history for 6m return"}
                continue
            ret_6m = (closes[i] / closes[i - return_days] - 1) * 100.0

            # 20-day extension check
            if i < ext_days:
                sym_metrics[sym] = {"skip": True, "reason": "Insufficient history for extension check"}
                continue
            ext_20d = closes[i] / closes[i - ext_days] - 1  # fraction (not %)

            sym_metrics[sym] = {
                "skip": False,
                "price": price,
                "sma200": s200,
                "sma50": s50,
                "ret_6m": ret_6m,
                "ext_20d": ext_20d,
            }
            sym_6m_returns[sym] = ret_6m

        # ── Compute P75 threshold (top top_pct% cutoff) ───────────────────────
        # Sort ascending; top 25% starts at the 75th percentile index.
        if sym_6m_returns:
            returns_sorted = sorted(sym_6m_returns.values())
            n = len(returns_sorted)
            p75_idx = int(n * (1.0 - top_pct))
            p75_threshold = returns_sorted[min(p75_idx, n - 1)]
        else:
            p75_threshold = float("inf")  # no data → no buy signals possible

        log.info(
            "PORTFOLIO universe=%s syms=%d with_6m=%d p75=%.2f spy_6m=%s",
            universe,
            len(syms),
            len(sym_6m_returns),
            p75_threshold if p75_threshold != float("inf") else float("nan"),
            f"{spy_6m_return:.2f}" if spy_6m_return is not None else "N/A",
        )

        # ── Pass 2: emit signals ──────────────────────────────────────────────
        for sym in syms:
            if sym == proxy:
                signals.append(Signal(sym, "hold", "Skip market proxy symbol"))
                continue

            m = sym_metrics.get(sym)
            if m is None or m.get("skip"):
                reason = (m or {}).get("reason", "Not enough data")
                signals.append(Signal(sym, "hold", reason))
                continue

            price = m["price"]
            s200 = m["sma200"]
            s50 = m["sma50"]
            ret_6m = m["ret_6m"]
            ext_20d = m["ext_20d"]

            # a) Long-term trend
            if price <= s200:
                signals.append(
                    Signal(sym, "hold", f"PORTFOLIO: price({price:.2f}) <= SMA{sma_long}({s200:.2f})")
                )
                continue

            # b) Medium-term trend
            if price <= s50:
                signals.append(
                    Signal(sym, "hold", f"PORTFOLIO: price({price:.2f}) <= SMA{sma_mid}({s50:.2f})")
                )
                continue

            # c) Top top_pct% of universe by 6m return
            if ret_6m < p75_threshold:
                signals.append(
                    Signal(
                        sym, "hold",
                        f"PORTFOLIO: 6m_ret({ret_6m:.2f}%) < P75({p75_threshold:.2f}%)",
                    )
                )
                continue

            # d) Relative strength vs proxy
            if spy_6m_return is not None and (ret_6m - spy_6m_return) <= 0:
                signals.append(
                    Signal(
                        sym, "hold",
                        f"PORTFOLIO: RS({ret_6m - spy_6m_return:.2f}%) not positive vs {proxy}",
                    )
                )
                continue

            # e) Not extended
            if ext_20d >= ext_max:
                signals.append(
                    Signal(
                        sym, "hold",
                        f"PORTFOLIO: extended {ext_20d * 100:.1f}% >= {ext_max * 100:.0f}%",
                    )
                )
                continue

            # All conditions pass — BUY
            # reason string starts with "PORTFOLIO:" so planner._signal_key_for()
            # maps it to signal_key="portfolio_momentum_strength".
            signals.append(
                Signal(
                    sym,
                    "buy",
                    "PORTFOLIO: momentum + trend + RS leader, not extended",
                    strength=max(0.01, ret_6m),  # planner sorts candidates by strength DESC
                )
            )

    return signals
