import sqlite3
from importlib import resources

def apply_schema(conn: sqlite3.Connection) -> None:
    sql = resources.files("trading").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(sql)
    _migrate_runs_regime_cols(conn)
    _migrate_symbol_exclusions(conn)
    _migrate_universe_membership_source(conn)


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


def _migrate_symbol_exclusions(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS symbol_exclusions (
          symbol           TEXT PRIMARY KEY,
          reason_code      TEXT NOT NULL,
          reason_note      TEXT,
          excluded_by      TEXT NOT NULL DEFAULT 'operator',
          excluded_at      TEXT NOT NULL DEFAULT (datetime('now')),
          review_after     TEXT,
          reinstated_at    TEXT,
          reinstated_note  TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_symbol_exclusions_active
        ON symbol_exclusions(symbol)
        WHERE reinstated_at IS NULL;
    """)
    # Additive migration: strategy_scope column (scopes exclusions by strategy)
    existing = {r["name"] for r in conn.execute(
        "PRAGMA table_info(symbol_exclusions);"
    ).fetchall()}
    if "strategy_scope" not in existing:
        conn.execute(
            "ALTER TABLE symbol_exclusions ADD COLUMN strategy_scope TEXT "
            "NOT NULL DEFAULT 'all';"
        )
        # Backfill: MRIT-derived reason codes → 'mrit'; data_anomaly stays 'all'
        conn.execute(
            "UPDATE symbol_exclusions "
            "SET strategy_scope = 'mrit' "
            "WHERE reason_code IN "
            "  ('chronic_loser', 'gap_risk', 'earnings_risk', 'sp500_removal', 'manual') "
            "AND strategy_scope = 'all';"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_symbol_exclusions_scope "
        "ON symbol_exclusions(strategy_scope) WHERE reinstated_at IS NULL;"
    )


def _migrate_universe_membership_source(conn: sqlite3.Connection) -> None:
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(universe_membership);").fetchall()}
    if "source" not in existing:
        conn.execute(
            "ALTER TABLE universe_membership ADD COLUMN source TEXT NOT NULL DEFAULT 'manual';"
        )
