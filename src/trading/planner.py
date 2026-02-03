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
    target_notional: float | None = None
    signal_key: str | None = None

    # Phase 5 (read-only): policy overlay
    policy_path: str | None = None
    policy_asof: str | None = None
    policy_rec: str | None = None
    policy_score: float | None = None
    policy_best_exits: str | None = None

    # Phase 5 (advisory): sizing recommendation (NOT enforced)
    policy_trades: int | None = None
    policy_enforceable: bool = False
    policy_mult: float | None = None
    policy_reco_notional: float | None = None
    policy_would_skip: bool = False


def _norm_sym(sym: str | None) -> str:
    return (sym or "").strip().upper()


def _signal_key_for(action: str, reason: str) -> str | None:
    """
    Map free-text reason -> stable signal key.
    Keep keys stable forever once introduced.
    """
    a = (action or "").strip().lower()
    r = (reason or "").strip()

    # SMA strategy mappings
    if a == "buy" and "SMA20 crossed above SMA50" in r:
        return "sma20_cross_up_sma50"
    if a == "sell" and "SMA20 crossed below SMA50" in r:
        return "sma20_cross_down_sma50"

    # Unknown/unmapped signal (safe)
    return None


def _current_positions() -> dict[str, dict]:
    """
    Returns:
      { "AAPL": {"qty": 1.0, "opened_at": "2026-01-20T15:09:30+00:00"} , ... }

    Conservative behaviors:
      - symbols are normalized to uppercase
      - missing qty -> 0
      - opened_at can be None (planner will block sells if missing)
    """
    with connect() as conn:
        rows = conn.execute("SELECT symbol, qty, opened_at FROM positions;").fetchall()

    pos: dict[str, dict] = {}
    for r in rows:
        sym = _norm_sym(r["symbol"])
        if not sym:
            continue

        qty_val = r["qty"]
        try:
            qty = float(qty_val) if qty_val is not None else 0.0
        except Exception:
            qty = 0.0

        opened_at = r["opened_at"]  # can be None; handled downstream
        pos[sym] = {"qty": qty, "opened_at": opened_at}

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
    policy=None,  # PolicySnapshot | None (kept untyped to avoid import coupling)
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
    per_position_notional = (
        per_position_notional if per_position_notional is not None else _env_float("TRADING_PER_POSITION", 100.0)
    )
    cash_buffer = cash_buffer if cash_buffer is not None else _env_float("TRADING_CASH_BUFFER", 25.0)

    # Policy sizing knobs
    pol_min_trades_env = _env_int("TRADING_POLICY_MIN_TRADES_ENFORCE", 20)
    pol_boost_mult = _env_float("TRADING_POLICY_BOOST_MULT", 1.25)
    pol_reduce_mult = _env_float("TRADING_POLICY_REDUCE_MULT", 0.50)

    def _policy_enforce_threshold() -> int:
        """
        Use stricter of:
          - env enforcement threshold
          - policy snapshot's own meta.min_entry_trades (what generated it)
        """
        thr = pol_min_trades_env
        try:
            if policy and isinstance(getattr(policy, "meta", None), dict):
                v = policy.meta.get("min_entry_trades")
                if v is not None:
                    thr = max(thr, int(v))
        except Exception:
            pass
        return thr

    enforce_thr = _policy_enforce_threshold()

    assumed_bp = _env_float("TRADING_ASSUMED_BP", 0.0)
    buying_power = _get_buying_power_fallback(default=assumed_bp)

    # Count currently held positions (>0 qty)
    held_symbols = [s for s, p in pos.items() if float(p.get("qty", 0.0) or 0.0) > 0.0]
    held_count = len(held_symbols)

    intents: list[Intent] = []
    buy_candidates: list[Signal] = []

    def _policy_for_entry(entry_key: str | None) -> tuple[str | None, float | None, str | None, str | None]:
        """
        Returns: (rec, score, asof, best_exits_csv)
        """
        if not policy or not entry_key:
            return (None, None, None, None)

        try:
            rec_row = policy.entry_rec(entry_key) if hasattr(policy, "entry_rec") else None
            rec = rec_row.get("rec") if isinstance(rec_row, dict) else None
            score = rec_row.get("score") if isinstance(rec_row, dict) else None
            asof = getattr(policy, "asof", None)

            best = []
            if hasattr(policy, "best_exits"):
                xs = policy.best_exits(entry_key) or []
                # xs is a list[dict] like {"exit_key": "...", ...}
                for x in xs:
                    k = x.get("exit_key")
                    if k:
                        best.append(str(k))
            best_csv = ", ".join(best) if best else None

            return (str(rec) if rec is not None else None,
                    float(score) if score is not None else None,
                    str(asof) if asof is not None else None,
                    best_csv)
        except Exception:
            # policy is advisory; never break planning
            return (None, None, None, None)
        
    def _policy_sizing_for_entry(entry_key: str | None, base_notional: float | None) -> dict:
        out = {
            "policy_rec": None,
            "policy_score": None,
            "policy_asof": None,
            "policy_best_exits": None,
            "policy_trades": None,
            "policy_enforceable": False,
            "policy_mult": None,
            "policy_reco_notional": None,
            "policy_would_skip": False,
        }

        if not policy or not entry_key or base_notional is None:
            return out

        try:
            row = policy.entry_rec(entry_key)
            if not isinstance(row, dict):
                return out

            rec = row.get("rec")
            score = row.get("score")
            trades_raw = row.get("trades")
            trades = int(trades_raw) if trades_raw is not None else None

            enforceable = bool(trades is not None and trades >= enforce_thr)

            mult = 1.0
            would_skip = False

            if rec == "BOOST":
                mult = pol_boost_mult
            elif rec == "REDUCE":
                mult = pol_reduce_mult
            elif rec == "DISABLE":
                mult = 0.0
                would_skip = True

            reco_notional = float(base_notional) * float(mult)

            best = []
            try:
                for x in policy.best_exits(entry_key)[:3]:
                    ek = x.get("exit_key")
                    if ek:
                        best.append(str(ek))
            except Exception:
                pass

            out.update({
                "policy_rec": str(rec) if rec is not None else None,
                "policy_score": float(score) if score is not None else None,
                "policy_asof": getattr(policy, "asof", None),
                "policy_best_exits": ", ".join(best) if best else None,
                "policy_trades": trades,
                "policy_enforceable": enforceable,
                "policy_mult": float(mult),
                "policy_reco_notional": float(reco_notional),
                "policy_would_skip": bool(would_skip and enforceable),
            })
            return out
        except Exception:
            return out


    # 1) Process SELL/HOLD decisions; collect BUY candidates
    for sig in signals:
        sym = _norm_sym(sig.symbol)
        if not sym:
            continue

        holding = sym in pos and float(pos[sym].get("qty", 0.0) or 0.0) > 0.0

        if sig.action == "sell":
            if not holding:
                intents.append(Intent(sym, "hold", "Not holding; skip sell", sig.strength))
                continue

            opened_at = pos[sym].get("opened_at")
            if not opened_at:
                intents.append(
                    Intent(
                        sym,
                        "hold",
                        "Sell blocked: missing opened_at (sync fills/positions first)",
                        sig.strength,
                    )
                )
                continue

            d = can_sell(opened_at, now=now)
            if not d.allowed:
                intents.append(Intent(sym, "hold", f"Sell blocked: {d.reason}", sig.strength))
            else:
                intents.append(
                    Intent(
                        sym,
                        "sell",
                        sig.reason,
                        sig.strength,
                        signal_key=_signal_key_for("sell", sig.reason),
                    )
                )

        elif sig.action == "buy":
            if holding:
                intents.append(Intent(sym, "hold", "Already holding; skip buy", sig.strength))
            else:
                buy_candidates.append(Signal(sym, "buy", sig.reason, strength=sig.strength))

        else:
            intents.append(Intent(sym, "hold", sig.reason, sig.strength))

    # 2) Budget-aware BUY selection
    buy_candidates.sort(key=lambda x: float(x.strength or 0.0), reverse=True)

    slots_left = max(0, max_positions - held_count)

    spendable = max(0.0, buying_power - cash_buffer)
    n_afford = int(spendable // per_position_notional) if per_position_notional > 0 else 0
    n_buys_allowed = max(0, min(slots_left, n_afford))

    for idx, sig in enumerate(buy_candidates):
        sym = _norm_sym(sig.symbol)

        if idx < n_buys_allowed:
            entry_key = _signal_key_for("buy", sig.reason)

            # Single policy call (contains overlay + sizing)
            pol = _policy_sizing_for_entry(entry_key, per_position_notional)

            intents.append(
                Intent(
                    sym,
                    "buy",
                    sig.reason,
                    sig.strength,
                    target_notional=per_position_notional,
                    signal_key=entry_key,

                    # policy overlay
                    policy_rec=pol.get("policy_rec"),
                    policy_score=pol.get("policy_score"),
                    policy_asof=pol.get("policy_asof"),
                    policy_best_exits=pol.get("policy_best_exits"),

                    # advisory sizing
                    policy_trades=pol.get("policy_trades"),
                    policy_enforceable=bool(pol.get("policy_enforceable", False)),
                    policy_mult=pol.get("policy_mult"),
                    policy_reco_notional=pol.get("policy_reco_notional"),
                    policy_would_skip=bool(pol.get("policy_would_skip", False)),
                )
            )

        else:
            if slots_left <= 0:
                intents.append(Intent(sym, "hold", f"Max positions reached ({max_positions}); skip buy", sig.strength))
            elif n_afford <= 0:
                intents.append(
                    Intent(
                        sym,
                        "hold",
                        f"Insufficient buying power for ${per_position_notional:.2f} (buffer ${cash_buffer:.2f}); skip buy",
                        sig.strength,
                    )
                )
            else:
                intents.append(Intent(sym, "hold", "Budget/slots limited; skip buy", sig.strength))

    return intents


def save_intents(run_id: int, intents: list[Intent]) -> int:
    """
    Persist intents. If policy columns don't exist yet, fall back to the old insert
    (so we don't brick older DBs).
    """
    import sqlite3

    rows_new = [
        (
            run_id,
            _norm_sym(i.symbol),
            i.action,
            i.strength,
            i.reason,
            i.signal_key,
            i.target_notional,
            i.policy_path,
            i.policy_asof,
            i.policy_rec,
            i.policy_score,
            i.policy_trades,
            1 if getattr(i, "policy_enforceable", False) else 0,
            i.policy_mult,
            i.policy_reco_notional,
            1 if getattr(i, "policy_would_skip", False) else 0,
            i.policy_best_exits,
        )
        for i in intents
    ]

    with connect() as conn:
        try:
            conn.executemany(
                """
                INSERT INTO intents(
                    run_id, symbol, action, strength, reason, signal_key,
                    target_notional,
                    policy_path, policy_asof, policy_rec, policy_score, policy_trades,
                    policy_enforceable, policy_mult, policy_reco_notional,
                    policy_would_skip, policy_best_exits
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                rows_new,
            )
        except sqlite3.OperationalError:
            # Old schema: only the original columns
            conn.executemany(
                """
                INSERT INTO intents(run_id, symbol, action, strength, reason, signal_key)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                [
                    (run_id, _norm_sym(i.symbol), i.action, i.strength, i.reason, i.signal_key)
                    for i in intents
                ],
            )

    return len(intents)