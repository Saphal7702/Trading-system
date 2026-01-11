from __future__ import annotations
from .base import Broker
from ..db import connect

def upsert_account(broker: Broker) -> None:
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
            (a.broker, str(a.account_id) if a.account_id is not None else None, a.status, a.currency, a.buying_power, a.equity),
        )

def sync_positions(broker: Broker) -> int:
    positions = broker.list_positions()

    with connect() as conn:
        for p in positions:
            conn.execute(
                """
                INSERT INTO positions(symbol, qty, avg_entry_price, opened_at, last_updated_at)
                VALUES (?, ?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(symbol) DO UPDATE SET
                    qty=excluded.qty,
                  avg_entry_price=excluded.avg_entry_price,
                  last_updated_at=datetime('now');
                """,
                (p.symbol, p.qty, p.avg_entry_price),
            )
    return len(positions)