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
  filled_at TEXT,
  COLUMN intent_id INTEGER
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  notes TEXT,
  asof_date TEXT,
  reason TEXT,
  summary_json TEXT,
  policy_path TEXT,
  policy_asof TEXT,
  policy_loaded INTEGER DEFAULT 0,
  policy_error TEXT
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
  raw_json TEXT,
  COLUMN order_id INTEGER
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
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  COLUMN signal_key TEXT,
  COLUMN target_notional REAL,
  COLUMN policy_path TEXT,
  COLUMN policy_asof TEXT,
  COLUMN policy_rec TEXT,
  COLUMN policy_score REAL,
  COLUMN policy_trades INTEGER,
  COLUMN policy_enforceable INTEGER,      -- 0/1
  COLUMN policy_mult REAL,
  COLUMN policy_reco_notional REAL,
  COLUMN policy_would_skip INTEGER,       -- 0/1
  COLUMN policy_best_exits TEXT

  COLUMN policy_mode TEXT,                -- off|reduce_only|allow_boost
  COLUMN base_rank REAL,
  COLUMN policy_rank_adj REAL,
  COLUMN final_rank REAL,
  COLUMN base_notional REAL,
  COLUMN effective_notional REAL
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

CREATE TABLE IF NOT EXISTS symbol_cooldowns (
  symbol TEXT PRIMARY KEY,
  cooldown_until TEXT NOT NULL,
  reason TEXT,
  set_at TEXT NOT NULL DEFAULT (datetime('now'))
);


CREATE INDEX IF NOT EXISTS ix_universe_daily_lookup
ON universe_daily(asof_date, universe, include, score DESC);

CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_idempotency
ON orders(idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_exec_broker_order
ON executions(broker, broker_order_id)
WHERE broker_order_id IS NOT NULL;

-- ---- Phase 3: P&L / lots ----

CREATE TABLE IF NOT EXISTS position_lots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  qty_open REAL NOT NULL,
  entry_price REAL NOT NULL,
  entry_filled_at TEXT NOT NULL,
  entry_execution_id INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(entry_execution_id) REFERENCES executions(id)
);

CREATE INDEX IF NOT EXISTS ix_position_lots_symbol
ON position_lots(symbol);

CREATE INDEX IF NOT EXISTS ix_position_lots_symbol_entry_time
ON position_lots(symbol, entry_filled_at);


CREATE TABLE IF NOT EXISTS lot_closings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  lot_id INTEGER NOT NULL,
  qty_closed REAL NOT NULL,
  entry_price REAL NOT NULL,
  exit_price REAL NOT NULL,
  realized_pnl REAL NOT NULL,
  entry_filled_at TEXT NOT NULL,
  exit_filled_at TEXT NOT NULL,
  entry_execution_id INTEGER NOT NULL,
  exit_execution_id INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(lot_id) REFERENCES position_lots(id),
  FOREIGN KEY(entry_execution_id) REFERENCES executions(id),
  FOREIGN KEY(exit_execution_id) REFERENCES executions(id)
);

CREATE INDEX IF NOT EXISTS ix_lot_closings_symbol_exit_time
ON lot_closings(symbol, exit_filled_at);

CREATE INDEX IF NOT EXISTS ix_lot_closings_exit_execution
ON lot_closings(exit_execution_id);


CREATE TABLE IF NOT EXISTS account_snapshots_daily (
  asof_date TEXT PRIMARY KEY,  -- YYYY-MM-DD
  cash REAL NOT NULL,
  equity REAL NOT NULL,
  buying_power REAL,
  long_market_value REAL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  run_id INTEGER,
  FOREIGN KEY(run_id) REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS ix_orders_intent_id ON orders(intent_id);
CREATE INDEX IF NOT EXISTS ix_exec_order_id ON executions(order_id);
CREATE INDEX IF NOT EXISTS ix_intents_signal_key ON intents(signal_key);
