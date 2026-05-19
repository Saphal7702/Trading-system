from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3
import time
import hashlib
import os
import json
import logging
import math
from typing import Any

from .db import connect
from .cooldown import set_cooldown
from .broker.factory import make_broker
from .utils import env_int, env_float

log = logging.getLogger("trading")

def _is_locked_error(exc: BaseException) -> bool:
    try:
        return "database is locked" in str(exc).lower()
    except Exception:
        return False

def execute_run(
    *,
    run_id: int,
    qty_default: float = 1.0,
    submit: bool = False,
    retry_failed: bool = False,
    allow_buys: bool = True,
    allow_sells: bool = True,
    risk_state: str | None = None,
) -> dict[str, Any]:
    """
    Core execution routine (Phase 2): callable from CLI and runloop.

    Behavior:
      - build_orders_from_intents + persist_orders are idempotent
      - claims rows by flipping status -> 'submitting'
      - retry_failed=True allows resubmitting rows with status='failed'
      - BUY submitted as NOTIONAL
      - SELL sells full current position qty
      - paper-gated via is_paper_submit_allowed()
      - safety rails: daily trade cap, open-order protection, cooldown (planner + execution backup)
      - NEW: execution-layer risk gating (PAUSE_BUYS / SELL_ONLY / HALT_ALL)

    Locking note:
      - keep SQLite write transactions short
      - never hold a DB transaction open across broker/network calls
      - write cooldowns only after the order submit DB transaction is closed
    """
    from .cooldown import is_in_cooldown, set_cooldown  # local import ok
    from .market_calendar import today_ny_str

    def _last_close(conn, symbol: str) -> float | None:
        r = conn.execute(
            """
            SELECT c
            FROM bars_daily
            WHERE symbol = ?
            ORDER BY t DESC
            LIMIT 1;
            """,
            (symbol,),
        ).fetchone()
        if not r:
            return None
        c = r[0]
        return float(c) if c is not None else None

    def _position_qty(conn, symbol: str) -> float:
        r = conn.execute("SELECT qty FROM positions WHERE symbol = ?;", (symbol,)).fetchone()
        if not r or r[0] is None:
            return 0.0
        return float(r[0])

    def _find_open_order_for_symbol(conn, symbol: str, exclude_order_id: int) -> dict | None:
        r = conn.execute(
            """
            SELECT id, side, status, broker_order_id
            FROM orders
            WHERE symbol = ?
              AND id <> ?
              AND status IN ('created', 'submitting', 'submitted')
            ORDER BY id DESC
            LIMIT 1;
            """,
            (symbol, exclude_order_id),
        ).fetchone()
        if not r:
            return None
        return {
            "id": r["id"],
            "side": r["side"],
            "status": r["status"],
            "broker_order_id": r["broker_order_id"],
        }

    def _exposure_by_signal_key(conn) -> dict[str, float]:
        rows = conn.execute(
            """
            SELECT COALESCE(entry_signal_key, 'UNKNOWN') AS k,
                   SUM(COALESCE(entry_notional, 0.0)) AS notional
            FROM positions
            WHERE COALESCE(qty, 0) > 0
            GROUP BY COALESCE(entry_signal_key, 'UNKNOWN');
            """
        ).fetchall()
        out: dict[str, float] = {}
        for r in rows:
            out[str(r["k"])] = float(r["notional"] or 0.0)
        return out

    def _db_call(
        fn,
        *,
        write: bool = False,
        retries: int = 10,
        base_delay: float = 0.35,
        max_delay: float = 2.5,
    ):
        last_err = None
        for attempt in range(retries):
            try:
                with connect() as conn:
                    if write:
                        conn.execute("BEGIN IMMEDIATE;")
                    out = fn(conn)
                    if write:
                        conn.commit()
                    return out
            except sqlite3.OperationalError as e:
                last_err = e
                if not _is_locked_error(e) or attempt == retries - 1:
                    raise
                delay = min(max_delay, base_delay * (2 ** attempt))
                log.warning(
                    "SQLite busy/locked during %s transaction (attempt %s/%s): %s",
                    "write" if write else "read",
                    attempt + 1,
                    retries,
                    e,
                )
                time.sleep(delay)
        if last_err is not None:
            raise last_err

    # ------- sizing knobs (env controlled) -------
    per_position_notional = env_float("TRADING_PER_POSITION", 100.0)
    notional_haircut = env_float("TRADING_NOTIONAL_HAIRCUT", 0.98)
    frac_decimals = int(env_float("TRADING_FRACTIONAL_DECIMALS", 6.0))

    # ------- safety knobs -------
    daily_cap = env_int("TRADING_DAILY_TRADE_CAP", 999999)
    cooldown_days = env_int("TRADING_SYMBOL_COOLDOWN_DAYS", 0)
    sell_cooldown_days = env_int("TRADING_SELL_COOLDOWN_DAYS", 0)
    max_exposure_per_signal = env_float("TRADING_MAX_EXPOSURE_PER_SIGNAL_KEY", 0.0)

    summary: dict[str, Any] = {
        "run_id": run_id,
        "submit": submit,
        "retry_failed": retry_failed,
        "per_position_notional": per_position_notional,
        "notional_haircut": notional_haircut,
        "frac_decimals": frac_decimals,
        "daily_trade_cap": daily_cap,
        "cooldown_days": cooldown_days,
        "asof": None,
        "proposed": 0,
        "inserted": 0,
        "submitted": 0,
        "already_submitted": 0,
        "skipped_reason": None,
        "skipped_due_to_cap": 0,
        "skipped_due_to_open_order": 0,
        "skipped_due_to_cooldown": 0,
        "skipped_due_to_exposure": 0,
        "skipped_due_to_risk_buys": 0,
        "skipped_due_to_risk_sells": 0,
        "risk_state": risk_state,
        "cooldown_set": 0,
        "sell_cooldown_days": sell_cooldown_days,
        "sell_cooldown_set": 0,
        "deferred_due_to_db_lock": 0,
    }

    # 1) Build + persist (idempotent) from intents
    # Phase 6: intent-queue execution (execute pending intents across runs)
    # Toggle with TRADING_EXECUTION_MODE=run|queue (default queue).
    exec_mode = os.getenv("TRADING_EXECUTION_MODE", "queue").strip().lower()
    lookback_days = env_int("TRADING_INTENT_QUEUE_LOOKBACK_DAYS", 7)

    if exec_mode == "run":
        proposed = build_orders_from_intents(run_id=run_id, default_qty=qty_default)
    else:
        proposed = build_orders_from_intent_queue(
            run_id=run_id,
            default_qty=qty_default,
            lookback_days=lookback_days,
        )

    inserted = persist_orders(run_id=run_id, orders=proposed)

    summary["proposed"] = len(proposed)
    summary["inserted"] = inserted

    log.info(
        "Execute(run_id=%s): proposed=%s | inserted_into_db=%s | retry_failed=%s | per_pos=$%.2f | haircut=%.4f | risk_state=%s | allow_buys=%s allow_sells=%s",
        run_id,
        len(proposed),
        inserted,
        retry_failed,
        per_position_notional,
        notional_haircut,
        risk_state,
        bool(allow_buys),
        bool(allow_sells),
    )

    if not proposed:
        log.info("No actionable intents (buy/sell). Nothing to do.")
        return summary

    for o in proposed:
        log.info("PROPOSED %s %s qty=%s | %s", o.side.upper(), o.symbol, o.qty, o.reason)

    if not submit:
        summary["skipped_reason"] = "dry_run"
        log.info("Dry-run only. Use submit=True to send orders (paper-gated).")
        return summary

    trading_mode = os.getenv("TRADING_ENV", "paper").strip().lower()
    if not is_submit_allowed(trading_mode):
        if trading_mode == 'paper':
            summary["skipped_reason"] = "paper_gated"
            log.info("Submission blocked: set TRADING_ALLOW_PAPER_ORDERS=true in .env to allow paper submissions.")
        elif trading_mode == 'live':
            summary["skipped_reason"] = "live_gated"
            log.info("Submission blocked: set TRADING_ALLOW_LIVE_ORDERS=true in .env to allow live submissions.")
        return summary

    eligible_statuses = ("created", "failed") if retry_failed else ("created",)
    placeholders = ",".join("?" for _ in eligible_statuses)

    broker = make_broker()
    submitted = 0

    def _load_run_context(conn):
        run_row = conn.execute(
            "SELECT asof_date FROM runs WHERE id=?;",
            (run_id,),
        ).fetchone()
        asof_local = run_row["asof_date"] if run_row and run_row["asof_date"] else None
        if not asof_local:
            asof_local = today_ny_str()
            try:
                conn.execute("UPDATE runs SET asof_date=? WHERE id=? AND asof_date IS NULL;", (asof_local, run_id))
            except Exception:
                pass

        db_orders_local = conn.execute(
            f"""
            SELECT
                o.id, o.symbol, o.side, o.qty, o.reason, o.idempotency_key, o.status, o.intent_id,
                i.target_notional AS intent_notional,
                i.signal_key AS intent_signal_key,
                i.final_rank AS intent_final_rank
            FROM orders o
            LEFT JOIN intents i
              ON i.id = o.intent_id
            WHERE o.run_id = ?
              AND o.status IN ({placeholders})
            ORDER BY
              CASE o.side WHEN 'sell' THEN 0 ELSE 1 END,
              COALESCE(i.final_rank, 0) DESC,
              o.id ASC;
            """,
            (run_id, *eligible_statuses),
        ).fetchall()

        already_local = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM orders
            WHERE run_id = ?
              AND status IN ('submitted', 'filled');
            """,
            (run_id,),
        ).fetchone()["c"]

        exposure_local = _exposure_by_signal_key(conn) if float(max_exposure_per_signal) > 0.0 else {}
        return asof_local, db_orders_local, int(already_local or 0), exposure_local

    asof, db_orders, already, current_exposure = _db_call(_load_run_context)
    summary["asof"] = asof
    summary["already_submitted"] = already

    cap_enabled = float(max_exposure_per_signal) > 0.0
    planned_exposure: dict[str, float] = {}

    if not db_orders:
        log.info("No DB-eligible orders to submit (statuses=%s).", eligible_statuses)
        log.info("Submitted total=0")
        summary["submitted"] = 0
        return summary

    remaining = daily_cap - summary["already_submitted"]
    if remaining <= 0:
        summary["skipped_reason"] = "daily_trade_cap_reached"
        log.info(
            "Daily trade cap reached before submitting: cap=%s already_submitted=%s",
            daily_cap,
            summary["already_submitted"],
        )
        summary["submitted"] = 0
        return summary

    for row in db_orders:
        order_id = row["id"]
        symbol = (row["symbol"] or "").upper().strip()
        side = (row["side"] or "").lower().strip()
        reason = row["reason"]
        intent_notional = row["intent_notional"] if "intent_notional" in row.keys() else None
        intent_signal_key = row["intent_signal_key"] if "intent_signal_key" in row.keys() else None

        if not symbol or side not in ("buy", "sell"):
            log.info("Skip order id=%s: invalid symbol/side (symbol=%r side=%r)", order_id, symbol, side)
            continue

        def _claim_and_prepare(conn):
            cur = conn.execute(
                f"UPDATE orders SET status='submitting' WHERE id=? AND status IN ({placeholders});",
                (order_id, *eligible_statuses),
            )
            if cur.rowcount != 1:
                return {"action": "already_handled"}

            if side == "buy" and not bool(allow_buys):
                msg = f"risk_gate_buy_blocked state={risk_state or 'UNKNOWN'}"
                conn.execute("UPDATE orders SET status='skipped', reason=? WHERE id=?;", (msg, order_id))
                return {"action": "skipped", "skip_type": "risk_buy", "message": msg}

            if side == "sell" and not bool(allow_sells):
                msg = f"risk_gate_sell_blocked state={risk_state or 'UNKNOWN'}"
                conn.execute("UPDATE orders SET status='skipped', reason=? WHERE id=?;", (msg, order_id))
                return {"action": "skipped", "skip_type": "risk_sell", "message": msg}

            if submitted >= remaining:
                msg = f"daily_trade_cap_reached cap={daily_cap}"
                conn.execute("UPDATE orders SET status='skipped', reason=? WHERE id=?;", (msg, order_id))
                return {"action": "skipped", "skip_type": "daily_cap", "message": msg}

            other = _find_open_order_for_symbol(conn, symbol, exclude_order_id=order_id)
            if other is not None:
                msg = (
                    f"open_order_exists other_id={other['id']} other_side={other['side']} "
                    f"other_status={other['status']} other_broker_order_id={other.get('broker_order_id')}"
                )
                conn.execute("UPDATE orders SET status='skipped', reason=? WHERE id=?;", (msg, order_id))
                return {"action": "skipped", "skip_type": "open_order", "message": msg}

            if side == "buy" and cap_enabled:
                key = (str(intent_signal_key).strip() if intent_signal_key is not None else "").strip() or "UNKNOWN"
                try:
                    base_notional = float(intent_notional) if intent_notional is not None else float(per_position_notional)
                except Exception:
                    base_notional = float(per_position_notional)

                intended = float(base_notional) * float(notional_haircut)
                curr = float(current_exposure.get(key, 0.0))
                planned = float(planned_exposure.get(key, 0.0))

                if (curr + planned + intended) > float(max_exposure_per_signal):
                    msg = (
                        f"exposure_cap_hit key={key} cap={float(max_exposure_per_signal):.2f} "
                        f"curr={curr:.2f} planned={planned:.2f} intended={intended:.2f}"
                    )
                    conn.execute("UPDATE orders SET status='skipped', reason=? WHERE id=?;", (msg, order_id))
                    return {"action": "skipped", "skip_type": "exposure", "message": msg}

                planned_exposure[key] = planned + intended

            if side == "buy":
                close = _last_close(conn, symbol)
                if close is None or close <= 0:
                    raise RuntimeError(f"No close price available to size BUY for {symbol}")

                base_notional = intent_notional if intent_notional is not None else per_position_notional
                try:
                    base_notional = float(base_notional)
                except Exception:
                    base_notional = float(per_position_notional)

                target = float(base_notional) * float(notional_haircut)
                raw_qty = target / close
                scale = 10 ** frac_decimals
                est_qty = math.floor(raw_qty * scale) / scale
                if est_qty <= 0:
                    raise RuntimeError(
                        f"Computed BUY qty too small for {symbol}: close={close} target={target} raw_qty={raw_qty}"
                    )

                return {
                    "action": "submit",
                    "side": side,
                    "symbol": symbol,
                    "reason": reason,
                    "qty_for_execution_row": float(est_qty),
                    "qty_to_persist": float(est_qty),
                    "broker_kwargs": {"symbol": symbol, "side": "buy", "notional": round(target, 2)},
                    "log_msg": (
                        f"ORDER BUY {symbol} target_notional=${target:.2f} close={close:.4f} "
                        f"-> est_qty={est_qty:.6f} | {reason}"
                    ),
                }

            pos_qty = _position_qty(conn, symbol)
            if pos_qty <= 0:
                raise RuntimeError(f"No position qty to SELL for {symbol} (pos_qty={pos_qty})")

            qty_to_send = float(pos_qty)
            return {
                "action": "submit",
                "side": side,
                "symbol": symbol,
                "reason": reason,
                "qty_for_execution_row": qty_to_send,
                "qty_to_persist": qty_to_send,
                "broker_kwargs": {"symbol": symbol, "side": "sell", "qty": qty_to_send},
                "log_msg": f"ORDER SELL {symbol} qty={qty_to_send:.6f} | {reason}",
            }

        try:
            prep = _db_call(_claim_and_prepare, write=True)
        except Exception as e:
            if _is_locked_error(e):
                summary["deferred_due_to_db_lock"] += 1
                log.warning(
                    "Deferred %s %s (order_id=%s): SQLite remained locked during claim/prepare after retries",
                    side.upper(),
                    symbol,
                    order_id,
                )
                continue
            try:
                _db_call(
                    lambda conn: conn.execute(
                        "UPDATE orders SET status='failed', reason=? WHERE id=?;",
                        (str(e), order_id),
                    ),
                    write=True,
                )
            except Exception as write_err:
                log.warning("Could not persist prepare failure for order_id=%s: %s", order_id, write_err)
            log.info("FAILED preparing %s %s: %s", side.upper(), symbol, e)
            continue

        if prep["action"] == "already_handled":
            log.info("Skip %s %s: could not claim order (already handled)", side.upper(), symbol)
            continue

        if prep["action"] == "skipped":
            skip_type = prep.get("skip_type")
            if skip_type == "risk_buy":
                summary["skipped_due_to_risk_buys"] += 1
            elif skip_type == "risk_sell":
                summary["skipped_due_to_risk_sells"] += 1
            elif skip_type == "daily_cap":
                summary["skipped_due_to_cap"] += 1
            elif skip_type == "open_order":
                summary["skipped_due_to_open_order"] += 1
            elif skip_type == "exposure":
                summary["skipped_due_to_exposure"] += 1
            log.info("Skip %s %s (order_id=%s): %s", side.upper(), symbol, order_id, prep.get("message"))
            continue

        if side == "buy" and cooldown_days > 0 and asof and is_in_cooldown(symbol, asof):
            msg = f"cooldown_active asof={asof}"
            _db_call(lambda conn: conn.execute("UPDATE orders SET status='skipped', reason=? WHERE id=?;", (msg, order_id)), write=True)
            summary["skipped_due_to_cooldown"] += 1
            log.info("Skip BUY %s due to cooldown (order_id=%s)", symbol, order_id)
            continue

        log.info(prep["log_msg"])

        try:
            if prep["side"] == "buy":
                bo = broker.place_market_order(
                    prep["broker_kwargs"]["symbol"],
                    prep["broker_kwargs"]["side"],
                    notional=float(prep["broker_kwargs"]["notional"]),
                )
            else:
                bo = broker.place_market_order(
                    prep["broker_kwargs"]["symbol"],
                    prep["broker_kwargs"]["side"],
                    qty=float(prep["broker_kwargs"]["qty"]),
                )
        except Exception as e:
            try:
                _db_call(
                    lambda conn: conn.execute(
                        "UPDATE orders SET status='failed', reason=? WHERE id=?;",
                        (str(e), order_id),
                    ),
                    write=True,
                )
            except Exception as write_err:
                log.warning("Could not persist submit failure for order_id=%s: %s", order_id, write_err)
            log.info("FAILED submitting %s %s: %s", side.upper(), symbol, e)
            continue

        broker_order_id = str(getattr(bo, "id", None))

        def _persist_submission(conn):
            conn.execute("UPDATE orders SET qty=? WHERE id=?;", (float(prep["qty_to_persist"]), order_id))
            conn.execute(
                "UPDATE orders SET status='submitted', broker_order_id=? WHERE id=?;",
                (broker_order_id, order_id),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO executions(broker, broker_order_id, symbol, side, qty, raw_json)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (
                    "alpaca",
                    broker_order_id,
                    symbol,
                    side,
                    float(prep["qty_for_execution_row"]),
                    json.dumps(bo.model_dump(), default=str),
                ),
            )

        try:
            _db_call(_persist_submission, write=True, retries=14, base_delay=0.50, max_delay=3.0)
        except Exception as e:
            log.exception("Order submitted to broker but DB persistence failed for order_id=%s broker_order_id=%s: %s", order_id, broker_order_id, e)
            raise

        try:
            if side == "buy" and broker_order_id and cooldown_days > 0 and asof:
                until = set_cooldown(
                    symbol=symbol,
                    asof=asof,
                    days=cooldown_days,
                    reason="buy_submitted",
                )
                summary["cooldown_set"] += 1
                log.info("Cooldown set: %s until=%s (days=%s)", symbol, until, cooldown_days)

            if side == "sell" and broker_order_id and sell_cooldown_days > 0 and asof:
                until = set_cooldown(
                    symbol=symbol,
                    asof=asof,
                    days=int(sell_cooldown_days),
                    reason="sell_submitted",
                )
                summary["sell_cooldown_set"] += 1
                log.info("Cooldown set (post-sell): %s until=%s (days=%s)", symbol, until, sell_cooldown_days)
        except Exception as e:
            log.warning("Cooldown write failed for %s %s after submission: %s", side.upper(), symbol, e)

        submitted += 1
        log.info("Submitted %s %s -> broker_order_id=%s", side.upper(), symbol, broker_order_id)

    summary["submitted"] = submitted

    log.info(
        "Submitted total=%s | skipped_risk_buys=%s skipped_risk_sells=%s",
        summary["submitted"],
        summary["skipped_due_to_risk_buys"],
        summary["skipped_due_to_risk_sells"],
    )
    return summary

@dataclass(frozen=True)
class ProposedOrder:
    symbol: str
    side: str   # buy/sell
    qty: float
    reason: str
    idempotency_key: str
    # Phase 4: link back to the intent that produced this order
    intent_id: int | None = None


def _today_key() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _make_idempotency_key(symbol: str, side: str, day: str) -> str:
    raw = f"{day}|{symbol}|{side}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_orders_from_intents(run_id: int, default_qty: float = 1.0) -> list[ProposedOrder]:
    """
    Build 0+ ProposedOrders from intents, deduping by (symbol, side) for the day.
    If multiple intents exist for same symbol+side, we keep the most recent one.
    """
    day = _today_key()

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, action, reason, created_at
            FROM intents
            WHERE run_id = ?
              AND action IN ('buy','sell')
            ORDER BY created_at ASC, id ASC;
            """,
            (run_id,),
        ).fetchall()

    # key = (symbol, side) -> keep last
    chosen: dict[tuple[str, str], ProposedOrder] = {}

    for r in rows:
        intent_id = int(r["id"]) if r["id"] is not None else None
        symbol = str(r["symbol"]).strip().upper()
        side = str(r["action"]).strip().lower()
        reason = r["reason"]

        idem = _make_idempotency_key(symbol, side, day)
        chosen[(symbol, side)] = ProposedOrder(
            symbol=symbol,
            side=side,
            qty=default_qty,
            reason=reason,
            idempotency_key=idem,
            intent_id=intent_id,
        )

    return list(chosen.values())



def build_orders_from_intent_queue(
    *,
    run_id: int,
    default_qty: float = 1.0,
    lookback_days: int = 7,
    include_run_id: bool = False,
) -> list[ProposedOrder]:
    """
    Intent-queue execution (Phase 6):
    Build ProposedOrders from *pending* intents regardless of run_id.

    Pending intent definition (conservative):
      - intents.action in ('buy','sell')
      - no order row exists linked via orders.intent_id
      - intents.created_at within lookback window (prevents executing very old intents)

    Notes:
      - We still persist orders under the CURRENT run_id for auditability.
      - intent_id is preserved for attribution (intent knows its original run_id).
      - We dedupe by (symbol, side) and keep the MOST RECENT intent within the window.
    """
    day = _today_key()
    lookback_days = int(lookback_days) if lookback_days is not None else 7
    if lookback_days <= 0:
        lookback_days = 7

    # SQLite datetime modifier like '-7 days'
    modifier = f"-{lookback_days} days"

    with connect() as conn:
        if include_run_id:
            rows = conn.execute(
                """
                SELECT i.id, i.symbol, i.action, i.reason, i.created_at, i.run_id
                FROM intents i
                LEFT JOIN orders o
                  ON o.intent_id = i.id
                WHERE i.action IN ('buy','sell')
                  AND o.id IS NULL
                  AND i.created_at >= datetime('now', ?)
                ORDER BY i.created_at ASC, i.id ASC;
                """,
                (modifier,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT i.id, i.symbol, i.action, i.reason, i.created_at
                FROM intents i
                LEFT JOIN orders o
                  ON o.intent_id = i.id
                WHERE i.action IN ('buy','sell')
                  AND o.id IS NULL
                  AND i.created_at >= datetime('now', ?)
                ORDER BY i.created_at ASC, i.id ASC;
                """,
                (modifier,),
            ).fetchall()

    chosen: dict[tuple[str, str], ProposedOrder] = {}
    for r in rows:
        intent_id = int(r["id"]) if r["id"] is not None else None
        symbol = str(r["symbol"]).strip().upper()
        side = str(r["action"]).strip().lower()
        reason = r["reason"]

        # Per-day idempotency across the order table (symbol+side per day).
        idem = _make_idempotency_key(symbol, side, day)

        # Keep the latest intent for this (symbol, side)
        chosen[(symbol, side)] = ProposedOrder(
            symbol=symbol,
            side=side,
            qty=default_qty,
            reason=reason,
            idempotency_key=idem,
            intent_id=intent_id,
        )

    return list(chosen.values())


def persist_orders(run_id: int, orders: list[ProposedOrder]) -> int:
    """
    Insert into DB with idempotency. If already exists, skip.
    Phase 4: store orders.intent_id for attribution.
    """
    if not orders:
        return 0

    inserted = 0
    with connect() as conn:
        for o in orders:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO orders(
                    run_id, symbol, side, qty, status, reason, idempotency_key, requested_at, intent_id
                )
                VALUES (?,?, ?, ?, 'created', ?, ?, datetime('now'), ?);
                """,
                (run_id, o.symbol, o.side, o.qty, o.reason, o.idempotency_key, o.intent_id),
            )
            if cur.rowcount == 1:
                inserted += 1
            else:
                # Order already exists (idempotency). Backfill intent_id if missing.
                if o.intent_id is not None:
                    conn.execute(
                        """
                        UPDATE orders
                        SET intent_id = ?
                        WHERE run_id = ?
                          AND idempotency_key = ?
                          AND (intent_id IS NULL OR intent_id = 0);
                        """,
                        (o.intent_id, run_id, o.idempotency_key),
                    )
    return inserted

def is_submit_allowed(env: str) -> bool:
    env = (env or "").strip().lower()
    if env == "paper":
        return os.getenv("TRADING_ALLOW_PAPER_ORDERS", "false").strip().lower() in ("1", "true", "yes", "y")
    if env == "live":
        return os.getenv("TRADING_ALLOW_LIVE_ORDERS", "false").strip().lower() in ("1", "true", "yes", "y")
    return False