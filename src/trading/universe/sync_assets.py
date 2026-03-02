from alpaca.trading.requests import GetAssetsRequest
from alpaca.trading.enums import AssetClass, AssetStatus

from ..db import connect
from ..broker.factory import make_broker

def sync_assets_cache() -> int:
    broker = make_broker()

    req = GetAssetsRequest(
        asset_class=AssetClass.US_EQUITY,
        status=AssetStatus.ACTIVE,
    )
    assets = broker.client.get_all_assets(req)

    n = 0
    with connect() as conn:
        for a in assets:
            d = a.model_dump()  # alpaca-py models
            symbol = (d.get("symbol") or "").strip().upper()
            if not symbol:
                continue

            # Keep only what we care about for $500 trading
            tradable = 1 if d.get("tradable") else 0
            fractionable = 1 if d.get("fractionable") else 0

            # Optional metadata (useful later)
            status = d.get("status")
            exchange = d.get("exchange")

            conn.execute(
                """
                INSERT INTO assets_cache(symbol, tradable, fractionable, status, exchange, updated_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(symbol) DO UPDATE SET
                  tradable=excluded.tradable,
                  fractionable=excluded.fractionable,
                  status=excluded.status,
                  exchange=excluded.exchange,
                  updated_at=datetime('now');
                """,
                (symbol, tradable, fractionable, status, exchange),
            )
            n += 1

    return n
