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
  idempotency_key TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  notes TEXT
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