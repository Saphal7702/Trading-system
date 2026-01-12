from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone

from .db import connect
from .compliance import can_sell
from .strategy_sma import Signal

@dataclass(frozen=True)
class Intent:
    symbol: str
    action: str   # buy/sell/hold
    reason: str
    strength: float | None = None

def _current_positions() -> dict[str, dict]:
    with connect() as conn:
        rows = conn.execute("SELECT symbol, qty, opened_at FROM positions;").fetchall()
    pos = {}
    for r in rows:
        pos[r["symbol"]] = {"qty": float(r["qty"]), "opened_at": r["opened_at"]}
    return pos

def plan_intents(signals: list[Signal]) -> list[Intent]:
    pos = _current_positions()
    now = datetime.now(timezone.utc)

    intents: list[Intent] = []
    for s in signals:
        holding = s.symbol in pos and pos[s.symbol]["qty"] > 0

        if s.action == "buy":
            if holding:
                intents.append(Intent(s.symbol, "hold", "Already holding; skip buy"))
            else:
                intents.append(Intent(s.symbol, "buy", s.reason, s.strength))

        elif s.action == "sell":
            if not holding:
                intents.append(Intent(s.symbol, "hold", "Not holding; skip sell"))
            else:
                opened_at = pos[s.symbol]["opened_at"]
                d = can_sell(opened_at, now=now)
                if not d.allowed:
                    intents.append(Intent(s.symbol, "hold", f"Sell blocked: {d.reason}"))
                else:
                    intents.append(Intent(s.symbol, "sell", s.reason, s.strength))

        else:
            intents.append(Intent(s.symbol, "hold", s.reason, s.strength))

    return intents

def save_intents(run_id: int, intents: list[Intent]) -> int:
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO intents(run_id, symbol, action, strength, reason)
            VALUES (?, ?, ?, ?, ?);
            """,
            [(run_id, i.symbol, i.action, i.strength, i.reason) for i in intents],
        )
    return len(intents)
