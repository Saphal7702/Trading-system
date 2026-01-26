from __future__ import annotations
from .base import Broker
from ..db import connect


def upsert_account(broker: Broker) -> None:
    """
    Snapshot broker account into broker_accounts (1 row per broker).
    """
    a = broker.get_account()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO broker_accounts (broker, account_id, status, currency, buying_power, equity, last_synced_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(broker) DO UPDATE SET
                account_id=excluded.account_id,
                status=excluded.status,
                currency=excluded.currency,
                buying_power=excluded.buying_power,
                equity=excluded.equity,
                last_synced_at=datetime('now');
            """,
            (
                a.broker,
                str(a.account_id) if a.account_id is not None else None,
                a.status,
                a.currency,
                float(a.buying_power) if a.buying_power is not None else None,
                float(a.equity) if a.equity is not None else None,
            ),
        )


def sync_positions(broker: Broker) -> int:
    positions = broker.list_positions()
    seen = {p.symbol.strip().upper() for p in positions}

    with connect() as conn:
        # Upsert live positions
        for p in positions:
            sym = p.symbol.strip().upper()
            qty = float(p.qty)
            avg = float(p.avg_entry_price) if p.avg_entry_price is not None else None

            conn.execute(
                """
                INSERT INTO positions(symbol, qty, avg_entry_price, opened_at, last_updated_at)
                VALUES (?, ?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(symbol) DO UPDATE SET
                  qty=excluded.qty,
                  avg_entry_price=excluded.avg_entry_price,
                  last_updated_at=datetime('now');
                """,
                (sym, qty, avg),
            )

        # Mark any previously-known positions that are no longer returned as closed (qty=0)
        if seen:
            placeholders = ",".join("?" for _ in seen)
            conn.execute(
                f"""
                UPDATE positions
                SET qty=0, last_updated_at=datetime('now')
                WHERE symbol NOT IN ({placeholders});
                """,
                tuple(seen),
            )
        else:
            conn.execute("UPDATE positions SET qty=0, last_updated_at=datetime('now');")

    updated = backfill_opened_at_from_fills()
    linked = link_executions_to_orders()
    return len(positions)


def backfill_opened_at_from_fills() -> int:
    """
    Set positions.opened_at from the most recent BUY execution filled_at per symbol.
    This is conservative for compliance (if you buy again, hold timer resets).
    """
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE positions
            SET opened_at = (
                SELECT e.filled_at
                FROM executions e
                WHERE e.symbol = positions.symbol
                  AND e.side = 'buy'
                  AND e.filled_at IS NOT NULL
                ORDER BY e.filled_at DESC
                LIMIT 1
            )
            WHERE qty > 0
              AND EXISTS (
                SELECT 1
                FROM executions e
                WHERE e.symbol = positions.symbol
                  AND e.side = 'buy'
                  AND e.filled_at IS NOT NULL
              );
            """
        )
        return cur.rowcount


def link_executions_to_orders() -> int:
    """
    Phase 4 attribution backfill:
      executions.order_id := orders.id by matching broker_order_id.

    Safe to run repeatedly; only fills NULL order_id rows.
    Works even if executions are inserted as snapshots first and enriched later.
    """
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE executions
            SET order_id = (
                SELECT o.id
                FROM orders o
                WHERE o.broker_order_id = executions.broker_order_id
                LIMIT 1
            )
            WHERE order_id IS NULL
              AND broker_order_id IS NOT NULL
              AND EXISTS (
                SELECT 1
                FROM orders o
                WHERE o.broker_order_id = executions.broker_order_id
              );
            """
        )
        return cur.rowcount