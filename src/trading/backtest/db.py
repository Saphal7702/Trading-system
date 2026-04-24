from __future__ import annotations
import sqlite3
from pathlib import Path
 
BACKTEST_DB_PATH = "../TradingData/backtest.sqlite"
 
def connect_backtest(db_path: str | None = None) -> sqlite3.Connection:
    """
    Open (or create) the backtest SQLite database.
    """
    path = Path(db_path or BACKTEST_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn
 
def init_backtest_db(db_path: str | None = None) -> None:
    """Create all tables (if not exists) from schema.sql."""
    conn = connect_backtest(db_path)
    schema = (Path(__file__).parent / "schema.sql").read_text()
    conn.executescript(schema)
    conn.commit()
    conn.close()