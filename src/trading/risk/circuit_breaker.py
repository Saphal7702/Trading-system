from __future__ import annotations

from dataclasses import dataclass

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


def evaluate_and_apply(*, env: str, broker, asof: str | None) -> dict:
    """
    Portfolio circuit breaker:
      - reads current equity from broker
      - computes peak-to-trough drawdown
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

    # min equity floor (optional)
    floor = float(limits.get("min_equity_floor") or 0.0)
    if floor > 0 and equity <= floor and not operator_override:
        prev = st.get("state")
        set_state(env=env, state=STATE_HALT_ALL, reason=f"equity_floor({equity:.2f} <= {floor:.2f})", actor="system")
        emit_event(env=env, event_type="EQUITY_FLOOR", prev_state=prev, new_state=STATE_HALT_ALL, metrics=metrics, reason="equity_floor", actor="system")
        st = get_effective_state(env)

    pause_th = float(limits["max_dd_pause_buys_pct"])
    sell_th = float(limits["max_dd_sell_only_pct"])
    halt_th = float(limits["max_dd_halt_all_pct"])
    reset_th = float(limits["hysteresis_reset_pct"])

    desired = st.get("state") or STATE_NORMAL

    if dd >= halt_th:
        desired = STATE_HALT_ALL
    elif dd >= sell_th:
        desired = STATE_SELL_ONLY
    elif dd >= pause_th:
        desired = STATE_PAUSE_BUYS
    else:
        # Hysteresis: only return to NORMAL if dd <= reset threshold
        if dd <= reset_th:
            desired = STATE_NORMAL
        else:
            # remain in existing state if it was risk-triggered
            desired = st.get("state") or STATE_NORMAL

    if not operator_override:
        if desired != (st.get("state") or STATE_NORMAL):
            prev = st.get("state")
            set_state(env=env, state=desired, reason=f"dd={dd:.4f}", actor="system")
            emit_event(env=env, event_type="DD_TRIGGER", prev_state=prev, new_state=desired, metrics=metrics, reason="circuit_breaker", actor="system")

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
