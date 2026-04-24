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
    exit_reason  TEXT
);

CREATE TABLE IF NOT EXISTS backtest_equity_curve (
    run_id  INTEGER NOT NULL REFERENCES backtest_runs(id),
    date    TEXT    NOT NULL,
    equity  REAL    NOT NULL,
    cash    REAL    NOT NULL,
    PRIMARY KEY (run_id, date)
);
