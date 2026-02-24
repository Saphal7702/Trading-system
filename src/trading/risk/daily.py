from __future__ import annotations

from datetime import datetime, timezone
from ..db import connect


def upsert_risk_daily(*, env: str, asof: str, risk: dict) -> dict:
    env = (env or "paper").lower()
    if not asof:
        return {"skipped": True, "reason": "missing_asof"}
    if not isinstance(risk, dict):
        return {"skipped": True, "reason": "missing_risk"}

    date = asof
    equity = float(risk.get("equity") or 0.0)
    peak = float(risk.get("peak_equity") or equity)
    dd = float(risk.get("drawdown_pct") or 0.0)
    state = str(risk.get("state") or "UNKNOWN")
    buys_blocked = 1 if int(risk.get("allow_buys", 0)) == 0 else 0
    sells_blocked = 1 if int(risk.get("allow_sells", 0)) == 0 else 0
    broker_blocked = 1 if int(risk.get("allow_broker", 0)) == 0 else 0

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO risk_daily(env, date, equity, peak_equity, drawdown_pct, state, buys_blocked, sells_blocked, broker_blocked, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(env, date) DO UPDATE SET
              equity=excluded.equity,
              peak_equity=excluded.peak_equity,
              drawdown_pct=excluded.drawdown_pct,
              state=excluded.state,
              buys_blocked=excluded.buys_blocked,
              sells_blocked=excluded.sells_blocked,
              broker_blocked=excluded.broker_blocked,
              created_at=datetime('now');
            """,
            (env, date, equity, peak, dd, state, buys_blocked, sells_blocked, broker_blocked),
        )

    return {"env": env, "date": date, "state": state, "dd": dd, "equity": equity, "peak": peak}
