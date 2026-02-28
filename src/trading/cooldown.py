from __future__ import annotations

from datetime import datetime, timedelta

from .db import connect

def _add_days(yyyy_mm_dd: str, days: int) -> str:
    d = datetime.strptime(yyyy_mm_dd, "%Y-%m-%d").date()
    return (d + timedelta(days=days)).strftime("%Y-%m-%d")

def is_in_cooldown(symbol: str, asof: str) -> bool:
    sym = symbol.upper().strip()
    with connect() as conn:
        r = conn.execute(
            "SELECT cooldown_until FROM symbol_cooldowns WHERE symbol=?;",
            (sym,),
        ).fetchone()
    if not r:
        return False
    return str(r["cooldown_until"]) >= asof

def set_cooldown(symbol: str, asof: str, days: int, reason: str) -> str:
    sym = symbol.upper().strip()
    until = _add_days(asof, days)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO symbol_cooldowns(symbol, cooldown_until, reason)
            VALUES (?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
            cooldown_until = CASE
                WHEN excluded.cooldown_until > symbol_cooldowns.cooldown_until
                THEN excluded.cooldown_until
                ELSE symbol_cooldowns.cooldown_until
            END,
            reason = excluded.reason,
            set_at = datetime('now');
            """,
            (sym, until, reason),
        )
    return until
