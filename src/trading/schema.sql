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
  last_updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  entry_signal_key TEXT,
  entry_notional REAL
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
  intent_id INTEGER
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
  policy_error TEXT,
  regime TEXT,                       -- bull | pullback | defensive | crash
  regime_trending_up INTEGER,        -- 0/1  close > SMA200
  regime_momentum_positive INTEGER,  -- 0/1  close > SMA50
  regime_buy_notional_mult REAL,     -- sizing throttle applied this run
  regime_exposure_cap_mult REAL      -- exposure cap throttle applied this run
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
  order_id INTEGER
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
  signal_key TEXT,
  target_notional REAL,
  policy_path TEXT,
  policy_asof TEXT,
  policy_rec TEXT,
  policy_score REAL,
  policy_trades INTEGER,
  policy_enforceable INTEGER,      -- 0/1
  policy_mult REAL,
  policy_reco_notional REAL,
  policy_would_skip INTEGER,       -- 0/1
  policy_best_exits TEXT,

  policy_mode TEXT,                -- off|reduce_only|allow_boost
  base_rank REAL,
  policy_rank_adj REAL,
  final_rank REAL,
  base_notional REAL,
  effective_notional REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_idempotency
ON orders(idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_orders_run_id ON orders(run_id);

-- Candidate list: universe membership (e.g., SP500)
CREATE TABLE IF NOT EXISTS universe_membership (
  universe TEXT NOT NULL,          -- 'sp500'
  symbol   TEXT NOT NULL,
  added_at TEXT NOT NULL DEFAULT (datetime('now')),
  source   TEXT NOT NULL DEFAULT 'manual',
  -- 'manual' | 'sp500_new' (added by update-universe-sp500)
  -- | 'sp500_rem' (no longer in S&P 500 per last update-universe-sp500)
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

-- ---- Phase 8: Institutional Risk Layer ----

CREATE TABLE IF NOT EXISTS portfolio_state (
  env TEXT PRIMARY KEY,                       -- 'paper' | 'live'
  state TEXT NOT NULL,                        -- NORMAL | PAUSE_BUYS | SELL_ONLY | HALT_ALL
  reason TEXT,
  set_by TEXT NOT NULL,                       -- system | operator
  set_at TEXT NOT NULL,                       -- ISO timestamp
  expires_at TEXT,                            -- ISO timestamp, optional
  allow_sells INTEGER NOT NULL DEFAULT 1,      -- 1/0
  allow_buys  INTEGER NOT NULL DEFAULT 1,      -- 1/0
  allow_broker INTEGER NOT NULL DEFAULT 1,     -- 1/0
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_limits (
  env TEXT PRIMARY KEY,
  max_dd_pause_buys_pct REAL NOT NULL,        -- e.g. 0.05
  max_dd_sell_only_pct  REAL NOT NULL,        -- e.g. 0.08
  max_dd_halt_all_pct   REAL NOT NULL,        -- e.g. 0.12
  hysteresis_reset_pct  REAL NOT NULL,        -- e.g. 0.03 (resume only if dd <= this)
  min_equity_floor      REAL NOT NULL,        -- optional equity floor (0 disables)
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_peak_reset (
  env TEXT PRIMARY KEY,
  reset_ts TEXT NOT NULL,                     -- ISO timestamp
  reason TEXT,
  actor TEXT NOT NULL,                        -- system | operator
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS risk_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  env TEXT NOT NULL,
  ts TEXT NOT NULL,
  event_type TEXT NOT NULL,                   -- DD_TRIGGER | STATE_CHANGE | OVERRIDE_SET | OVERRIDE_CLEAR | PEAK_RESET ...
  prev_state TEXT,
  new_state TEXT,
  metrics_json TEXT,
  reason TEXT,
  actor TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_risk_events_env_ts
ON risk_events(env, ts);

CREATE TABLE IF NOT EXISTS risk_daily (
  env TEXT NOT NULL,
  date TEXT NOT NULL,                         -- YYYY-MM-DD
  equity REAL NOT NULL,
  peak_equity REAL NOT NULL,
  drawdown_pct REAL NOT NULL,
  state TEXT NOT NULL,
  buys_blocked INTEGER NOT NULL,
  sells_blocked INTEGER NOT NULL,
  broker_blocked INTEGER NOT NULL,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (env, date)
);

CREATE TABLE IF NOT EXISTS symbol_exclusions (
  symbol           TEXT PRIMARY KEY,
  reason_code      TEXT NOT NULL,
  -- valid values: 'chronic_loser' | 'sp500_removal' | 'data_anomaly'
  --               | 'manual' | 'gap_risk' | 'earnings_risk'
  reason_note      TEXT,
  excluded_by      TEXT NOT NULL DEFAULT 'operator',
  -- 'system' (automated) | 'operator' (manual)
  excluded_at      TEXT NOT NULL DEFAULT (datetime('now')),
  review_after     TEXT,        -- YYYY-MM-DD: re-evaluate after this date (nullable)
  reinstated_at    TEXT,        -- set when reinstated, NULL = currently excluded
  reinstated_note  TEXT
);

CREATE INDEX IF NOT EXISTS ix_symbol_exclusions_active
ON symbol_exclusions(symbol)
WHERE reinstated_at IS NULL;

-- Default seeds (safe if already present)
INSERT OR IGNORE INTO portfolio_state(env, state, reason, set_by, set_at, expires_at, allow_sells, allow_buys, allow_broker, updated_at)
VALUES
  ('paper', 'NORMAL', 'seed', 'system', datetime('now'), NULL, 1, 1, 1, datetime('now')),
  ('live',  'NORMAL', 'seed', 'system', datetime('now'), NULL, 1, 1, 1, datetime('now'));

INSERT OR IGNORE INTO risk_limits(env, max_dd_pause_buys_pct, max_dd_sell_only_pct, max_dd_halt_all_pct, hysteresis_reset_pct, min_equity_floor, updated_at)
VALUES
  ('paper', 0.05, 0.08, 0.12, 0.03, 0.0, datetime('now')),
  ('live',  0.05, 0.08, 0.12, 0.03, 0.0, datetime('now'));
