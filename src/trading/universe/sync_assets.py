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
    # Deliberately bypass get_all_assets()'s pydantic Asset model: Alpaca has
    # added exchange codes (e.g. TXSE) that older alpaca-py AssetExchange
    # enums don't know about, which makes the typed call raise a
    # ValidationError for the *entire* response. We only need 5 plain
    # fields, so hit the same endpoint and take the raw JSON directly.
    assets = broker.client.get("/assets", req.to_request_fields())

    n = 0
    with connect() as conn:
        for d in assets:  # raw dicts from the API, not alpaca-py models
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
