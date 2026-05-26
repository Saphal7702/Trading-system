from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import os
import math

from .db import connect
from .utils import env_float_opt, env_int_opt, env_bool_opt, env_float, env_bool

def _format_policy_exit_annotation(
    policy: Any | None,
    *,
    entry_key: str | None,
    exit_key: str | None,
    max_best: int = 3,
) -> str:
    """Return a compact policy note for an exit decision (advisory only)."""
    if not policy or not entry_key or not exit_key:
        return ""

    try:
        pol_asof = getattr(policy, "asof", None)

        # Exit-level recommendation (may be missing if exit_key isn't in policy.exit_policy)
        rec = None
        score = None
        tr = None
        er = policy.exit_rec(exit_key) if hasattr(policy, "exit_rec") else None
        if isinstance(er, dict):
            rec = er.get("rec")
            score = er.get("score")
            tr = er.get("trades")

        # Best exits for this entry (combo intelligence)
        # Filter to only "positive" exits (score > 0 if present, else avg_ret_pct > 0).
        best = policy.best_exits(entry_key) if hasattr(policy, "best_exits") else []
        best_pos: list[tuple[str, float | None]] = []  # (exit_key, score)

        if isinstance(best, list):
            for x in best:
                if not isinstance(x, dict):
                    continue
                k = x.get("exit_key")
                if not k:
                    continue
                k = str(k)

                sc = x.get("score")
                sc_f = None
                try:
                    sc_f = float(sc) if sc is not None else None
                except Exception:
                    sc_f = None

                # positivity rule
                pos = False
                if sc_f is not None:
                    pos = sc_f > 0
                else:
                    # fallback if score missing
                    ar = x.get("avg_ret_pct")
                    try:
                        pos = float(ar) > 0
                    except Exception:
                        pos = False

                if pos:
                    best_pos.append((k, sc_f))

        # sort best positives by score (None last)
        best_pos.sort(key=lambda t: (t[1] is None, -(t[1] or 0.0)))
        best_keys = [k for (k, _) in best_pos][:max_best]

        # rank of current exit among positive best exits
        rank = None
        for idx, k in enumerate(best_keys):
            if k == str(exit_key):
                rank = idx + 1
                break

        parts: list[str] = []
        if pol_asof:
            parts.append(f"pol_asof={pol_asof}")

        if rec is not None:
            parts.append(f"policy_exit={rec}")
        else:
            # exit_key not present in policy.exit_policy
            parts.append("policy_exit=unscored")

        if score is not None:
            try:
                parts.append(f"score={float(score):+.2f}")
            except Exception:
                pass
        if tr is not None:
            try:
                parts.append(f"tr={int(tr)}")
            except Exception:
                pass

        if best_keys:
            parts.append(f"best=[{','.join(best_keys)}]")
            if rank is not None:
                parts.append(f"best_rank={rank}/{len(best_keys)}")
        else:
            parts.append("best=[none]")

        return " ".join(parts)
    except Exception:
        return ""


@dataclass(frozen=True)
class ExitRuleConfig:
    # Hard stop loss
    stop_loss_pct: float = 5.0  # recommend SELL if ret <= -5%

    # Early failure stop
    early_fail_days: int = 5
    early_fail_max_ret_pct: float = 0.0  # if ret <= 0%

    # Trailing stop
    trail_activate_peak_gain_pct: float = 8.0   # activate after peak gain >= +8%
    trail_drawdown_pct: float = 4.0             # sell if drawdown <= -4% from peak

    # Take profit (soft)
    take_profit_pct: float = 15.0

    # Time stop
    time_stop_days: int = 30
    time_stop_min_ret_pct: float = 2.0

    # Trend reversal (SMA)
    enable_sma_reversal: bool = True

    break_even_peak_gain_pct: float = 10.0
    break_even_floor_ret_pct: float = 2.0

    # Portfolio-level dollar stop (PORTFOLIO profile only; 0 = disabled).
    # Fires when abs(unrealized_pnl) / portfolio_equity * 100 >= this value.
    # Read from TRADING_EXIT_PROFILE_PORTFOLIO_PORTFOLIO_STOP_PCT.
    portfolio_stop_dollar_pct: float = 0.0


_ENV_PREFIX = "TRADING_EXIT_"

def _env_get(name: str) -> str | None:
    return os.getenv(_ENV_PREFIX + name)

def _parse_bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "t", "yes", "y", "on")

def exit_rule_config_from_env(base: ExitRuleConfig | None = None) -> ExitRuleConfig:
    """Return an ExitRuleConfig using env vars as defaults (falls back to dataclass defaults)."""
    base = base or ExitRuleConfig()
    # floats
    stop_loss_pct = float(_env_get("STOP_LOSS_PCT")) if _env_get("STOP_LOSS_PCT") else base.stop_loss_pct
    early_fail_max_ret_pct = float(_env_get("EARLY_FAIL_MAX_RET_PCT")) if _env_get("EARLY_FAIL_MAX_RET_PCT") else base.early_fail_max_ret_pct
    trail_activate_peak_gain_pct = float(_env_get("TRAIL_PEAK_PCT")) if _env_get("TRAIL_PEAK_PCT") else base.trail_activate_peak_gain_pct
    trail_drawdown_pct = float(_env_get("TRAIL_DD_PCT")) if _env_get("TRAIL_DD_PCT") else base.trail_drawdown_pct
    break_even_peak_gain_pct = float(_env_get("BREAKEVEN_PEAK_PCT")) if _env_get("BREAKEVEN_PEAK_PCT") else base.break_even_peak_gain_pct
    break_even_floor_ret_pct = float(_env_get("BREAKEVEN_FLOOR_PCT")) if _env_get("BREAKEVEN_FLOOR_PCT") else base.break_even_floor_ret_pct
    take_profit_pct = float(_env_get("TAKE_PROFIT_PCT")) if _env_get("TAKE_PROFIT_PCT") else base.take_profit_pct
    time_stop_min_ret_pct = float(_env_get("TIME_STOP_MIN_RET_PCT")) if _env_get("TIME_STOP_MIN_RET_PCT") else base.time_stop_min_ret_pct

    # ints
    early_fail_days = int(_env_get("EARLY_FAIL_DAYS")) if _env_get("EARLY_FAIL_DAYS") else base.early_fail_days
    time_stop_days = int(_env_get("TIME_STOP_DAYS")) if _env_get("TIME_STOP_DAYS") else base.time_stop_days

    # bool
    if _env_get("ENABLE_SMA_REVERSAL") is None:
        enable_sma_reversal = base.enable_sma_reversal
    else:
        enable_sma_reversal = _parse_bool(_env_get("ENABLE_SMA_REVERSAL") or "0")

    return ExitRuleConfig(
        stop_loss_pct=stop_loss_pct,
        early_fail_days=early_fail_days,
        early_fail_max_ret_pct=early_fail_max_ret_pct,
        trail_activate_peak_gain_pct=trail_activate_peak_gain_pct,
        trail_drawdown_pct=trail_drawdown_pct,
        break_even_peak_gain_pct=break_even_peak_gain_pct,
        break_even_floor_ret_pct=break_even_floor_ret_pct,
        take_profit_pct=take_profit_pct,
        time_stop_days=time_stop_days,
        time_stop_min_ret_pct=time_stop_min_ret_pct,
        enable_sma_reversal=enable_sma_reversal,
    )

def _resolve_asof_date(conn) -> str:
    r = conn.execute("SELECT MAX(t) AS asof_date FROM bars_daily;").fetchone()
    return r["asof_date"] if r and r["asof_date"] else ""


def _fetch_open_position_metrics(conn, asof: str | None) -> list[dict[str, Any]]:
    if not asof:
        asof = _resolve_asof_date(conn)

    sql = """
    WITH
    asof AS (
      SELECT ? AS asof_date
    ),

    -- Broker-synced truth: what is actually open right now
    open_positions AS (
      SELECT UPPER(symbol) AS symbol, qty
      FROM positions
      WHERE qty > 0.000001
    ),

    open_pos_core AS (
      SELECT
        UPPER(pl.symbol) AS symbol,
        SUM(pl.qty_open) AS qty_open,
        SUM(pl.qty_open * pl.entry_price) AS cost_basis,
        MIN(pl.entry_filled_at) AS first_entry_at
      FROM position_lots pl
      JOIN open_positions op ON op.symbol = UPPER(pl.symbol)
      WHERE pl.qty_open > 0
      GROUP BY UPPER(pl.symbol)
    ),

    entry_sig AS (
      SELECT
        UPPER(pl.symbol) AS symbol,
        MAX(i.signal_key) AS entry_signal_key
      FROM position_lots pl
      JOIN open_positions op ON op.symbol = UPPER(pl.symbol)
      LEFT JOIN executions e ON e.id = pl.entry_execution_id
      LEFT JOIN orders o     ON o.id = e.order_id
      LEFT JOIN intents i    ON i.id = o.intent_id
      WHERE pl.qty_open > 0
      GROUP BY UPPER(pl.symbol)
    ),

    open_pos AS (
      SELECT
        c.symbol,
        c.qty_open,
        c.cost_basis,
        c.first_entry_at,
        s.entry_signal_key
      FROM open_pos_core c
      LEFT JOIN entry_sig s ON s.symbol = c.symbol
    ),

    last_close AS (
      SELECT UPPER(b.symbol) AS symbol, b.c AS last_close
      FROM bars_daily b
      JOIN (
        SELECT UPPER(symbol) AS symbol, MAX(t) AS tmax
        FROM bars_daily
        WHERE t <= (SELECT asof_date FROM asof)
        GROUP BY UPPER(symbol)
      ) mx
        ON mx.symbol = UPPER(b.symbol) AND mx.tmax = b.t
    ),

    peak_since_entry AS (
      SELECT
        op.symbol,
        MAX(b.c) AS peak_close
      FROM open_pos op
      JOIN bars_daily b
        ON UPPER(b.symbol) = op.symbol
       AND b.t >= date(op.first_entry_at)
       AND b.t <= (SELECT asof_date FROM asof)
      GROUP BY op.symbol
    )

    SELECT
      op.symbol,
      op.qty_open AS qty_open,
      op.cost_basis AS cost_basis,
      (op.cost_basis / NULLIF(op.qty_open,0)) AS vwap_entry,
      lc.last_close AS last_close,
      (op.qty_open * (lc.last_close - (op.cost_basis / NULLIF(op.qty_open,0)))) AS unrealized_pnl,
      (100.0 * (op.qty_open * (lc.last_close - (op.cost_basis / NULLIF(op.qty_open,0)))) / NULLIF(op.cost_basis,0)) AS unrealized_ret_pct,
      CAST(julianday((SELECT asof_date FROM asof)) - julianday(date(op.first_entry_at)) AS INT) AS holding_days,
      ps.peak_close AS peak_close,
      (100.0 * (lc.last_close - ps.peak_close) / NULLIF(ps.peak_close,0)) AS drawdown_from_peak_pct,
      (100.0 * (ps.peak_close - (op.cost_basis / NULLIF(op.qty_open,0))) / NULLIF((op.cost_basis / NULLIF(op.qty_open,0)),0)) AS peak_gain_pct,
      op.first_entry_at AS first_entry_at,
      op.entry_signal_key AS entry_signal_key
    FROM open_pos op
    JOIN last_close lc ON lc.symbol = op.symbol
    JOIN peak_since_entry ps ON ps.symbol = op.symbol
    ORDER BY unrealized_ret_pct ASC;
    """

    rows = conn.execute(sql, (asof,)).fetchall()
    return [dict(r) for r in rows]

def _fetch_sma(conn, symbol: str, asof: str | None) -> tuple[float | None, float | None]:
    """
    Compute SMA20 and SMA50 from bars_daily closes up to asof date.
    """
    if not asof:
        asof = _resolve_asof_date(conn)

    closes = conn.execute(
        """
        SELECT c
        FROM bars_daily
        WHERE symbol = ?
          AND t <= ?
          AND c IS NOT NULL
        ORDER BY t DESC
        LIMIT 60;
        """,
        (symbol, asof),
    ).fetchall()

    vals = [float(r["c"]) for r in closes if r["c"] is not None]
    if len(vals) < 20:
        return None, None

    sma20 = sum(vals[:20]) / 20.0
    sma50 = (sum(vals[:50]) / 50.0) if len(vals) >= 50 else None
    return sma20, sma50


def _fetch_atr(
    conn,
    *,
    symbol: str,
    asof: str | None,
    period: int = 14,
    lookback_pad: int = 5,
) -> float | None:
    """Compute ATR(period) from daily bars up to `asof`.

    True Range (TR) per day:
      TR = max(H-L, |H-prevC|, |L-prevC|)

    ATR returned here is SMA(TR, period) over the most recent `period` TR values.
    This is stable and sufficient for exit bands (TP/SL) without adding stored indicators.
    """
    if not asof:
        asof = _resolve_asof_date(conn)

    # Need at least period+1 closes to produce `period` TR observations.
    n = max(period + 1 + lookback_pad, period + 2)
    rows = conn.execute(
        """
        SELECT t, h, l, c
        FROM bars_daily
        WHERE symbol=? AND t<=?
        ORDER BY t DESC
        LIMIT ?;
        """,
        (symbol, asof, n),
    ).fetchall()

    if not rows or len(rows) < period + 1:
        return None

    rows = list(reversed(rows))  # chronological

    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    for r in rows:
        if r["h"] is None or r["l"] is None or r["c"] is None:
            continue
        highs.append(float(r["h"]))
        lows.append(float(r["l"]))
        closes.append(float(r["c"]))

    if len(closes) < period + 1:
        return None

    trs: list[float] = []
    for i in range(1, len(closes)):
        h = highs[i]
        l = lows[i]
        pc = closes[i - 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        if tr <= 0 or math.isnan(tr):
            continue
        trs.append(float(tr))

    if len(trs) < period:
        return None

    window = trs[-period:]
    atr = sum(window) / float(period)
    if atr <= 0 or math.isnan(atr):
        return None
    return float(atr)


def _rule_priority(signal_key: str) -> int:
    """
    Higher means more urgent.
    """
    if signal_key.startswith("exit_stop_loss"):
        return 100
    if signal_key.startswith("exit_portfolio_stop"):
        return 95
    if signal_key.startswith("exit_trailing"):
        return 90
    if signal_key.startswith("exit_break_even_floor"):
        return 85
    if signal_key.startswith("exit_sma_reversal"):
        return 80
    if signal_key.startswith("exit_early_failure"):
        return 70
    if signal_key.startswith("exit_time_stop"):
        return 60
    if signal_key.startswith("exit_take_profit"):
        return 50
    if signal_key.startswith("watch_"):
        return 10
    return 0


def _exit_profile_name(entry_signal_key: str) -> str:
    """Map entry signal to an exit profile name."""
    k = (entry_signal_key or "").lower()
    if k.startswith("mrit_"):
        return "MRIT"
    if k.startswith("portfolio_"):
        return "PORTFOLIO"
    return "DEFAULT"


def _apply_profile_overrides(base: ExitRuleConfig, profile: str) -> ExitRuleConfig:
    """Return ExitRuleConfig based on base, overridden by per-profile env vars.

    Env naming convention:
      TRADING_EXIT_PROFILE_{PROFILE}_STOP_LOSS_PCT
      TRADING_EXIT_PROFILE_{PROFILE}_TAKE_PROFIT_PCT
      TRADING_EXIT_PROFILE_{PROFILE}_EARLY_FAIL_DAYS
      ...
    """
    if profile == "DEFAULT":
        return base

    pfx = f"TRADING_EXIT_PROFILE_{profile.upper()}_"

    stop_loss = env_float_opt(pfx + "STOP_LOSS_PCT")
    early_days = env_int_opt(pfx + "EARLY_FAIL_DAYS")
    early_max = env_float_opt(pfx + "EARLY_FAIL_MAX_RET_PCT")

    trail_peak = env_float_opt(pfx + "TRAIL_PEAK_PCT")
    trail_dd = env_float_opt(pfx + "TRAIL_DD_PCT")

    be_peak = env_float_opt(pfx + "BREAKEVEN_PEAK_PCT")
    be_floor = env_float_opt(pfx + "BREAKEVEN_FLOOR_PCT")

    take_profit = env_float_opt(pfx + "TAKE_PROFIT_PCT")

    time_days = env_int_opt(pfx + "TIME_STOP_DAYS")
    time_min = env_float_opt(pfx + "TIME_STOP_MIN_RET_PCT")

    enable_sma = env_bool_opt(pfx + "ENABLE_SMA_REVERSAL")
    port_stop = env_float_opt(pfx + "PORTFOLIO_STOP_PCT")

    return ExitRuleConfig(
        stop_loss_pct=stop_loss if stop_loss is not None else base.stop_loss_pct,
        early_fail_days=early_days if early_days is not None else base.early_fail_days,
        early_fail_max_ret_pct=early_max if early_max is not None else base.early_fail_max_ret_pct,
        trail_activate_peak_gain_pct=trail_peak if trail_peak is not None else base.trail_activate_peak_gain_pct,
        trail_drawdown_pct=trail_dd if trail_dd is not None else base.trail_drawdown_pct,
        break_even_peak_gain_pct=be_peak if be_peak is not None else base.break_even_peak_gain_pct,
        break_even_floor_ret_pct=be_floor if be_floor is not None else base.break_even_floor_ret_pct,
        take_profit_pct=take_profit if take_profit is not None else base.take_profit_pct,
        time_stop_days=time_days if time_days is not None else base.time_stop_days,
        time_stop_min_ret_pct=time_min if time_min is not None else base.time_stop_min_ret_pct,
        enable_sma_reversal=enable_sma if enable_sma is not None else base.enable_sma_reversal,
        portfolio_stop_dollar_pct=port_stop if port_stop is not None else base.portfolio_stop_dollar_pct,
    )


def _fetch_portfolio_equity(conn) -> float | None:
    """Fetch current portfolio equity from the broker_accounts cache (latest row)."""
    try:
        r = conn.execute(
            "SELECT equity FROM broker_accounts ORDER BY last_synced_at DESC LIMIT 1;"
        ).fetchone()
        if not r or r["equity"] is None:
            return None
        return float(r["equity"])
    except Exception:
        return None


def evaluate_exit_advice(
    *,
    asof: str | None = None,
    cfg: ExitRuleConfig | None = None,
) -> list[dict[str, Any]]:
    """
    Returns a ranked list of exit advice rows:
      symbol, ret_pct, holding_days, peak_gain_pct, drawdown_pct,
      entry_signal_key,
      exit_signal_key, action (SELL/WATCH/HOLD), priority, rationale,
      sma20, sma50
    """
    cfg = cfg or exit_rule_config_from_env()

    with connect() as conn:
        asof_eff = asof or _resolve_asof_date(conn)
        metrics = _fetch_open_position_metrics(conn, asof)

        # Lazy-loaded portfolio equity (fetched once if any PORTFOLIO position exists).
        _portfolio_equity: float | None = None
        _portfolio_equity_fetched: bool = False

        advice: list[dict[str, Any]] = []
        for m in metrics:
            symbol = str(m["symbol"]).upper()
            ret = float(m["unrealized_ret_pct"]) if m["unrealized_ret_pct"] is not None else 0.0
            days = int(m["holding_days"]) if m["holding_days"] is not None else 0
            peak_gain = float(m["peak_gain_pct"]) if m["peak_gain_pct"] is not None else 0.0
            dd = float(m["drawdown_from_peak_pct"]) if m["drawdown_from_peak_pct"] is not None else 0.0
            entry_signal_key = str(m.get("entry_signal_key") or "unknown")

            profile = _exit_profile_name(entry_signal_key)
            cfg_eff = _apply_profile_overrides(cfg, profile)

            # ATR (primarily for MRIT): profile-aware env controls
            #   TRADING_EXIT_PROFILE_MRIT_ATR_PERIOD=14
            #   TRADING_EXIT_PROFILE_MRIT_TP_ATR_MULT=1.0
            #   TRADING_EXIT_PROFILE_MRIT_SL_ATR_MULT=1.5
            atr_period = env_int_opt(f"TRADING_EXIT_PROFILE_{profile.upper()}_ATR_PERIOD") or 14
            tp_atr_mult = env_float_opt(f"TRADING_EXIT_PROFILE_{profile.upper()}_TP_ATR_MULT")
            sl_atr_mult = env_float_opt(f"TRADING_EXIT_PROFILE_{profile.upper()}_SL_ATR_MULT")

            last_close = float(m["last_close"]) if m.get("last_close") is not None else None
            vwap_entry = float(m["vwap_entry"]) if m.get("vwap_entry") is not None else None

            sma20 = sma50 = None
            if cfg_eff.enable_sma_reversal:
                sma20, sma50 = _fetch_sma(conn, symbol, asof)

            # Decide rule
            exit_signal = "hold_trend_intact"
            action = "HOLD"
            rationale = "No exit rule triggered."

            # 0) ATR-based exits (only if enabled + we can compute ATR)
            atr = None
            if tp_atr_mult is not None or sl_atr_mult is not None:
                # Currently intended for MRIT, but profile-aware by env. If you set
                # TP_ATR_MULT/SL_ATR_MULT for other profiles, it will work too.
                if last_close is not None and vwap_entry is not None:
                    atr = _fetch_atr(conn, symbol=symbol, asof=asof_eff, period=int(atr_period))

            if atr is not None and atr > 0 and last_close is not None and vwap_entry is not None:
                tp_px = (vwap_entry + float(tp_atr_mult) * atr) if tp_atr_mult is not None else None
                sl_px = (vwap_entry - float(sl_atr_mult) * atr) if sl_atr_mult is not None else None

                # Stop first
                if sl_px is not None and last_close <= sl_px:
                    exit_signal = f"exit_stop_loss_atr{float(sl_atr_mult):g}"
                    action = "SELL"
                    rationale = (
                        f"[{profile}][ATR] Stop: last={last_close:.2f} <= sl={sl_px:.2f} "
                        f"(entry={vwap_entry:.2f} atr={atr:.2f} period={atr_period})"
                    )

                # Take profit second
                elif tp_px is not None and last_close >= tp_px:
                    exit_signal = f"exit_take_profit_atr{float(tp_atr_mult):g}"
                    action = "SELL"
                    rationale = (
                        f"[{profile}][ATR] Take: last={last_close:.2f} >= tp={tp_px:.2f} "
                        f"(entry={vwap_entry:.2f} atr={atr:.2f} period={atr_period})"
                    )

            # 0.5) Portfolio-level dollar stop (PORTFOLIO profile only)
            # Fires when the position's unrealized dollar loss exceeds
            # portfolio_stop_dollar_pct % of total portfolio equity.
            if (
                action == "HOLD"
                and profile == "PORTFOLIO"
                and cfg_eff.portfolio_stop_dollar_pct > 0
            ):
                pnl = float(m["unrealized_pnl"]) if m.get("unrealized_pnl") is not None else None
                if pnl is not None and pnl < 0:
                    if not _portfolio_equity_fetched:
                        _portfolio_equity = _fetch_portfolio_equity(conn)
                        _portfolio_equity_fetched = True
                    if _portfolio_equity is not None and _portfolio_equity > 0:
                        loss_pct_of_port = abs(pnl) / _portfolio_equity * 100.0
                        if loss_pct_of_port >= cfg_eff.portfolio_stop_dollar_pct:
                            exit_signal = "exit_portfolio_stop_dollar"
                            action = "SELL"
                            rationale = (
                                f"Portfolio-level stop: loss=${abs(pnl):.2f} = "
                                f"{loss_pct_of_port:.2f}% of equity(${_portfolio_equity:.2f}) "
                                f">= {cfg_eff.portfolio_stop_dollar_pct:.2f}%"
                            )

            # 1) Hard stop loss
            if action == "HOLD" and ret <= -abs(cfg_eff.stop_loss_pct):
                exit_signal = f"exit_stop_loss_{int(cfg_eff.stop_loss_pct)}pct"
                action = "SELL"
                rationale = f"Hard stop: ret={ret:.2f}% <= -{cfg_eff.stop_loss_pct:.2f}%"

            # 2) Trailing stop (only after meaningful peak gain)
            elif action == "HOLD" and peak_gain >= cfg_eff.trail_activate_peak_gain_pct and dd <= -abs(cfg_eff.trail_drawdown_pct):
                exit_signal = f"exit_trailing_dd{int(cfg_eff.trail_drawdown_pct)}_peak{int(cfg_eff.trail_activate_peak_gain_pct)}"
                action = "SELL"
                rationale = f"Trailing stop: peak_gain={peak_gain:.2f}% and drawdown={dd:.2f}%"

            # 3) Break-even floor
            elif action == "HOLD" and peak_gain >= cfg_eff.break_even_peak_gain_pct and ret < cfg_eff.break_even_floor_ret_pct:
                exit_signal = f"exit_break_even_floor_{int(cfg_eff.break_even_peak_gain_pct)}pct_peak"
                action = "SELL"
                rationale = f"Break-even floor: peak_gain={peak_gain:.2f}% >= {cfg_eff.break_even_peak_gain_pct:.2f}% and ret={ret:.2f}% < {cfg_eff.break_even_floor_ret_pct:.2f}%"

            # 4) SMA reversal
            elif action == "HOLD" and cfg_eff.enable_sma_reversal and sma20 is not None and sma50 is not None and sma20 < sma50:
                exit_signal = "exit_sma_reversal_sma20_below_sma50"
                action = "SELL"
                rationale = f"Trend reversal: SMA20={sma20:.4f} < SMA50={sma50:.4f}"

            # 5) Early failure stop
            elif action == "HOLD" and days >= cfg_eff.early_fail_days and ret <= cfg_eff.early_fail_max_ret_pct:
                exit_signal = f"exit_early_failure_{cfg_eff.early_fail_days}d"
                action = "SELL"
                rationale = f"Early failure: days={days} ret={ret:.2f}% <= {cfg_eff.early_fail_max_ret_pct:.2f}%"

            # 6) Time stop
            elif action == "HOLD" and days >= cfg_eff.time_stop_days and ret < cfg_eff.time_stop_min_ret_pct:
                exit_signal = f"exit_time_stop_{cfg_eff.time_stop_days}d"
                action = "SELL"
                rationale = f"Time stop: days={days} ret={ret:.2f}% < {cfg_eff.time_stop_min_ret_pct:.2f}%"

            # 7) Take profit (soft)
            elif action == "HOLD" and ret >= cfg_eff.take_profit_pct:
                exit_signal = f"exit_take_profit_{int(cfg_eff.take_profit_pct)}pct"
                action = "SELL"
                rationale = f"Take profit: ret={ret:.2f}% >= {cfg_eff.take_profit_pct:.2f}%"

            # Optional WATCH: early negative before early_fail_days
            elif action == "HOLD" and days < cfg_eff.early_fail_days and ret < 0:
                exit_signal = "watch_early_weakness"
                action = "WATCH"
                rationale = f"Early weakness: days={days} ret={ret:.2f}% (monitor until day {cfg_eff.early_fail_days})"

            # Prefix rationale with profile tag unless already included.
            if profile != "DEFAULT" and not rationale.startswith(f"[{profile}]"):
                rationale = f"[{profile}] {rationale}"

            priority = _rule_priority(exit_signal)

            advice.append(
                {
                    "symbol": symbol,
                    "entry_signal_key": entry_signal_key,
                    "qty_open": float(m["qty_open"]) if m["qty_open"] is not None else None,
                    "cost_basis": float(m["cost_basis"]) if m["cost_basis"] is not None else None,
                    "vwap_entry": float(m["vwap_entry"]) if m["vwap_entry"] is not None else None,
                    "last_close": float(m["last_close"]) if m["last_close"] is not None else None,
                    "unrealized_pnl": float(m["unrealized_pnl"]) if m["unrealized_pnl"] is not None else None,
                    "unrealized_ret_pct": ret,
                    "holding_days": days,
                    "peak_close": float(m["peak_close"]) if m["peak_close"] is not None else None,
                    "drawdown_from_peak_pct": dd,
                    "peak_gain_pct": peak_gain,
                    "exit_signal_key": exit_signal,
                    "action": action,
                    "priority": priority,
                    "rationale": rationale,
                    "sma20": sma20,
                    "sma50": sma50,
                    "exit_profile": profile,
                }
            )

    # Sort: SELL first, then priority desc, then worst return asc (risk first)
    def _sort_key(r: dict[str, Any]) -> tuple[int, int, float]:
        sell_rank = 0 if r["action"] == "SELL" else (1 if r["action"] == "WATCH" else 2)
        return (sell_rank, -int(r["priority"]), float(r["unrealized_ret_pct"]))

    advice.sort(key=_sort_key)
    return advice


def _pick_run_id_for_asof(conn, asof: str | None) -> int | None:
    """
    Choose a run_id to attach emitted intents to.
    Preference:
      - most recent run where runs.asof_date == asof (if provided)
      - else most recent run (max id)
    """
    if asof:
        r = conn.execute(
            """
            SELECT id
            FROM runs
            WHERE asof_date = ?
            ORDER BY id DESC
            LIMIT 1;
            """,
            (asof,),
        ).fetchone()
        if r and r["id"] is not None:
            return int(r["id"])

    r2 = conn.execute("SELECT MAX(id) AS id FROM runs;").fetchone()
    return int(r2["id"]) if r2 and r2["id"] is not None else None

def emit_sell_intents(
    *,
    asof: str | None = None,
    cfg: ExitRuleConfig | None = None,
    dry_run: bool = False,
    policy: Any | None = None,
) -> dict[str, Any]:
    """
    Emit SELL intents derived from evaluate_exit_advice(), with an optional crash-guard
    that temporarily halts SELL intent emission on major down days.

    Crash guard inputs:
      - SPY day return (best-effort; may be None intraday if you don't have today's close)
      - Portfolio day return (preferred): broker equity vs broker last_equity (yesterday close)
        fallback: equity snapshots table (requires EOD snapshots)
        fallback: positions-only MTM close-to-close using positions + bars_daily

    Notes on your current issue:
      - Portfolio was "wrong" because equity snapshots were taken intraday (not EOD), so the
        prev-day baseline wasn't yesterday close. Using broker.last_equity fixes that.
      - SPY can become NaN if today's bar close is NaN/None. We treat NaN as missing.
    """
    import os
    import logging
    from .broker.factory import make_broker
    from .market_calendar import today_ny_str

    log = logging.getLogger("trading")

    def _prev_trading_day_from_bars(conn, symbol: str, day: str) -> str | None:
        r = conn.execute(
            """
            SELECT MAX(t) AS t
            FROM bars_daily
            WHERE UPPER(symbol)=UPPER(?)
              AND t < ?
            """,
            (symbol, day),
        ).fetchone()
        return str(r["t"]) if r and r["t"] else None

    def _close_on(conn, symbol: str, day: str) -> float | None:
        r = conn.execute(
            """
            SELECT c
            FROM bars_daily
            WHERE UPPER(symbol)=UPPER(?)
              AND t = ?
            LIMIT 1;
            """,
            (symbol, day),
        ).fetchone()
        if not r or r["c"] is None:
            return None
        try:
            return float(r["c"])
        except Exception:
            return None

    def _spy_day_return_pct(conn, day: str, *, broker=None) -> float | None:
        """
        Intraday-safe SPY day return using Alpaca market data when possible.

        Priority:
        1) Use Alpaca latest price + previous close from Alpaca snapshot/bar
        2) Fallback to previous close from bars_daily + Alpaca latest price
        3) Fallback to bars_daily close-to-close if today's close exists
        """
        import math

        def _sf(x) -> float | None:
            try:
                if x is None:
                    return None
                f = float(x)
                if not math.isfinite(f) or f <= 0:
                    return None
                return f
            except Exception:
                return None

        # --- 1) Try direct broker/alpaca methods first ---
        if broker is not None:
            # If your broker wrapper exposes a dedicated helper, use it first.
            for meth in ("get_spy_day_return_pct",):
                fn = getattr(broker, meth, None)
                if callable(fn):
                    try:
                        v = _sf(fn())
                        if v is not None:
                            return v
                    except Exception:
                        pass

            # Try generic latest/prev-close helpers if your wrapper has them
            last_px = None
            prev_close = None

            for meth in ("get_latest_price", "get_last_price", "last_price", "get_latest_trade_price", "get_last_trade_price"):
                fn = getattr(broker, meth, None)
                if callable(fn):
                    try:
                        last_px = _sf(fn("SPY"))
                        if last_px is not None:
                            break
                    except Exception:
                        pass

            for meth in ("get_prev_close", "get_previous_close", "previous_close"):
                fn = getattr(broker, meth, None)
                if callable(fn):
                    try:
                        prev_close = _sf(fn("SPY"))
                        if prev_close is not None:
                            break
                    except Exception:
                        pass

            if last_px is not None and prev_close is not None and prev_close > 0:
                return ((last_px / prev_close) - 1.0) * 100.0

        # --- 2) Fallback: previous close from bars_daily + live price from broker ---
        prev = _prev_trading_day_from_bars(conn, "SPY", day)
        prev_close = _close_on(conn, "SPY", prev) if prev else None

        if broker is not None and prev_close is not None and prev_close > 0:
            last_px = None
            for meth in ("get_latest_price", "get_last_price", "last_price", "get_latest_trade_price", "get_last_trade_price"):
                fn = getattr(broker, meth, None)
                if callable(fn):
                    try:
                        last_px = _sf(fn("SPY"))
                        if last_px is not None:
                            break
                    except Exception:
                        pass

            if last_px is not None and last_px > 0:
                return ((last_px / prev_close) - 1.0) * 100.0

        # --- 3) Final fallback: bars_daily close-to-close (EOD only) ---
        if prev:
            c0 = _close_on(conn, "SPY", prev)
            c1 = _close_on(conn, "SPY", day)
            if c0 is not None and c1 is not None and c0 > 0:
                return ((c1 / c0) - 1.0) * 100.0

        return None

    def _snapshot_account_into_db(conn, *, broker, day: str, run_id: int | None) -> dict[str, Any]:
        a = broker.get_account()

        def _f(x):
            try:
                return None if x is None else float(x)
            except Exception:
                return None

        cash = _f(getattr(a, "cash", None))
        equity = _f(getattr(a, "equity", None))
        buying_power = _f(getattr(a, "buying_power", None))
        long_mv = _f(getattr(a, "long_market_value", None))

        if equity is None:
            return {"skipped": True, "reason": "account_missing_equity"}

        if cash is None:
            cash = 0.0

        conn.execute(
            """
            INSERT INTO account_snapshots_daily(asof_date, cash, equity, buying_power, long_market_value, run_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(asof_date) DO UPDATE SET
              cash=excluded.cash,
              equity=excluded.equity,
              buying_power=excluded.buying_power,
              long_market_value=excluded.long_market_value,
              run_id=excluded.run_id,
              created_at=datetime('now');
            """,
            (day, cash, equity, buying_power, long_mv, run_id),
        )
        return {"asof": day, "cash": cash, "equity": equity, "buying_power": buying_power, "long_mv": long_mv}

    def _prev_snapshot_day(conn, day: str) -> str | None:
        # Prefer the previous day for which we actually have an equity snapshot (robust, universe-agnostic).
        r = conn.execute(
            """
            SELECT MAX(asof_date) AS d
            FROM account_snapshots_daily
            WHERE asof_date < ?;
            """,
            (day,),
        ).fetchone()
        return str(r["d"]) if r and r["d"] else None

    def _portfolio_day_return_from_equity_snapshots(conn, day: str) -> tuple[float | None, str | None]:
        """
        Intraday-safe portfolio return:
          equity(today) vs equity(prev snapshot day)
        """
        prev = _prev_snapshot_day(conn, day)
        if not prev:
            return None, None

        r0 = conn.execute(
            "SELECT equity FROM account_snapshots_daily WHERE asof_date=?;",
            (prev,),
        ).fetchone()
        r1 = conn.execute(
            "SELECT equity FROM account_snapshots_daily WHERE asof_date=?;",
            (day,),
        ).fetchone()
        if not r0 or not r1:
            return None, None

        try:
            e0 = float(r0["equity"])
            e1 = float(r1["equity"])
        except Exception:
            return None, None
        if e0 <= 0:
            return None, None

        return ((e1 / e0) - 1.0) * 100.0, f"equity_snapshots prev={prev} day={day}"

    def _portfolio_positions_day_return_pct(conn, day: str) -> tuple[float | None, str | None]:
        """
        EOD-ish fallback: positions-only MTM using closes in bars_daily.
        Will NOT match intraday UI unless you store intraday prices in DB.
        """
        pos = conn.execute(
            """
            SELECT UPPER(symbol) AS symbol, qty
            FROM positions
            WHERE COALESCE(qty,0) > 0.000001;
            """
        ).fetchall()
        if not pos:
            return None, None

        # Need a prev day; use SPY bars if present; else use bars_daily max<day for any symbol as best-effort.
        prev = _prev_trading_day_from_bars(conn, "SPY", day)
        if not prev:
            r = conn.execute("SELECT MAX(t) AS t FROM bars_daily WHERE t < ?;", (day,)).fetchone()
            prev = str(r["t"]) if r and r["t"] else None
        if not prev:
            return None, None

        mv0 = 0.0
        mv1 = 0.0
        used = 0

        for r in pos:
            sym = str(r["symbol"] or "").upper().strip()
            try:
                qty = float(r["qty"] or 0.0)
            except Exception:
                qty = 0.0
            if not sym or qty <= 0:
                continue

            c0 = _close_on(conn, sym, prev)
            c1 = _close_on(conn, sym, day)
            if c0 is None or c1 is None or c0 <= 0 or c1 <= 0:
                continue

            mv0 += qty * c0
            mv1 += qty * c1
            used += 1

        if used == 0 or mv0 <= 0:
            return None, None

        return ((mv1 / mv0) - 1.0) * 100.0, f"positions_mtm_fallback prev={prev} day={day} used={used}"

    # --- Crash guard knobs ---
    enable_guard = env_bool("TRADING_CRASH_GUARD_ENABLE", True)
    require_both = env_bool("TRADING_CRASH_GUARD_REQUIRE_BOTH", False)
    spy_halt_pct = env_float("TRADING_CRASH_GUARD_SPY_HALT_PCT", 2.5)
    port_halt_pct = env_float("TRADING_CRASH_GUARD_PORTFOLIO_HALT_PCT", 2.5)

    cfg = cfg or exit_rule_config_from_env()
    rows = evaluate_exit_advice(asof=asof, cfg=cfg)
    sell_rows = [r for r in rows if r["action"] == "SELL"]

    summary: dict[str, Any] = {
        "asof": asof or "latest",
        "advisor_rows": len(rows),
        "sell_rows": len(sell_rows),
        "run_id": None,
        "inserted": 0,
        "would_insert": 0,
        "skipped_existing": 0,
        "enable_guard": enable_guard,
        "sell_halted": False,
        "sell_halt_reason": None,
        "spy_day_ret_pct": None,
        "portfolio_day_ret_pct": None,
        "portfolio_day_ret_source": None,
        "account_snapshot": None,
    }

    with connect() as conn:
        # IMPORTANT: use *today* for crash-guard/equity snapshot, not bars_daily MAX(t)
        guard_day = today_ny_str()
        summary["asof"] = guard_day

        run_id = _pick_run_id_for_asof(conn, guard_day) or _pick_run_id_for_asof(conn, asof or _resolve_asof_date(conn))
        summary["run_id"] = run_id

        # Snapshot broker equity for *today* so portfolio return is intraday-correct
        try:
            broker = make_broker()
            snap = _snapshot_account_into_db(conn, broker=broker, day=guard_day, run_id=run_id)
            summary["account_snapshot"] = snap
        except Exception as e:
            summary["account_snapshot"] = {"skipped": True, "reason": f"snapshot_failed: {e}"}

        # Compute guard metrics even if there are no sell rows (so you can test)
        #spy_ret = _spy_day_return_pct(conn, guard_day) if enable_guard else None
        spy_ret = _spy_day_return_pct(conn, asof, broker=broker) if enable_guard else None
        summary["spy_day_ret_pct"] = spy_ret

        port_ret = None
        port_src = None
        if enable_guard:
            port_ret, port_src = _portfolio_day_return_from_equity_snapshots(conn, guard_day)
            if port_ret is None:
                port_ret, port_src = _portfolio_positions_day_return_pct(conn, guard_day)

        summary["portfolio_day_ret_pct"] = port_ret
        summary["portfolio_day_ret_source"] = port_src

        crash_spy = (spy_ret is not None and spy_ret <= -abs(spy_halt_pct))
        crash_port = (port_ret is not None and port_ret <= -abs(port_halt_pct))

        crash = False
        if enable_guard:
            crash = (crash_spy and crash_port) if require_both else (crash_spy or crash_port)

        # Always log one line so you can see it in cron logs
        log.info(
            "CRASH_GUARD enabled=%s spy=%s port=%s src=%s halted=%s",
            enable_guard,
            ("None" if spy_ret is None else f"{spy_ret:.2f}"),
            ("None" if port_ret is None else f"{port_ret:.2f}"),
            port_src,
            crash,
        )

        if crash:
            reasons: list[str] = []
            if crash_spy:
                reasons.append(f"SPY_day={spy_ret:.2f}% <= -{abs(spy_halt_pct):.2f}%")
            if crash_port:
                reasons.append(f"PORT_day={port_ret:.2f}% <= -{abs(port_halt_pct):.2f}% src={port_src}")
            if require_both:
                reasons.append("mode=require_both")

            summary["sell_halted"] = True
            summary["sell_halt_reason"] = " | ".join(reasons) if reasons else "crash_guard_triggered"
            summary["would_insert"] = len(sell_rows)

            log.info("CRASH_GUARD HALT REASON: %s", summary["sell_halt_reason"])
            return summary

        # No sells? return after we’ve still computed + reported guard metrics
        if not sell_rows:
            return summary

        if run_id is None:
            raise RuntimeError("Cannot emit sell intents: runs table is empty (no run_id available).")

        inserted = 0
        would_insert = 0
        skipped = 0

        for r in sell_rows:
            symbol = str(r["symbol"] or "").upper().strip()
            signal_key = str(r["exit_signal_key"] or "").strip()
            reason = f"{signal_key}: {r.get('rationale') or ''}"

            note = _format_policy_exit_annotation(
                policy,
                entry_key=r.get("entry_signal_key"),
                exit_key=r.get("exit_signal_key"),
            )
            if note:
                reason = f"{reason} | {note}"

            if dry_run:
                would_insert += 1
                continue

            cur = conn.execute(
                """
                INSERT OR IGNORE INTO intents(run_id, symbol, action, strength, reason, signal_key)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (run_id, symbol, "sell", None, reason, signal_key),
            )
            if cur.rowcount == 1:
                inserted += 1
            else:
                skipped += 1

        summary["inserted"] = inserted
        summary["would_insert"] = would_insert
        summary["skipped_existing"] = skipped

        if not dry_run:
            conn.commit()

    return summary