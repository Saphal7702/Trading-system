from __future__ import annotations
import os
from datetime import date, timedelta

import pytest

os.environ.setdefault("TRADING_DB_PATH", "/tmp/_trading_test_placeholder.sqlite")
os.environ.setdefault("TRADING_ENV", "test")

@pytest.fixture
def db(tmp_path, monkeypatch):
    """Fresh SQLite database with schema applied. Returns the db path."""
    db_path = str(tmp_path / "test.sqlite")
    monkeypatch.setenv("TRADING_DB_PATH", db_path)
    from trading.db import init_db
    init_db()
    return db_path

def make_dates(n: int, end: str = "2024-05-01") -> list[str]:
    end_d = date.fromisoformat(end)
    return [(end_d - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]

def insert_bars(db_path: str, symbol: str, prices: list[float], end: str = "2024-05-01") -> None:
    """Insert daily bars for a symbol."""
    from trading.db import connect
    dates = make_dates(len(prices), end)
    with connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO bars_daily(symbol, t, o, h, l, c, v) VALUES (?,?,?,?,?,?,?);",
            [(symbol, d, p, p, p, p, 1_000_000) for d, p in zip(dates, prices)],
        )

def insert_universe(db_path: str, symbol: str, asof: str = "2024-05-01", universe: str = "sp500") -> None:
    """Add a symbol to universe_daily as included."""
    from trading.db import connect
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO universe_daily(asof_date, universe, symbol, include, score) VALUES (?,?,?,1,1.0);",
            (asof, universe, symbol),
        )

def insert_buying_power(db_path: str, buying_power: float) -> None:
    """Insert a broker account row with the given buying power."""
    from trading.db import connect
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO broker_accounts(broker, buying_power, equity, last_synced_at) VALUES ('alpaca',?,?,datetime('now'));",
            (buying_power, buying_power),
        )