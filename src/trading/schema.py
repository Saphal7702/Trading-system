import sqlite3
from importlib import resources

def apply_schema(conn: sqlite3.Connection) -> None:
    sql = resources.files("trading").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(sql)
    _migrate_runs_regime_cols(conn)


def _migrate_runs_regime_cols(conn: sqlite3.Connection) -> None:
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(runs);").fetchall()}
    for col, typ in [
        ("regime", "TEXT"),
        ("regime_trending_up", "INTEGER"),
        ("regime_momentum_positive", "INTEGER"),
        ("regime_buy_notional_mult", "REAL"),
        ("regime_exposure_cap_mult", "REAL"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {typ};")
