from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import os

from .db import connect

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


def _rule_priority(signal_key: str) -> int:
    """
    Higher means more urgent.
    """
    if signal_key.startswith("exit_stop_loss"):
        return 100
    if signal_key.startswith("exit_trailing"):
        return 90
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
        metrics = _fetch_open_position_metrics(conn, asof)

        advice: list[dict[str, Any]] = []
        for m in metrics:
            symbol = str(m["symbol"]).upper()
            ret = float(m["unrealized_ret_pct"]) if m["unrealized_ret_pct"] is not None else 0.0
            days = int(m["holding_days"]) if m["holding_days"] is not None else 0
            peak_gain = float(m["peak_gain_pct"]) if m["peak_gain_pct"] is not None else 0.0
            dd = float(m["drawdown_from_peak_pct"]) if m["drawdown_from_peak_pct"] is not None else 0.0
            entry_signal_key = str(m.get("entry_signal_key") or "unknown")

            sma20 = sma50 = None
            if cfg.enable_sma_reversal:
                sma20, sma50 = _fetch_sma(conn, symbol, asof)

            # Decide rule
            exit_signal = "hold_trend_intact"
            action = "HOLD"
            rationale = "No exit rule triggered."

            # 1) Hard stop loss
            if ret <= -abs(cfg.stop_loss_pct):
                exit_signal = f"exit_stop_loss_{int(cfg.stop_loss_pct)}pct"
                action = "SELL"
                rationale = f"Hard stop: ret={ret:.2f}% <= -{cfg.stop_loss_pct:.2f}%"

            # 2) Trailing stop (only after meaningful peak gain)
            elif peak_gain >= cfg.trail_activate_peak_gain_pct and dd <= -abs(cfg.trail_drawdown_pct):
                exit_signal = f"exit_trailing_dd{int(cfg.trail_drawdown_pct)}_peak{int(cfg.trail_activate_peak_gain_pct)}"
                action = "SELL"
                rationale = f"Trailing stop: peak_gain={peak_gain:.2f}% and drawdown={dd:.2f}%"

            # 3) SMA reversal
            elif cfg.enable_sma_reversal and sma20 is not None and sma50 is not None and sma50 is not None and sma20 < sma50:
                exit_signal = "exit_sma_reversal_sma20_below_sma50"
                action = "SELL"
                rationale = f"Trend reversal: SMA20={sma20:.4f} < SMA50={sma50:.4f}"

            # 4) Early failure stop
            elif days >= cfg.early_fail_days and ret <= cfg.early_fail_max_ret_pct:
                exit_signal = f"exit_early_failure_{cfg.early_fail_days}d"
                action = "SELL"
                rationale = f"Early failure: days={days} ret={ret:.2f}% <= {cfg.early_fail_max_ret_pct:.2f}%"

            # 5) Time stop
            elif days >= cfg.time_stop_days and ret < cfg.time_stop_min_ret_pct:
                exit_signal = f"exit_time_stop_{cfg.time_stop_days}d"
                action = "SELL"
                rationale = f"Time stop: days={days} ret={ret:.2f}% < {cfg.time_stop_min_ret_pct:.2f}%"

            # 6) Take profit (soft)
            elif ret >= cfg.take_profit_pct:
                exit_signal = f"exit_take_profit_{int(cfg.take_profit_pct)}pct"
                action = "SELL"
                rationale = f"Take profit: ret={ret:.2f}% >= {cfg.take_profit_pct:.2f}%"

            # Optional WATCH: early negative before early_fail_days
            elif days < cfg.early_fail_days and ret < 0:
                exit_signal = "watch_early_weakness"
                action = "WATCH"
                rationale = f"Early weakness: days={days} ret={ret:.2f}% (monitor until day {cfg.early_fail_days})"

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
    }

    with connect() as conn:
        resolved_asof = asof or _resolve_asof_date(conn)
        run_id = _pick_run_id_for_asof(conn, resolved_asof)
        summary["run_id"] = run_id

        if not sell_rows:
            return summary

        if run_id is None:
            raise RuntimeError("Cannot emit sell intents: runs table is empty (no run_id available).")

        inserted = 0
        would_insert = 0
        skipped = 0

        for r in sell_rows:
            symbol = r["symbol"]
            signal_key = r["exit_signal_key"]

            reason = f"{signal_key}: {r['rationale']}"

            # ---- policy advisory annotation (read-only) ----
            note = _format_policy_exit_annotation(
                policy,
                entry_key=r.get("entry_signal_key"),
                exit_key=r.get("exit_signal_key"),
            )
            if note:
                reason = f"{reason} | {note}"

            try:
                conn.execute(
                    """
                    INSERT INTO intents(run_id, symbol, action, strength, reason, signal_key)
                    VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    (
                        run_id,
                        symbol,
                        "sell",
                        None,
                        reason,
                        signal_key,
                    ),
                )
                inserted += 1
            except Exception:
                skipped_existing += 1

        summary["inserted"] = inserted
        summary["would_insert"] = would_insert
        summary["skipped_existing"] = skipped

        # Explicit commit when writing (nice for clarity)
        if not dry_run:
            conn.commit()

    return summary