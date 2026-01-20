PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS symbols (
  symbol TEXT PRIMARY KEY,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS positions (
  symbol TEXT PRIMARY KEY,
  qty REAL NOT NULL,
  avg_entry_price REAL,
  opened_at TEXT NOT NULL,
  last_updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('buy','sell')),
  qty REAL NOT NULL,
  requested_at TEXT NOT NULL DEFAULT (datetime('now')),
  status TEXT NOT NULL DEFAULT 'created',
  reason TEXT,
  idempotency_key TEXT,
  broker_order_id TEXT,
  run_id INTEGER,
  filled_qty REAL,
  filled_avg_price REAL,
  filled_at TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  notes TEXT,
  asof_date TEXT
);

CREATE TABLE IF NOT EXISTS broker_accounts (
  broker TEXT PRIMARY KEY,
  account_id TEXT,
  status TEXT,
  currency TEXT,
  buying_power REAL,
  equity REAL,
  last_synced_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS executions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  broker TEXT NOT NULL,
  broker_order_id TEXT,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('buy','sell')),
  qty REAL NOT NULL,
  filled_avg_price REAL,
  filled_at TEXT,
  raw_json TEXT
);

CREATE TABLE IF NOT EXISTS bars_daily (
  symbol TEXT NOT NULL,
  t TEXT NOT NULL,  
  o REAL,
  h REAL,
  l REAL,
  c REAL,
  v REAL,
  PRIMARY KEY(symbol, t)
);

CREATE TABLE IF NOT EXISTS intents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('buy','sell','hold')),
  strength REAL,
  reason TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_idempotency
ON orders(idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_executions_broker_order
ON executions(broker, broker_order_id);

CREATE INDEX IF NOT EXISTS ix_orders_run_id ON orders(run_id);

-- Candidate list: universe membership (e.g., SP500)
CREATE TABLE IF NOT EXISTS universe_membership (
  universe TEXT NOT NULL,          -- 'sp500'
  symbol   TEXT NOT NULL,
  added_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (universe, symbol)
);

-- Alpaca asset metadata cache
CREATE TABLE IF NOT EXISTS assets_cache (
  symbol       TEXT PRIMARY KEY,
  tradable     INTEGER NOT NULL,
  fractionable INTEGER NOT NULL,
  status       TEXT,
  exchange     TEXT,
  updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Daily computed snapshot for selection
CREATE TABLE IF NOT EXISTS universe_daily (
  asof_date  TEXT NOT NULL,        -- 'YYYY-MM-DD'
  universe   TEXT NOT NULL,         -- 'sp500'
  symbol     TEXT NOT NULL,
  close      REAL,
  adv20      REAL,                 -- avg dollar volume 20d
  ret60      REAL,                 -- 60d return
  include    INTEGER NOT NULL,      -- 1/0
  reason     TEXT,
  score      REAL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (asof_date, universe, symbol)
);

CREATE INDEX IF NOT EXISTS ix_universe_daily_lookup
ON universe_daily(asof_date, universe, include, score DESC);

-- Prevent duplicate logical orders (idempotency)
CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_idempotency
ON orders(idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_exec_broker_order
ON executions(broker, broker_order_id)
WHERE broker_order_id IS NOT NULL;