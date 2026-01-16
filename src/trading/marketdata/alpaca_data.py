from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
import time

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

def fetch_and_store_for_universe(
    days: int = 365,
    universe: str = "sp500",
    limit: int = 0,
    sleep_ms: int = 0,
) -> dict[str, int]:
    """
    Fetch daily bars for symbols in universe_membership.

    - universe: which membership list to use (default sp500)
    - limit: fetch only first N symbols (useful for batching)
    - sleep_ms: optional small delay between symbols to reduce rate-limit risk
    """
    md = AlpacaMarketData()

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    with connect() as conn:
        rows = conn.execute(
            "SELECT symbol FROM universe_membership WHERE universe=? ORDER BY symbol;",
            (universe,),
        ).fetchall()
        syms = [r["symbol"] for r in rows]

    if limit and limit > 0:
        syms = syms[:limit]

    counts: dict[str, int] = {}

    for s in syms:
        s = s.strip().upper()
        try:
            bars = md.fetch_daily_bars(s, start=start, end=end)
            counts[s] = store_daily_bars(s, bars=bars)
        except Exception as e:
            # don't crash the whole batch
            counts[s] = 0
            # if you have log here, use it:
            # log.warning("fetch-bars failed for %s: %s", s, e)
        finally:
            if sleep_ms and sleep_ms > 0:
                time.sleep(sleep_ms / 1000.0)

    return counts