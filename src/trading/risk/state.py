from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from ..db import connect

STATE_NORMAL = "NORMAL"
STATE_PAUSE_BUYS = "PAUSE_BUYS"
STATE_SELL_ONLY = "SELL_ONLY"
STATE_HALT_ALL = "HALT_ALL"

ALL_STATES = {STATE_NORMAL, STATE_PAUSE_BUYS, STATE_SELL_ONLY, STATE_HALT_ALL}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed_risk_defaults(env: str) -> None:
    """Ensure baseline rows exist for env. Safe to call every run."""
    env = (env or "paper").lower()
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO portfolio_state(env, state, reason, set_by, set_at, expires_at, allow_sells, allow_buys, allow_broker, updated_at)
            VALUES (?, 'NORMAL', 'seed', 'system', datetime('now'), NULL, 1, 1, 1, datetime('now'));
            """,
            (env,),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO risk_limits(env, max_dd_pause_buys_pct, max_dd_sell_only_pct, max_dd_halt_all_pct, hysteresis_reset_pct, min_equity_floor, updated_at)
            VALUES (?, 0.05, 0.08, 0.12, 0.03, 0.0, datetime('now'));
            """,
            (env,),
        )


def _expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) >= exp
    except Exception:
        return False


def get_effective_state(env: str) -> dict:
    """Return current state, auto-clearing expired operator overrides."""
    env = (env or "paper").lower()
    seed_risk_defaults(env)

    with connect() as conn:
        row = conn.execute(
            "SELECT env, state, reason, set_by, set_at, expires_at, allow_sells, allow_buys, allow_broker FROM portfolio_state WHERE env=?;",
            (env,),
        ).fetchone()

        if not row:
            # Should not happen after seed, but be defensive
            return {"env": env, "state": STATE_NORMAL, "allow_sells": 1, "allow_buys": 1, "allow_broker": 1}

        if row["set_by"] == "operator" and _expired(row["expires_at"]):
            # Clear expired override back to NORMAL; circuit breaker will re-assert if needed.
            conn.execute(
                """
                UPDATE portfolio_state
                SET state='NORMAL', reason='override_expired', set_by='system', set_at=?, expires_at=NULL,
                    allow_sells=1, allow_buys=1, allow_broker=1, updated_at=?
                WHERE env=?;
                """,
                (_now_iso(), _now_iso(), env),
            )
            row = conn.execute(
                "SELECT env, state, reason, set_by, set_at, expires_at, allow_sells, allow_buys, allow_broker FROM portfolio_state WHERE env=?;",
                (env,),
            ).fetchone()

    return dict(row)


def _perms_for_state(state: str) -> tuple[int, int, int]:
    if state == STATE_NORMAL:
        return (1, 1, 1)
    if state == STATE_PAUSE_BUYS:
        return (0, 1, 1)
    if state == STATE_SELL_ONLY:
        return (0, 1, 1)
    if state == STATE_HALT_ALL:
        return (0, 0, 0)
    # unknown => safest
    return (0, 0, 0)


def set_state(*, env: str, state: str, reason: str, actor: str) -> None:
    env = (env or "paper").lower()
    state = state.strip().upper()
    if state not in ALL_STATES:
        raise ValueError(f"invalid state: {state}")

    allow_buys, allow_sells, allow_broker = _perms_for_state(state)

    with connect() as conn:
        prev = conn.execute("SELECT state FROM portfolio_state WHERE env=?;", (env,)).fetchone()
        prev_state = prev["state"] if prev else None

        conn.execute(
            """
            INSERT INTO risk_events(env, ts, event_type, prev_state, new_state, metrics_json, reason, actor)
            VALUES (?, ?, 'STATE_CHANGE', ?, ?, ?, ?, ?);
            """,
            (
                env,
                _now_iso(),
                prev_state,
                state,
                json.dumps({}),
                reason,
                actor,
            ),
        )

        conn.execute(
            """
            UPDATE portfolio_state
            SET state=?, reason=?, set_by=?, set_at=?, expires_at=NULL,
                allow_buys=?, allow_sells=?, allow_broker=?, updated_at=?
            WHERE env=?;
            """,
            (state, reason, actor, _now_iso(), allow_buys, allow_sells, allow_broker, _now_iso(), env),
        )


def set_operator_override(*, env: str, state: str, reason: str, expires_minutes: int | None = None) -> None:
    env = (env or "paper").lower()
    state = state.strip().upper()
    if state not in ALL_STATES:
        raise ValueError(f"invalid state: {state}")

    allow_buys, allow_sells, allow_broker = _perms_for_state(state)
    exp = None
    if expires_minutes is not None:
        exp = (datetime.now(timezone.utc) + timedelta(minutes=int(expires_minutes))).isoformat()

    with connect() as conn:
        prev = conn.execute("SELECT state FROM portfolio_state WHERE env=?;", (env,)).fetchone()
        prev_state = prev["state"] if prev else None

        conn.execute(
            """
            INSERT INTO risk_events(env, ts, event_type, prev_state, new_state, metrics_json, reason, actor)
            VALUES (?, ?, 'OVERRIDE_SET', ?, ?, ?, ?, 'operator');
            """,
            (env, _now_iso(), prev_state, state, json.dumps({"expires_at": exp}), reason),
        )

        conn.execute(
            """
            UPDATE portfolio_state
            SET state=?, reason=?, set_by='operator', set_at=?, expires_at=?,
                allow_buys=?, allow_sells=?, allow_broker=?, updated_at=?
            WHERE env=?;
            """,
            (state, reason, _now_iso(), exp, allow_buys, allow_sells, allow_broker, _now_iso(), env),
        )


def clear_operator_override(*, env: str) -> None:
    env = (env or "paper").lower()
    with connect() as conn:
        row = conn.execute("SELECT state, set_by FROM portfolio_state WHERE env=?;", (env,)).fetchone()
        prev_state = row["state"] if row else None
        prev_by = row["set_by"] if row else None

        conn.execute(
            """
            INSERT INTO risk_events(env, ts, event_type, prev_state, new_state, metrics_json, reason, actor)
            VALUES (?, ?, 'OVERRIDE_CLEAR', ?, 'NORMAL', ?, 'operator_clear', 'operator');
            """,
            (env, _now_iso(), prev_state, json.dumps({"prev_set_by": prev_by})),
        )

        # revert to NORMAL; circuit breaker will apply if still needed
        allow_buys, allow_sells, allow_broker = _perms_for_state(STATE_NORMAL)
        conn.execute(
            """
            UPDATE portfolio_state
            SET state='NORMAL', reason='operator_clear', set_by='system', set_at=?, expires_at=NULL,
                allow_buys=?, allow_sells=?, allow_broker=?, updated_at=?
            WHERE env=?;
            """,
            (_now_iso(), allow_buys, allow_sells, allow_broker, _now_iso(), env),
        )


def reset_peak(*, env: str, reason: str) -> None:
    env = (env or "paper").lower()
    ts = _now_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO risk_events(env, ts, event_type, prev_state, new_state, metrics_json, reason, actor)
            VALUES (?, ?, 'PEAK_RESET', NULL, NULL, ?, ?, 'operator');
            """,
            (env, ts, json.dumps({}), reason),
        )
        conn.execute(
            """
            INSERT INTO risk_peak_reset(env, reset_ts, reason, actor)
            VALUES (?, ?, ?, 'operator')
            ON CONFLICT(env) DO UPDATE SET reset_ts=excluded.reset_ts, reason=excluded.reason, actor='operator', created_at=datetime('now');
            """,
            (env, ts, reason),
        )
