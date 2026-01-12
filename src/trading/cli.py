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

def cmd_plan(fast: int, slow: int) -> int:
    # create a run record
    from .runloop import start_run, finish_run
    from .strategy_sma import generate_signals_sma
    from .planner import plan_intents, save_intents

    run_id = start_run(notes=f"plan sma{fast}/{slow}")
    try:
        signals = generate_signals_sma(fast=fast, slow=slow)
        intents = plan_intents(signals)
        save_intents(run_id, intents)

        # Print only actionable ones first
        buys = [i for i in intents if i.action == "buy"]
        sells = [i for i in intents if i.action == "sell"]

        log.info("Plan run_id=%s | buys=%s | sells=%s | total=%s", run_id, len(buys), len(sells), len(intents))

        for i in sells:
            log.info("SELL %s | strength=%s | %s", i.symbol, i.strength, i.reason)
        for i in buys:
            log.info("BUY  %s | strength=%s | %s", i.symbol, i.strength, i.reason)

        finish_run(run_id, status="success")
        return 0
    except Exception:
        finish_run(run_id, status="failed")
        raise

def cmd_show_intents(run_id: int) -> int:
    from .db import connect
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT symbol, action, strength, reason, created_at
            FROM intents
            WHERE run_id = ?
            ORDER BY action DESC, symbol ASC;
            """,
            (run_id,),
        ).fetchall()

    if not rows:
        log.info("No intents found for run_id=%s", run_id)
        return 0

    for r in rows:
        log.info("%s %s | strength=%s | %s", r["action"].upper().ljust(4), r["symbol"], r["strength"], r["reason"])
    return 0

def cmd_explain(symbol: str, fast: int, slow: int) -> int:
    from .explain import explain_symbol
    info = explain_symbol(symbol, fast=fast, slow=slow)

    if "error" in info:
        log.info("%s: %s", info["symbol"], info["error"])
        return 0

    log.info(
        "%s @ %s close=%s | SMA%s now=%s prev=%s | SMA%s now=%s prev=%s",
        info["symbol"], info["last_date"], info["close"],
        fast, info["fast_now"], info["fast_prev"],
        slow, info["slow_now"], info["slow_prev"],
    )
    for d, c in info["last_closes"]:
        log.info("  %s close=%s", d, c)
    return 0

def cmd_execute(run_id: int, qty: float, submit: bool) -> int:
    from .execution import build_orders_from_intents, persist_orders, is_paper_submit_allowed
    from .broker.alpaca_broker import AlpacaPaperBroker
    from .db import connect
    import json

    proposed = build_orders_from_intents(run_id=run_id, default_qty=qty)
    inserted = persist_orders(run_id=run_id, orders=proposed)

    log.info(
        "Execute(run_id=%s): proposed=%s | inserted_into_db=%s",
        run_id,
        len(proposed),
        inserted,
    )

    if not proposed:
        log.info("No actionable intents (buy/sell). Nothing to do.")
        return 0

    for o in proposed:
        log.info("ORDER %s %s qty=%s | %s", o.side.upper(), o.symbol, o.qty, o.reason)

    if not submit:
        log.info("Dry-run only. Use --submit to send orders (paper-gated).")
        return 0

    if not is_paper_submit_allowed():
        log.info(
            "Submission blocked: set TRADING_ALLOW_PAPER_ORDERS=true in .env to allow paper submissions."
        )
        return 0

    broker = AlpacaPaperBroker()

    submitted = 0
    with connect() as conn:
        for o in proposed:
            # Fetch the DB order row and ensure it is still eligible to submit.
            row = conn.execute(
                "SELECT id, status FROM orders WHERE idempotency_key = ?;",
                (o.idempotency_key,),
            ).fetchone()
            if not row:
                continue

            order_id = row["id"]
            status = row["status"]

            # Idempotency: only submit orders that are still in 'created' state.
            if status != "created":
                log.info(
                    "Skip %s %s: status=%s (idempotency)",
                    o.side.upper(),
                    o.symbol,
                    status,
                )
                continue

            # Atomically claim the order so concurrent runs don't double-submit.
            cur = conn.execute(
                "UPDATE orders SET status='submitting' WHERE id=? AND status='created';",
                (order_id,),
            )
            if cur.rowcount != 1:
                log.info(
                    "Skip %s %s: could not claim order (already being handled)",
                    o.side.upper(),
                    o.symbol,
                )
                continue

            try:
                bo = broker.place_market_order(o.symbol, o.side, o.qty)
                broker_order_id = str(getattr(bo, "id", None))

                conn.execute(
                    "UPDATE orders SET status='submitted' WHERE id=?;",
                    (order_id,),
                )
                conn.execute(
                    """
                    INSERT INTO executions(broker, broker_order_id, symbol, side, qty, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    (
                        "alpaca",
                        broker_order_id,
                        o.symbol,
                        o.side,
                        o.qty,
                        json.dumps(bo.model_dump(), default=str),
                    ),
                )

                submitted += 1
                log.info(
                    "Submitted %s %s -> broker_order_id=%s",
                    o.side.upper(),
                    o.symbol,
                    broker_order_id,
                )
            except Exception as e:
                conn.execute(
                    "UPDATE orders SET status='failed', reason=? WHERE id=?;",
                    (str(e), order_id),
                )
                log.info("FAILED submitting %s %s: %s", o.side.upper(), o.symbol, e)

    log.info("Submitted total=%s", submitted)
    return 0

def cmd_seed_intent(run_id: int, symbol: str, action: str, reason: str) -> int:
    from .db import connect
    sym = symbol.strip().upper()

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO intents(run_id, symbol, action, strength, reason)
            VALUES (?, ?, ?, NULL, ?);
            """,
            (run_id, sym, action, reason),
        )

    log.info("Seeded intent: run_id=%s %s %s | %s", run_id, action.upper(), sym, reason)
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

    p_plan = sub.add_parser("plan", help="Generate today's trade plan (signals + intents). No orders placed.")
    p_plan.add_argument("--fast", type=int, default=20)
    p_plan.add_argument("--slow", type=int, default=50)

    p_si = sub.add_parser("show-intents", help="Show intents for a run")
    p_si.add_argument("--run-id", type=int, required=True)

    p_ex = sub.add_parser("explain", help="Explain SMA state for a symbol")
    p_ex.add_argument("symbol")
    p_ex.add_argument("--fast", type=int, default=20)
    p_ex.add_argument("--slow", type=int, default=50)

    p_exec = sub.add_parser("execute", help="Turn intents into orders; optionally submit to broker (paper-gated).")
    p_exec.add_argument("--run-id", type=int, required=True)
    p_exec.add_argument("--qty", type=float, default=1.0, help="Default quantity per order (Day 5 fixed sizing)")
    p_exec.add_argument("--submit", action="store_true", help="Actually submit to broker (requires TRADING_ALLOW_PAPER_ORDERS=true)")

    p_seed = sub.add_parser("seed-intent", help="Create a manual intent for testing (no broker action).")
    p_seed.add_argument("--run-id", type=int, required=True)
    p_seed.add_argument("--symbol", required=True)
    p_seed.add_argument("--action", choices=["buy", "sell"], required=True)
    p_seed.add_argument("--reason", default="manual test")

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
    
    if args.cmd == "plan":
        return cmd_plan(args.fast, args.slow)
    
    if args.cmd == "show-intents":
        return cmd_show_intents(args.run_id)
    
    if args.cmd == "explain":
        return cmd_explain(args.symbol, args.fast, args.slow)

    if args.cmd == "execute":
        return cmd_execute(args.run_id, args.qty, args.submit)

    if args.cmd == "seed-intent":
        return cmd_seed_intent(args.run_id, args.symbol, args.action, args.reason)

    return 1