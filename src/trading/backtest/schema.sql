CREATE TABLE IF NOT EXISTS backtest_runs (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy              TEXT    NOT NULL,
    start_date            TEXT    NOT NULL,
    end_date              TEXT    NOT NULL,
    universe              TEXT    NOT NULL,
    initial_capital       REAL    NOT NULL,
    max_positions         INTEGER NOT NULL,
    per_position_notional REAL    NOT NULL,
    created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    summary_json          TEXT
);

CREATE TABLE IF NOT EXISTS backtest_trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES backtest_runs(id),
    symbol       TEXT    NOT NULL,
    entry_date   TEXT    NOT NULL,
    exit_date    TEXT,
    entry_price  REAL    NOT NULL,
    exit_price   REAL,
    qty          REAL    NOT NULL,
    realized_pnl REAL,
    return_pct   REAL,
    exit_reason  TEXT,
    signal_key   TEXT
);

CREATE TABLE IF NOT EXISTS backtest_equity_curve (
    run_id  INTEGER NOT NULL REFERENCES backtest_runs(id),
    date    TEXT    NOT NULL,
    equity  REAL    NOT NULL,
    cash    REAL    NOT NULL,
    PRIMARY KEY (run_id, date)
);

CREATE TABLE IF NOT EXISTS bars_daily (
    symbol  TEXT    NOT NULL,
    t       TEXT    NOT NULL,   -- YYYY-MM-DD
    o       REAL,
    h       REAL,
    l       REAL,
    c       REAL    NOT NULL,
    v       REAL,
    source  TEXT    NOT NULL DEFAULT 'yfinance',
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (symbol, t)
);
 
CREATE INDEX IF NOT EXISTS idx_bars_daily_symbol_t ON bars_daily (symbol, t);