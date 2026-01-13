from __future__ import annotations
import json

from ..db import connect
from .alpaca_broker import AlpacaPaperBroker

def sync_orders(limit: int = 200) -> dict[str, int]:
    b = AlpacaPaperBroker()
    orders = b.list_recent_orders(status="all", limit=limit)

    upserted_exec = 0
    updated_orders = 0

    with connect() as conn:
        for o in orders:
            od = o.model_dump()
            broker_order_id = str(od.get("id") or "")
            if not broker_order_id:
                continue

            symbol = (od.get("symbol") or "").upper()
            side = (od.get("side") or "").lower()
            status = (od.get("status") or "").lower()

            filled_qty = od.get("filled_qty")
            filled_avg_price = od.get("filled_avg_price")
            filled_at = od.get("filled_at")

            # 1) Upsert execution row (audit trail)
            conn.execute(
                """
                INSERT OR REPLACE INTO executions(
                  broker, broker_order_id, symbol, side, qty, filled_avg_price, filled_at, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    "alpaca",
                    broker_order_id,
                    symbol,
                    side,
                    float(filled_qty) if filled_qty not in (None, "") else 0.0,
                    float(filled_avg_price) if filled_avg_price not in (None, "") else None,
                    str(filled_at) if filled_at else None,
                    json.dumps(od, default=str),
                ),
            )
            upserted_exec += 1

            # 2) Update our internal orders row (exact match by broker_order_id)
            row = conn.execute(
                "SELECT id, status FROM orders WHERE broker_order_id=?;",
                (broker_order_id,),
            ).fetchone()

            if row and row["status"] != status:
                conn.execute(
                    "UPDATE orders SET status=? WHERE id=?;",
                    (status, row["id"]),
                )
                updated_orders += 1

    return {"executions_upserted": upserted_exec, "orders_updated": updated_orders}