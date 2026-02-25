from __future__ import annotations

from datetime import datetime, timezone, timedelta

from ..db import connect
from .limits import get_limits
from .state import (
    get_effective_state,
    set_state,
    STATE_NORMAL,
    STATE_PAUSE_BUYS,
    STATE_SELL_ONLY,
    STATE_HALT_ALL,
)
from .events import emit_event


def _reset_date(env: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT reset_ts FROM risk_peak_reset WHERE env=?;", (env,)).fetchone()
    if not row:
        return None
    ts = row["reset_ts"]
    return str(ts)[:10] if ts else None


def compute_peak_and_dd(*, env: str, equity: float | None) -> tuple[float, float]:
    """Return (peak_equity, drawdown_pct). drawdown_pct is in [0, 1]."""
    env = (env or "paper").lower()
    reset_date = _reset_date(env)

    with connect() as conn:
        if reset_date:
            row = conn.execute(
                "SELECT MAX(equity) AS peak FROM account_snapshots_daily WHERE asof_date >= ?;",
                (reset_date,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT MAX(equity) AS peak FROM account_snapshots_daily;",
            ).fetchone()

    peak_db = float(row["peak"]) if row and row["peak"] is not None else None

    if equity is None and peak_db is None:
        return (0.0, 0.0)

    if equity is None:
        equity = float(peak_db)

    peak = max(float(peak_db) if peak_db is not None else float(equity), float(equity))

    if peak <= 0:
        return (peak, 0.0)

    dd = max(0.0, (peak - float(equity)) / peak)
    return (peak, dd)


def _env_int(name: str, default: int) -> int:
    import os

    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return int(default)
    try:
        return int(float(v))
    except Exception:
        return int(default)


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = str(s).strip()
    # SQLite datetime('now') -> "YYYY-MM-DD HH:MM:SS"
    try:
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s.replace(" ", "T") + "+00:00")
    except Exception:
        try:
            # last resort: best-effort, assume UTC
            return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            return None


def _severity(state: str) -> int:
    if state == STATE_NORMAL:
        return 0
    if state == STATE_PAUSE_BUYS:
        return 1
    if state == STATE_SELL_ONLY:
        return 2
    if state == STATE_HALT_ALL:
        return 3
    return 3


def _max_state(a: str, b: str) -> str:
    return a if _severity(a) >= _severity(b) else b


def _dd_to_state(*, dd: float, pause_th: float, sell_th: float, halt_th: float) -> str:
    if dd >= halt_th:
        return STATE_HALT_ALL
    if dd >= sell_th:
        return STATE_SELL_ONLY
    if dd >= pause_th:
        return STATE_PAUSE_BUYS
    return STATE_NORMAL


def _consecutive_dd_streak(
    *,
    conn,
    env: str,
    threshold: float,
    op: str,
    lookback: int = 60,
) -> int:
    """Count consecutive prior days in risk_daily meeting the condition on drawdown_pct.

    - Uses ONLY historical rows in risk_daily (i.e., dates already written).
    - Caller can include 'today' separately if desired.
    """
    op = (op or "ge").lower().strip()
    if op not in ("ge", "le"):
        op = "ge"

    rows = conn.execute(
        """
        SELECT date, drawdown_pct
        FROM risk_daily
        WHERE env=?
        ORDER BY date DESC
        LIMIT ?;
        """,
        (env, int(lookback)),
    ).fetchall()

    streak = 0
    for r in rows:
        dd = float(r["drawdown_pct"] or 0.0)
        ok = (dd >= threshold) if op == "ge" else (dd <= threshold)
        if ok:
            streak += 1
        else:
            break
    return streak

def explain_risk_status(
    *,
    env: str,
    equity: float,
    peak_equity: float,
    drawdown_pct: float,
    effective_state: dict,
    limits: dict,
) -> dict:
    """
    Read-only diagnostics for `trading risk status`.

    Returns:
      - instant_state: mapping of today's dd -> state
      - streak_candidate: state implied by dd persistence streaks
      - would_auto_state: what circuit breaker would set if no operator override
      - streak counters (pause/sell/halt/clear)
      - held_ok / can_deescalate booleans
    """
    from datetime import datetime, timezone, timedelta

    env = (env or "paper").lower()

    pause_th = float(limits["max_dd_pause_buys_pct"])
    sell_th = float(limits["max_dd_sell_only_pct"])
    halt_th = float(limits["max_dd_halt_all_pct"])
    reset_th = float(limits["hysteresis_reset_pct"])

    # knobs (same as breaker)
    pause_streak_days = _env_int("TRADING_DD_PAUSE_STREAK_DAYS", 1)
    sell_streak_days = _env_int("TRADING_DD_SELL_STREAK_DAYS", 2)
    halt_streak_days = _env_int("TRADING_DD_HALT_STREAK_DAYS", 2)
    clear_streak_days = _env_int("TRADING_DD_CLEAR_STREAK_DAYS", 3)
    min_hold_minutes = _env_int("TRADING_RISK_MIN_HOLD_MINUTES", 240)

    instant_state = _dd_to_state(dd=drawdown_pct, pause_th=pause_th, sell_th=sell_th, halt_th=halt_th)

    # streaks from risk_daily + include today
    with connect() as conn:
        pause_streak = _consecutive_dd_streak(conn=conn, env=env, threshold=pause_th, op="ge") + (1 if drawdown_pct >= pause_th else 0)
        sell_streak = _consecutive_dd_streak(conn=conn, env=env, threshold=sell_th, op="ge") + (1 if drawdown_pct >= sell_th else 0)
        halt_streak = _consecutive_dd_streak(conn=conn, env=env, threshold=halt_th, op="ge") + (1 if drawdown_pct >= halt_th else 0)
        clear_streak = _consecutive_dd_streak(conn=conn, env=env, threshold=reset_th, op="le") + (1 if drawdown_pct <= reset_th else 0)

    streak_candidate = STATE_NORMAL
    if halt_streak_days > 0 and halt_streak >= halt_streak_days:
        streak_candidate = STATE_HALT_ALL
    elif sell_streak_days > 0 and sell_streak >= sell_streak_days:
        streak_candidate = STATE_SELL_ONLY
    elif pause_streak_days > 0 and pause_streak >= pause_streak_days:
        streak_candidate = STATE_PAUSE_BUYS

    current = (effective_state.get("state") or STATE_NORMAL).upper().strip()

    # held_ok (min hold before loosening)
    held_ok = True
    set_at = _parse_dt(effective_state.get("set_at"))
    if set_at and min_hold_minutes > 0:
        held_ok = (datetime.now(timezone.utc) - set_at) >= timedelta(minutes=int(min_hold_minutes))

    age_minutes = None
    if set_at:
        age_minutes = int((datetime.now(timezone.utc) - set_at).total_seconds() // 60)

    can_deescalate = bool((drawdown_pct <= reset_th) and (clear_streak >= max(1, int(clear_streak_days))) and held_ok)

    # What the breaker would do IF no operator override:
    # - escalation always allowed
    # - de-escalation only allowed under can_deescalate
    desired = _max_state(current, _max_state(instant_state, streak_candidate))
    if _severity(desired) < _severity(current):
        desired = STATE_NORMAL if can_deescalate else current
    else:
        if instant_state == STATE_NORMAL and _severity(current) > 0 and not can_deescalate:
            desired = current
        
    # ---- Decision explanation ----
    decision_reason = None
    blocked_by_hold = False

    operator_override = (effective_state.get("set_by") == "operator")

    if operator_override:
        decision_reason = "operator_override_active"
    else:
        if desired != current:
            if _severity(desired) > _severity(current):
                if streak_candidate != STATE_NORMAL:
                    decision_reason = f"streak_trigger -> {streak_candidate}"
                else:
                    decision_reason = f"instant_dd_trigger -> {instant_state}"
            else:
                decision_reason = "recovery_trigger"
        else:
            if instant_state == STATE_NORMAL and not can_deescalate and _severity(current) > 0:
                decision_reason = "recovery_blocked_by_hold_or_streak"
                blocked_by_hold = True
            else:
                decision_reason = "no_change"

    # ---- Headroom to thresholds (how far from escalation) ----
    def _headroom(threshold: float) -> float:
        # threshold and drawdown_pct are fractions (e.g., 0.05 == 5%)
        try:
            return max(0.0, float(threshold) - float(drawdown_pct))
        except Exception:
            return 0.0

    headroom = {
        "to_pause_buys": _headroom(limits["max_dd_pause_buys_pct"]),
        "to_sell_only": _headroom(limits["max_dd_sell_only_pct"]),
        "to_halt_all": _headroom(limits["max_dd_halt_all_pct"]),
        # Reset is a *recovery* band: if dd > reset, how much you must recover to re-enter reset band.
        "to_reset_band": max(0.0, float(drawdown_pct) - float(limits["hysteresis_reset_pct"])),
    }

    return {
        "operator_override": bool(operator_override),
        "instant_state": instant_state,
        "streak_candidate": streak_candidate,
        "would_auto_state": desired,
        "held_ok": bool(held_ok),
        "can_deescalate": bool(can_deescalate),
        "age_minutes": age_minutes,
        "set_at": effective_state.get("set_at"),
        "desired_state": desired,
        "decision_reason": decision_reason,
        "blocked_by_hold": blocked_by_hold,
        "headroom": headroom,
        "limits": {
            "pause_buys": pause_th,
            "sell_only": sell_th,
            "halt_all": halt_th,
            "reset": reset_th,
        },
        "streaks": {
            "pause_ge": int(pause_streak),
            "sell_ge": int(sell_streak),
            "halt_ge": int(halt_streak),
            "clear_le": int(clear_streak),
        },
        "cfg": {
            "pause_streak_days": int(pause_streak_days),
            "sell_streak_days": int(sell_streak_days),
            "halt_streak_days": int(halt_streak_days),
            "clear_streak_days": int(clear_streak_days),
            "min_hold_minutes": int(min_hold_minutes),
        },
        "snapshot": {
            "equity": float(equity),
            "peak_equity": float(peak_equity),
            "drawdown_pct": float(drawdown_pct),
        },
    }

def evaluate_and_apply(*, env: str, broker, asof: str | None) -> dict:
    """
    Portfolio circuit breaker (institutional):
      - reads current equity from broker
      - computes peak-to-trough drawdown
      - applies instant thresholds + time-based streak escalation
      - uses hysteresis + min-hold + clear-streak to de-escalate
      - transitions portfolio_state unless operator override is active
    """
    env = (env or "paper").lower()
    limits = get_limits(env)
    st = get_effective_state(env)

    # If operator override is active, do not change state here.
    operator_override = (st.get("set_by") == "operator")

    a = broker.get_account()
    equity = float(getattr(a, "equity", 0.0) or 0.0)

    peak, dd = compute_peak_and_dd(env=env, equity=equity)

    metrics = {
        "asof": asof,
        "equity": equity,
        "peak_equity": peak,
        "drawdown_pct": dd,
        "buying_power": float(getattr(a, "buying_power", 0.0) or 0.0),
        "cash": float(getattr(a, "cash", 0.0) or 0.0),
    }

    # Min equity floor (optional, hard halt)
    floor = float(limits.get("min_equity_floor") or 0.0)
    if floor > 0 and equity <= floor and not operator_override:
        prev = st.get("state")
        set_state(env=env, state=STATE_HALT_ALL, reason=f"equity_floor({equity:.2f} <= {floor:.2f})", actor="system")
        emit_event(
            env=env,
            event_type="EQUITY_FLOOR",
            prev_state=prev,
            new_state=STATE_HALT_ALL,
            metrics=metrics,
            reason="equity_floor",
            actor="system",
        )
        st = get_effective_state(env)

    pause_th = float(limits["max_dd_pause_buys_pct"])
    sell_th = float(limits["max_dd_sell_only_pct"])
    halt_th = float(limits["max_dd_halt_all_pct"])
    reset_th = float(limits["hysteresis_reset_pct"])

    # --- New institutional knobs (env based) ---
    # Escalate if DD persists
    pause_streak_days = _env_int("TRADING_DD_PAUSE_STREAK_DAYS", 1)
    sell_streak_days = _env_int("TRADING_DD_SELL_STREAK_DAYS", 2)
    halt_streak_days = _env_int("TRADING_DD_HALT_STREAK_DAYS", 2)

    # De-escalate only after sustained recovery
    clear_streak_days = _env_int("TRADING_DD_CLEAR_STREAK_DAYS", 3)

    # Prevent rapid state flipping (minutes)
    min_hold_minutes = _env_int("TRADING_RISK_MIN_HOLD_MINUTES", 240)

    # Compute instant desired from today's dd
    instant = _dd_to_state(dd=dd, pause_th=pause_th, sell_th=sell_th, halt_th=halt_th)

    # Compute streaks from historical risk_daily + include today
    with connect() as conn:
        pause_streak = _consecutive_dd_streak(conn=conn, env=env, threshold=pause_th, op="ge") + (1 if dd >= pause_th else 0)
        sell_streak = _consecutive_dd_streak(conn=conn, env=env, threshold=sell_th, op="ge") + (1 if dd >= sell_th else 0)
        halt_streak = _consecutive_dd_streak(conn=conn, env=env, threshold=halt_th, op="ge") + (1 if dd >= halt_th else 0)
        clear_streak = _consecutive_dd_streak(conn=conn, env=env, threshold=reset_th, op="le") + (1 if dd <= reset_th else 0)

    metrics.update(
        {
            "limits": {
                "pause_buys": pause_th,
                "sell_only": sell_th,
                "halt_all": halt_th,
                "reset": reset_th,
            },
            "streaks": {
                "pause_ge": pause_streak,
                "sell_ge": sell_streak,
                "halt_ge": halt_streak,
                "clear_le": clear_streak,
            },
            "streak_days_cfg": {
                "pause": pause_streak_days,
                "sell": sell_streak_days,
                "halt": halt_streak_days,
                "clear": clear_streak_days,
                "min_hold_minutes": min_hold_minutes,
            },
        }
    )

    # Streak-based escalation candidate (handles slow bleed)
    streak_candidate = STATE_NORMAL
    if halt_streak_days > 0 and halt_streak >= halt_streak_days:
        streak_candidate = STATE_HALT_ALL
    elif sell_streak_days > 0 and sell_streak >= sell_streak_days:
        streak_candidate = STATE_SELL_ONLY
    elif pause_streak_days > 0 and pause_streak >= pause_streak_days:
        streak_candidate = STATE_PAUSE_BUYS

    current = st.get("state") or STATE_NORMAL

    # Escalation is always allowed (unless operator override active).
    desired = _max_state(current, _max_state(instant, streak_candidate))

    # De-escalation guard: only allow loosening under sustained recovery
    # and only after holding current state for some minimum time.
    held_ok = True
    set_at = _parse_dt(st.get("set_at"))
    if set_at and min_hold_minutes > 0:
        held_ok = (datetime.now(timezone.utc) - set_at) >= timedelta(minutes=int(min_hold_minutes))

    can_deescalate = bool((dd <= reset_th) and (clear_streak >= max(1, int(clear_streak_days))) and held_ok)

    if _severity(desired) < _severity(current):
        # Never loosen unless the recovery conditions are met.
        desired = STATE_NORMAL if can_deescalate else current
    else:
        # If we're below pause threshold but still in a risk state,
        # keep hysteresis: do not auto-normalize unless can_deescalate.
        if instant == STATE_NORMAL and _severity(current) > 0 and not can_deescalate:
            desired = current

    # If operator override active, do not change state, but still return metrics.
    if not operator_override:
        if desired != current:
            prev = current
            set_state(env=env, state=desired, reason=f"dd={dd:.4f} instant={instant} streak={streak_candidate}", actor="system")
            emit_event(
                env=env,
                event_type="DD_TRIGGER",
                prev_state=prev,
                new_state=desired,
                metrics=metrics,
                reason="circuit_breaker",
                actor="system",
            )

    st2 = get_effective_state(env)
    out = {
        "env": env,
        "state": st2.get("state"),
        "set_by": st2.get("set_by"),
        "allow_buys": int(st2.get("allow_buys", 0)),
        "allow_sells": int(st2.get("allow_sells", 0)),
        "allow_broker": int(st2.get("allow_broker", 0)),
        "equity": equity,
        "peak_equity": peak,
        "drawdown_pct": dd,
    }
    return out