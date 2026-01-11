import argparse
import logging
from .logging_setup import setup_logging
from .config import get_settings
from .db import init_db, connect
from datetime import datetime, timedelta, timezone
from .compliance import can_sell
from .universe import load_watchlist_csv
from .runloop import run_once
from trading.broker.alpaca_broker import AlpacaPaperBroker
from trading.broker.sync import upsert_account, sync_positions


log = logging.getLogger("trading")

def cmd_healthcheck() -> int:
    s = get_settings()
    log.info("Env: %s", s.env)
    log.info("DB path: %s", s.db_path)

    init_db()

    with connect() as conn:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
        tables = [r["name"] for r in cur.fetchall()]

    log.info("DB OK. Tables: %s", ", ".join(tables))
    return 0


def cmd_compliance_test() -> int:
    now = datetime.now(timezone.utc)

    opened_recent = (now - timedelta(hours=12)).isoformat()
    d1 = can_sell(opened_recent, now=now)
    log.info("Opened 12h ago -> allowed=%s | %s", d1.allowed, d1.reason)

    opened_old = (now - timedelta(days=2)).isoformat()
    d2 = can_sell(opened_old, now=now)
    log.info("Opened 2d ago  -> allowed=%s | %s", d2.allowed, d2.reason)

    return 0

def cmd_load_watchlist(csv_path: str) -> int:
    n = load_watchlist_csv(csv_path)
    log.info("Loaded/updated %s symbols from %s", n, csv_path)
    return 0

def cmd_run(notes: str) -> int:
    res = run_once(notes=notes or None)
    log.info("Run completed. run_id=%s", res.run_id)
    return 0

def cmd_status() -> int:
    from .db import connect
    with connect() as conn:
        sym = conn.execute("SELECT COUNT(*) AS c FROM symbols;").fetchone()["c"]
        runs = conn.execute("SELECT COUNT(*) AS c FROM runs;").fetchone()["c"]
        pos = conn.execute("SELECT COUNT(*) AS c FROM positions;").fetchone()["c"]
        ords = conn.execute("SELECT COUNT(*) AS c FROM orders;").fetchone()["c"]

    log.info("symbols=%s | runs=%s | positions=%s | orders=%s", sym, runs, pos, ords)
    return 0

def cmd_broker_check() -> int:
    b = AlpacaPaperBroker()
    upsert_account(b)
    a = b.get_account()
    log.info("Broker OK: %s | status=%s | buying_power=%s | equity=%s", a.broker, a.status, a.buying_power, a.equity)
    return 0

def cmd_sync_positions() -> int:
    b = AlpacaPaperBroker()
    n = sync_positions(b)
    log.info("Synced %s positions from %s", n, b.name)
    return 0

def cmd_fetch_bars(days: int) -> int:
    from .marketdata.alpaca_data import fetch_and_store_for_universe
    counts = fetch_and_store_for_universe(days=days)
    total = sum(counts.values())
    log.info("Fetched bars: total=%s | per_symbol=%s", total, counts)
    return 0

def main() -> int:
    setup_logging()

    p = argparse.ArgumentParser(prog="trading", description="Personal trading system (Phase 1)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("healthcheck", help="Verify config + initialize DB schema")
    sub.add_parser("compliance-test")

    p_watch = sub.add_parser("load-watchlist", help="Load symbols from a watchlist CSV into the DB")
    p_watch.add_argument("--csv", default="data/watchlist.csv", help="Path to watchlist CSV (default: data/watchlist.csv)")

    p_run = sub.add_parser("run", help="Execute one trading cycle (Phase 1 skeleton)")
    p_run.add_argument("--notes", default="", help="Optional notes to store in runs table")

    sub.add_parser("status", help="Show quick DB status counts")

    sub.add_parser("broker-check", help="Check broker connection and store account snapshot")
    sub.add_parser("sync-positions", help="Sync broker positions into SQLite")

    p_fb = sub.add_parser("fetch-bars", help="Fetch and store daily OHLCV bars for active symbols")
    p_fb.add_argument("--days", type=int, default=365, help="Lookback days (default 365)")


    args = p.parse_args()

    if args.cmd == "healthcheck":
        return cmd_healthcheck()
    
    if args.cmd == "compliance-test":
        return cmd_compliance_test()
    
    if args.cmd == "load-watchlist":
        return cmd_load_watchlist(args.csv)
        
    if args.cmd == "run":
        return cmd_run(args.notes)
    
    if args.cmd == "status":
        return cmd_status()
    
    if args.cmd == "broker-check":
        return cmd_broker_check()

    if args.cmd == "sync-positions":
        return cmd_sync_positions()
    
    if args.cmd == "fetch-bars":
        return cmd_fetch_bars(args.days)

    return 1