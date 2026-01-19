from __future__ import annotations
from .base import Broker
from ..db import connect

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
        # (This prevents stale positions lingering forever.)
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
            # Broker returned no positions: mark everything as qty=0
            conn.execute(
                "UPDATE positions SET qty=0, last_updated_at=datetime('now');"
            )

    updated = backfill_opened_at_from_fills()
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