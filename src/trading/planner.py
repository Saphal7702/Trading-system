from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os

from .db import connect
from .compliance import can_sell
from .strategy_sma import Signal


@dataclass(frozen=True)
class Intent:
    symbol: str
    action: str   # buy/sell/hold
    reason: str
    strength: float | None = None
    # Optional: carry sizing hint forward (execute can use later)
    target_notional: float | None = None


def _current_positions() -> dict[str, dict]:
    with connect() as conn:
        rows = conn.execute("SELECT symbol, qty, opened_at FROM positions;").fetchall()
    pos: dict[str, dict] = {}
    for r in rows:
        pos[r["symbol"]] = {"qty": float(r["qty"]), "opened_at": r["opened_at"]}
    return pos


def _get_buying_power_fallback(default: float = 0.0) -> float:
    """
    Get latest buying_power from broker_accounts cache.
    If missing, return default (we'll behave conservatively).
    """
    with connect() as conn:
        r = conn.execute(
            """
            SELECT buying_power
            FROM broker_accounts
            ORDER BY last_synced_at DESC
            LIMIT 1;
            """
        ).fetchone()
    if not r or r["buying_power"] is None:
        return float(default)
    try:
        return float(r["buying_power"])
    except Exception:
        return float(default)


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return float(default)
    try:
        return float(v)
    except ValueError:
        return float(default)


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return int(default)
    try:
        return int(v)
    except ValueError:
        return int(default)


def plan_intents(
    signals: list[Signal],
    *,
    max_positions: int | None = None,
    per_position_notional: float | None = None,
    cash_buffer: float | None = None,
) -> list[Intent]:
    """
    Planner enforces:
      - compliance (min hold before sell)
      - budget realism for small accounts
      - max positions cap

    Defaults controlled by env:
      TRADING_MAX_POSITIONS (default 5)
      TRADING_PER_POSITION  (default 100)
      TRADING_CASH_BUFFER   (default 25)
      TRADING_ASSUMED_BP    (default 0)  # fallback if broker_accounts empty
    """
    pos = _current_positions()
    now = datetime.now(timezone.utc)

    max_positions = max_positions if max_positions is not None else _env_int("TRADING_MAX_POSITIONS", 5)
    per_position_notional = per_position_notional if per_position_notional is not None else _env_float("TRADING_PER_POSITION", 100.0)
    cash_buffer = cash_buffer if cash_buffer is not None else _env_float("TRADING_CASH_BUFFER", 25.0)

    assumed_bp = _env_float("TRADING_ASSUMED_BP", 0.0)
    buying_power = _get_buying_power_fallback(default=assumed_bp)

    # Count currently held positions (>0 qty)
    held_symbols = [s for s, p in pos.items() if p.get("qty", 0.0) > 0.0]
    held_count = len(held_symbols)

    intents: list[Intent] = []

    # 1) First, process SELL/HOLD decisions (so we know what might free slots later)
    # (Simple version: just create intents; we won't assume sells will fill instantly.)
    # We'll still keep the buy cap conservative.
    buy_candidates: list[Signal] = []

    for s in signals:
        holding = s.symbol in pos and pos[s.symbol]["qty"] > 0

        if s.action == "sell":
            if not holding:
                intents.append(Intent(s.symbol, "hold", "Not holding; skip sell", s.strength))
            else:
                opened_at = pos[s.symbol]["opened_at"]
                d = can_sell(opened_at, now=now)
                if not d.allowed:
                    intents.append(Intent(s.symbol, "hold", f"Sell blocked: {d.reason}", s.strength))
                else:
                    intents.append(Intent(s.symbol, "sell", s.reason, s.strength))

        elif s.action == "buy":
            if holding:
                intents.append(Intent(s.symbol, "hold", "Already holding; skip buy", s.strength))
            else:
                buy_candidates.append(s)

        else:
            intents.append(Intent(s.symbol, "hold", s.reason, s.strength))

    # 2) Budget-aware BUY selection
    # Sort strongest first (None treated as 0)
    buy_candidates.sort(key=lambda x: float(x.strength or 0.0), reverse=True)

    slots_left = max(0, max_positions - held_count)

    # How many buys can we afford?
    # We require: buying_power - cash_buffer >= per_position_notional * n
    # n_afford = floor((buying_power - cash_buffer) / per_position_notional)
    spendable = max(0.0, buying_power - cash_buffer)
    n_afford = int(spendable // per_position_notional) if per_position_notional > 0 else 0

    n_buys_allowed = max(0, min(slots_left, n_afford))

    for idx, s in enumerate(buy_candidates):
        if idx < n_buys_allowed:
            intents.append(
                Intent(
                    s.symbol,
                    "buy",
                    s.reason,
                    s.strength,
                    target_notional=per_position_notional,
                )
            )
        else:
            # Explain why it was skipped
            if slots_left <= 0:
                intents.append(Intent(s.symbol, "hold", f"Max positions reached ({max_positions}); skip buy", s.strength))
            elif n_afford <= 0:
                intents.append(Intent(s.symbol, "hold", f"Insufficient buying power for ${per_position_notional:.2f} (buffer ${cash_buffer:.2f}); skip buy", s.strength))
            else:
                intents.append(Intent(s.symbol, "hold", f"Budget/slots limited; skip buy", s.strength))

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