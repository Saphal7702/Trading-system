from __future__ import annotations

import bisect
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from ..db import connect
from .db import connect_backtest, init_backtest_db
from .sim_broker import SimBroker


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


# ---------------------------------------------------------------------------
# Signal generators (SMA crossover and MRIT)
# ---------------------------------------------------------------------------

def _sma_signals(
    symbols: list[str],
    bars_by_sym: dict[str, list[tuple[str, float]]],
    dates_by_sym: dict[str, list[str]],
    asof: str,
    *,
    fast: int = 20,
    slow: int = 50,
    lookback_min: int = 120,
) -> list[dict]:
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
            signals.append({
                "symbol": sym,
                "action": "buy",
                "reason": f"SMA{fast} crossed above SMA{slow}",
                "strength": abs(float(sma_f[i]) - float(sma_s[i])),
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
    spy_bars: list[tuple[str, float]],
    spy_dates: list[str],
    *,
    trend_sma: int = 50,
    mean_sma: int = 10,
    rsi_period: int = 2,
    rsi_max: float = 10.0,
    lookback_min: int = 120,
    market_sma: int = 200,
) -> list[dict]:
    # Market regime gate: SPY close > SPY SMA(market_sma)
    spy_closes = _closes_up_to(spy_bars, spy_dates, asof)
    if len(spy_closes) < market_sma + 5:
        return []
    spy_sma = _sma(spy_closes, market_sma)
    si = len(spy_closes) - 1
    if spy_sma[si] is None or spy_closes[si] <= float(spy_sma[si]):
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
            })
    return signals


# ---------------------------------------------------------------------------
# Exit rule checks (in-memory, no DB)
# ---------------------------------------------------------------------------

def _check_exits(
    broker: SimBroker,
    prices: dict[str, float],
    holding_days: dict[str, int],
    *,
    stop_loss_pct: float = 5.0,
    trail_activate_pct: float = 8.0,
    trail_dd_pct: float = 4.0,
    take_profit_pct: float = 15.0,
    time_stop_days: int = 30,
    time_stop_min_ret: float = 2.0,
    early_fail_days: int = 5,
    early_fail_max_ret: float = 0.0,
) -> list[tuple[str, str]]:
    """Return list of (symbol, exit_reason) for positions that should be closed."""
    exits: list[tuple[str, str]] = []
    for sym, pos in broker.positions.items():
        price = prices.get(sym)
        if price is None:
            continue
        ret = ((price - pos.entry_price) / pos.entry_price) * 100.0
        peak_gain = ((pos.peak_price - pos.entry_price) / pos.entry_price) * 100.0
        dd = ((price - pos.peak_price) / pos.peak_price) * 100.0
        days = holding_days.get(sym, 0)

        if ret <= -abs(stop_loss_pct):
            exits.append((sym, f"stop_loss ret={ret:.2f}%"))
        elif peak_gain >= trail_activate_pct and dd <= -abs(trail_dd_pct):
            exits.append((sym, f"trailing_stop peak={peak_gain:.2f}% dd={dd:.2f}%"))
        elif ret >= take_profit_pct:
            exits.append((sym, f"take_profit ret={ret:.2f}%"))
        elif days >= time_stop_days and ret < time_stop_min_ret:
            exits.append((sym, f"time_stop days={days} ret={ret:.2f}%"))
        elif days >= early_fail_days and ret <= early_fail_max_ret:
            exits.append((sym, f"early_fail days={days} ret={ret:.2f}%"))
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

    # Daily returns for Sharpe
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

    # Max drawdown
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

    # Trade stats
    closed = [t for t in trades if t.get("exit_reason") != "end_of_backtest"]
    wins = [t for t in closed if (t.get("realized_pnl") or 0.0) > 0]
    win_rate = len(wins) / len(closed) * 100.0 if closed else 0.0
    avg_ret = sum(t.get("return_pct") or 0.0 for t in closed) / len(closed) if closed else 0.0

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
                 max_positions, per_position_notional, summary_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (strategy, start, end, universe, initial_capital,
             max_positions, per_position_notional, json.dumps(summary)),
        )
        run_id = cur.lastrowid

        conn.executemany(
            """
            INSERT INTO backtest_trades
                (run_id, symbol, entry_date, exit_date, entry_price, exit_price,
                 qty, realized_pnl, return_pct, exit_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (run_id, t["symbol"], t["entry_date"], t.get("exit_date"),
                 t["entry_price"], t.get("exit_price"), t["qty"],
                 t.get("realized_pnl"), t.get("return_pct"), t.get("exit_reason"))
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
    strategy: str,
    start: str,
    end: str,
    universe: str = "sp500",
    initial_capital: float = 100_000.0,
    max_positions: int = 20,
    per_position_notional: float | None = None,
    backtest_db_path: str | None = None,
    fast: int = 20,
    slow: int = 50,
) -> dict:
    """
    Run a backtest for the given strategy over [start, end].

    Fills model: signals on day T are filled at day T+1's close.
    Universe symbols are read from universe_membership in trading.sqlite.
    Historical bars are read from bars_daily in trading.sqlite.

    Returns a summary dict with performance metrics and run_id.
    """
    if strategy not in ("sma", "mrit"):
        raise ValueError(f"Unknown strategy: {strategy!r}. Use 'sma' or 'mrit'.")

    if per_position_notional is None:
        per_position_notional = initial_capital / max_positions

    init_backtest_db(backtest_db_path)

    # -----------------------------------------------------------------------
    # Load universe from trading.sqlite, bars from backtest.sqlite (yfinance)
    # -----------------------------------------------------------------------
    lookback_start = (
        datetime.strptime(start, "%Y-%m-%d") - timedelta(days=400)
    ).strftime("%Y-%m-%d")

    from .db import connect_backtest
    from ..db import connect as live_connect
 
    # Universe membership (which symbols to trade) — from live trading DB
    with live_connect() as live_conn:
        sym_rows = live_conn.execute(
            "SELECT DISTINCT symbol FROM universe_membership WHERE universe=?",
            (universe,),
        ).fetchall()
    symbols = [r["symbol"] for r in sym_rows]
    if not symbols:
        raise RuntimeError(
            f"No symbols in universe '{universe}'. "
            "Run: trading load-universe --file data/sp500.csv"
        )
 
    # All price data from backtest DB (yfinance, split-adjusted, isolated)
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
            SELECT symbol, t, c
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
 
    bars_by_sym: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for r in bar_rows:
        bars_by_sym[r["symbol"]].append((r["t"], float(r["c"])))
 
    dates_by_sym: dict[str, list[str]] = {
        sym: [b[0] for b in bars] for sym, bars in bars_by_sym.items()
    }
 
    spy_bars = [(r["t"], float(r["c"])) for r in spy_rows]
    spy_dates = [b[0] for b in spy_bars]

    # -----------------------------------------------------------------------
    # Simulation loop
    # -----------------------------------------------------------------------
    broker = SimBroker(cash=initial_capital)
    holding_days: dict[str, int] = {}
    pending_orders: list[dict] = []
    trades: list[dict] = []
    equity_curve: list[dict] = []

    for day in trading_days:
        # Build current price map (last available close on or before this day)
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
                pos = broker.positions[sym]
                qty, entry_price, pnl = broker.fill_sell(sym, price)
                ret_pct = ((price - entry_price) / entry_price) * 100.0
                trades.append({
                    "symbol": sym,
                    "entry_date": pos.entry_date,
                    "exit_date": day,
                    "entry_price": entry_price,
                    "exit_price": price,
                    "qty": qty,
                    "realized_pnl": pnl,
                    "return_pct": ret_pct,
                    "exit_reason": order.get("reason", "signal"),
                })
                holding_days.pop(sym, None)
                filled_sells.add(sym)

            elif order["action"] == "buy" and sym not in broker.positions and sym not in filled_buys:
                qty = broker.fill_buy(sym, price, per_position_notional, day)
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
        exits = _check_exits(broker, prices, holding_days)
        exit_syms = {sym for sym, _ in exits}
        for sym, reason in exits:
            pending_orders.append({"action": "sell", "symbol": sym, "reason": reason})

        # 4. Generate signals based on today's close data
        if strategy == "sma":
            raw_signals = _sma_signals(symbols, bars_by_sym, dates_by_sym, day, fast=fast, slow=slow)
        else:
            raw_signals = _mrit_signals(symbols, bars_by_sym, dates_by_sym, day, spy_bars, spy_dates)

        # Queue sell signals for positions we hold (that exit advisor hasn't already caught)
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
        ]
        buy_candidates.sort(key=lambda s: float(s.get("strength") or 0.0), reverse=True)

        notional_queued = 0.0
        for sig in buy_candidates[:slots]:
            if broker.cash - notional_queued < per_position_notional * 0.5:
                break
            pending_orders.append({"action": "buy", "symbol": sig["symbol"], "reason": sig["reason"]})
            pending_buy_syms.add(sig["symbol"])
            notional_queued += per_position_notional

        # 6. Record daily equity snapshot (after fills, before queued orders)
        equity_curve.append({"date": day, "equity": broker.equity(prices), "cash": broker.cash})

    # -----------------------------------------------------------------------
    # Close remaining open positions at final-day close (marked as open)
    # -----------------------------------------------------------------------
    last_day = trading_days[-1]
    for sym in list(broker.positions.keys()):
        pos = broker.positions[sym]
        price = prices.get(sym, pos.entry_price)
        qty, entry_price, pnl = broker.fill_sell(sym, price)
        ret_pct = ((price - entry_price) / entry_price) * 100.0
        trades.append({
            "symbol": sym,
            "entry_date": pos.entry_date,
            "exit_date": last_day,
            "entry_price": entry_price,
            "exit_price": price,
            "qty": qty,
            "realized_pnl": pnl,
            "return_pct": ret_pct,
            "exit_reason": "end_of_backtest",
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
        trades=trades,
        equity_curve=equity_curve,
        summary=summary,
    )

    summary["run_id"] = run_id
    summary["trades_detail"] = trades
    return summary
