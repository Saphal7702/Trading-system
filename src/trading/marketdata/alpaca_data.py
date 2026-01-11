from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from ..db import connect

class AlpacaMarketData:
    def __init__(self) -> None:
        key = os.getenv("ALPACA_API_KEY","").strip()
        secret = os.getenv("ALPACA_API_SECRET","").strip()

        if not key or not secret:
            raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_API_SECRET in .env")
        
        self.client = StockHistoricalDataClient(api_key=key, secret_key=secret)

    def fetch_daily_bars(self, symbol: str, start: datetime, end: datetime) -> list[dict]:
        feed = os.getenv("TRADING_DATA_FEED", "iex").strip().lower()
        req = StockBarsRequest(
            symbol_or_symbols = symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            adjustment="raw",
            feed=feed,
        )

        bars = self.client.get_stock_bars(req)

        out = []

        for b in bars[symbol]:
            out.append(
                {
                    "t": b.timestamp.astimezone(timezone.utc).date().isoformat(),
                    "o": float(b.open),
                    "h": float(b.high),
                    "l": float(b.low),
                    "c": float(b.close),
                    "v": float(b.volume),
                }
            )

        return out
    
def store_daily_bars(symbol: str, bars: list[dict]) -> int:
    if not bars:
        return 0
    
    with connect() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO bars_daily(symbol, t, o, h, l, c, v)
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            [(symbol, b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]) for b in bars]
        )
    
    return len(bars)

def fetch_and_store_for_universe(days: int = 365) -> dict[str, int]:
    md = AlpacaMarketData()

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    with connect() as conn:
        rows = conn.execute("SELECT symbol FROM symbols WHERE is_active=1 ORDER BY symbol;").fetchall()
        syms = [r["symbol"] for r in rows]

    counts: dict[str, int] = {}

    for s in syms:
        bars = md.fetch_daily_bars(s, start=start, end=end)
        counts[s] = store_daily_bars(s, bars=bars)

    return counts