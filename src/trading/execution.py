from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
import json
import logging
import math
from typing import Any

from .db import connect
from .cooldown import set_cooldown
from .broker.alpaca_broker import AlpacaPaperBroker

log = logging.getLogger("trading")

def execute_run(
    *,
    run_id: int,
    qty_default: float = 1.0,
    submit: bool = False,
    retry_failed: bool = False,
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
    """
    from .cooldown import is_in_cooldown, set_cooldown  # local import ok

    def _env_float(name: str, default: float) -> float:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        try:
            return float(v)
        except Exception:
            return float(default)

    def _env_int(name: str, default: int) -> int:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        try:
            return int(float(v))
        except Exception:
            return int(default)

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

    # ------- sizing knobs (env controlled) -------
    per_position_notional = _env_float("TRADING_PER_POSITION", 100.0)
    notional_haircut = _env_float("TRADING_NOTIONAL_HAIRCUT", 0.98)
    frac_decimals = int(_env_float("TRADING_FRACTIONAL_DECIMALS", 6.0))

    # ------- safety knobs -------
    daily_cap = _env_int("TRADING_DAILY_TRADE_CAP", 999999)
    cooldown_days = _env_int("TRADING_SYMBOL_COOLDOWN_DAYS", 0)

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
        "cooldown_set": 0,
    }

    # 1) Build + persist (idempotent) from intents
    proposed = build_orders_from_intents(run_id=run_id, default_qty=qty_default)
    inserted = persist_orders(run_id=run_id, orders=proposed)

    summary["proposed"] = len(proposed)
    summary["inserted"] = inserted

    log.info(
        "Execute(run_id=%s): proposed=%s | inserted_into_db=%s | retry_failed=%s | per_pos=$%.2f | haircut=%.4f",
        run_id,
        len(proposed),
        inserted,
        retry_failed,
        per_position_notional,
        notional_haircut,
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

    if not is_paper_submit_allowed():
        summary["skipped_reason"] = "paper_gated"
        log.info("Submission blocked: set TRADING_ALLOW_PAPER_ORDERS=true in .env to allow paper submissions.")
        return summary

    eligible_statuses = ("created", "failed") if retry_failed else ("created",)
    placeholders = ",".join("?" for _ in eligible_statuses)

    broker = AlpacaPaperBroker()
    submitted = 0

    with connect() as conn:
        # Resolve asof_date for this run (used for cooldown)
        run_row = conn.execute(
            "SELECT asof_date FROM runs WHERE id=?;",
            (run_id,),
        ).fetchone()
        asof = run_row["asof_date"] if run_row and run_row["asof_date"] else None
        summary["asof"] = asof

        if cooldown_days > 0 and not asof:
            log.info(
                "Cooldown enabled but runs.asof_date is NULL for run_id=%s; cooldown will not be set/enforced in execution.",
                run_id,
            )

        # 2) Submit ONLY DB-eligible orders (DB is source of truth)
        db_orders = conn.execute(
            f"""
            SELECT id, symbol, side, qty, reason, idempotency_key, status
            FROM orders
            WHERE run_id = ?
              AND status IN ({placeholders})
            ORDER BY id ASC;
            """,
            (run_id, *eligible_statuses),
        ).fetchall()

        if not db_orders:
            log.info("No DB-eligible orders to submit (statuses=%s).", eligible_statuses)
            log.info("Submitted total=0")
            summary["submitted"] = 0
            return summary

        already = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM orders
            WHERE run_id = ?
              AND status IN ('submitted', 'filled');
            """,
            (run_id,),
        ).fetchone()["c"]

        summary["already_submitted"] = int(already or 0)

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

            if not symbol or side not in ("buy", "sell"):
                log.info("Skip order id=%s: invalid symbol/side (symbol=%r side=%r)", order_id, symbol, side)
                continue

            # Atomically claim this DB order (prevents concurrent submits)
            cur = conn.execute(
                f"UPDATE orders SET status='submitting' WHERE id=? AND status IN ({placeholders});",
                (order_id, *eligible_statuses),
            )
            if cur.rowcount != 1:
                log.info("Skip %s %s: could not claim order (already handled)", side.upper(), symbol)
                continue

            # Enforce daily trade cap (after claim for concurrency safety)
            if submitted >= remaining:
                conn.execute(
                    "UPDATE orders SET status='skipped', reason=? WHERE id=?;",
                    (f"daily_trade_cap_reached cap={daily_cap}", order_id),
                )
                summary["skipped_due_to_cap"] += 1
                log.info(
                    "Skip order id=%s due to daily trade cap (cap=%s already=%s submitted_now=%s)",
                    order_id,
                    daily_cap,
                    summary["already_submitted"],
                    submitted,
                )
                continue

            # Open-order protection: do not submit if any other open-ish internal order exists for this symbol
            other = _find_open_order_for_symbol(conn, symbol, exclude_order_id=order_id)
            if other is not None:
                msg = (
                    f"open_order_exists other_id={other['id']} other_side={other['side']} "
                    f"other_status={other['status']} other_broker_order_id={other.get('broker_order_id')}"
                )
                conn.execute("UPDATE orders SET status='skipped', reason=? WHERE id=?;", (msg, order_id))
                summary["skipped_due_to_open_order"] += 1
                log.info("Skip %s %s (order_id=%s): %s", side.upper(), symbol, order_id, msg)
                continue

            # Execution-side cooldown (backup safety)
            if side == "buy" and cooldown_days > 0 and asof and is_in_cooldown(symbol, asof):
                msg = f"cooldown_active asof={asof}"
                conn.execute("UPDATE orders SET status='skipped', reason=? WHERE id=?;", (msg, order_id))
                summary["skipped_due_to_cooldown"] += 1
                log.info("Skip BUY %s due to cooldown (order_id=%s)", symbol, order_id)
                continue

            try:
                qty_for_execution_row: float = 0.0  # for executions.qty

                if side == "buy":
                    close = _last_close(conn, symbol)
                    if close is None or close <= 0:
                        raise RuntimeError(f"No close price available to size BUY for {symbol}")

                    target = float(per_position_notional) * float(notional_haircut)

                    # Estimate qty for logging + executions table (broker will calculate actual fill qty)
                    raw_qty = target / close
                    scale = 10 ** frac_decimals
                    est_qty = math.floor(raw_qty * scale) / scale
                    if est_qty <= 0:
                        raise RuntimeError(
                            f"Computed BUY qty too small for {symbol}: close={close} target={target} raw_qty={raw_qty}"
                        )

                    qty_for_execution_row = float(est_qty)

                    log.info(
                        "ORDER BUY %s target_notional=$%.2f close=%.4f -> est_qty=%.6f | %s",
                        symbol,
                        target,
                        close,
                        est_qty,
                        reason,
                    )

                    # Submit as NOTIONAL
                    bo = broker.place_market_order(symbol, "buy", notional=float(target))

                    # Store est_qty for visibility (note: only an estimate)
                    try:
                        conn.execute("UPDATE orders SET qty=? WHERE id=?;", (float(est_qty), order_id))
                    except Exception:
                        pass

                else:  # sell
                    pos_qty = _position_qty(conn, symbol)
                    if pos_qty <= 0:
                        raise RuntimeError(f"No position qty to SELL for {symbol} (pos_qty={pos_qty})")

                    qty_to_send = float(pos_qty)
                    qty_for_execution_row = qty_to_send

                    conn.execute("UPDATE orders SET qty=? WHERE id=?;", (qty_to_send, order_id))
                    log.info("ORDER SELL %s qty=%.6f | %s", symbol, qty_to_send, reason)

                    bo = broker.place_market_order(symbol, "sell", qty=float(qty_to_send))

                broker_order_id = str(getattr(bo, "id", None))

                # Save broker order id on orders row
                conn.execute(
                    "UPDATE orders SET status='submitted', broker_order_id=? WHERE id=?;",
                    (broker_order_id, order_id),
                )

                # Set cooldown only for BUY, only if broker_order_id is present, and only if asof is known
                if side == "buy" and broker_order_id and cooldown_days > 0 and asof:
                    until = set_cooldown(
                        symbol=symbol,
                        asof=asof,
                        days=cooldown_days,
                        reason="buy_submitted",
                    )
                    summary["cooldown_set"] += 1
                    log.info("Cooldown set: %s until=%s (days=%s)", symbol, until, cooldown_days)

                # Save execution snapshot (raw_json keeps broker response)
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
                        float(qty_for_execution_row),
                        json.dumps(bo.model_dump(), default=str),
                    ),
                )

                submitted += 1
                log.info("Submitted %s %s -> broker_order_id=%s", side.upper(), symbol, broker_order_id)

            except Exception as e:
                conn.execute("UPDATE orders SET status='failed', reason=? WHERE id=?;", (str(e), order_id))
                log.info("FAILED submitting %s %s: %s", side.upper(), symbol, e)

        summary["submitted"] = submitted

    log.info("Submitted total=%s", summary["submitted"])
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


def is_paper_submit_allowed() -> bool:
    return os.getenv("TRADING_ALLOW_PAPER_ORDERS", "false").strip().lower() in ("1", "true", "yes", "y")