import argparse
import logging
from .logging_setup import setup_logging
from .config import get_settings
from .db import init_db, connect
from datetime import datetime, timedelta, timezone
from .compliance import can_sell
from .universe_watchlist import load_watchlist_csv
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
        broker_accounts = conn.execute("SELECT COUNT(*) AS c FROM broker_accounts;").fetchone()["c"]
        execs = conn.execute("SELECT COUNT(*) AS c FROM executions;").fetchone()["c"]
        intents = conn.execute("SELECT COUNT(*) AS c FROM intents;").fetchone()["c"]

    log.info(
        "symbols=%s | runs=%s | intents=%s | positions=%s | orders=%s | executions=%s | broker_accounts=%s",
        sym, runs, intents, pos, ords, execs, broker_accounts
    )

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

def cmd_fetch_bars(days: int, universe: str, limit: int , sleep_ms: int) -> int:
    from .marketdata.alpaca_data import fetch_and_store_for_universe
    counts = fetch_and_store_for_universe(days=days, universe=universe, limit=limit, sleep_ms=sleep_ms)
    total = sum(counts.values())
    log.info("Fetched bars: total=%s | symbols=%s | universe=%s | limit=%s", total, len(counts), universe, limit)
    return 0


def cmd_plan(fast: int, slow: int, universe: str = "sp500") -> int:
    from .runloop import start_run, finish_run
    from .asof import resolve_asof_date
    from .db import connect
    from .strategy_sma import generate_signals_sma
    from .planner import plan_intents, save_intents

    run_id = start_run(notes=f"plan sma{fast}/{slow} universe={universe}")
    try:
        # 1) Resolve asof from DB (needs a connection)
        with connect() as conn:
            asof = resolve_asof_date(conn, None)

            # If your runs table has asof_date, store it
            # (If it doesn't, comment this out or add the column.)
            try:
                conn.execute("UPDATE runs SET asof_date=? WHERE id=?;", (asof, run_id))
            except Exception:
                pass

        # 2) Generate signals using that asof snapshot
        signals = generate_signals_sma(
                    fast=fast,
                    slow=slow,
                    universe=universe,
                    asof=asof,
                )

        # 3) Plan + save intents
        intents = plan_intents(signals)
        save_intents(run_id, intents)

        buys = [i for i in intents if i.action == "buy"]
        sells = [i for i in intents if i.action == "sell"]

        log.info(
            "Plan run_id=%s asof=%s | buys=%s | sells=%s | total=%s",
            run_id,
            asof,
            len(buys),
            len(sells),
            len(intents),
        )

        # Optional: print actionable first
        for i in sells:
            log.info("SELL %s | strength=%s | %s", i.symbol, i.strength, i.reason)
        for i in buys:
            # if you added target_notional, include it
            tn = getattr(i, "target_notional", None)
            if tn is not None:
                log.info("BUY  %s | strength=%s | $%s | %s", i.symbol, i.strength, tn, i.reason)
            else:
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

def cmd_execute(run_id: int, qty: float, submit: bool, retry_failed: bool) -> int:
    from .execution import execute_run

    out = execute_run(
        run_id=run_id,
        qty_default=qty,
        submit=submit,
        retry_failed=retry_failed,
    )

    # Keep your current logging style
    log.info(
        "Execute done(run_id=%s): proposed=%s inserted=%s submitted=%s skipped_reason=%s",
        run_id,
        out.get("proposed"),
        out.get("inserted"),
        out.get("submitted"),
        out.get("skipped_reason"),
    )
    return 0

def cmd_orders(limit: int) -> int:
    from .db import connect
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, side, qty, status, requested_at, broker_order_id, reason,
                   filled_qty, filled_avg_price, filled_at
            FROM orders
            ORDER BY id DESC
            LIMIT ?;
            """,
            (limit,),
        ).fetchall()

    if not rows:
        log.info("No orders.")
        return 0

    for r in rows:
        msg = (
            f"#{r['id']} {r['side'].upper()} {r['symbol']} "
            f"qty={r['qty']} status={r['status']} at={r['requested_at']}"
        )
        if r["broker_order_id"]:
            msg += f" broker_order_id={r['broker_order_id']}"
        if r["filled_qty"] is not None or r["filled_avg_price"] is not None or r["filled_at"] is not None:
            msg += f" | fill_qty={r['filled_qty']} fill_avg={r['filled_avg_price']} filled_at={r['filled_at']}"
        if r["reason"]:
            msg += f" | reason={r['reason']}"
        log.info(msg)

    return 0

def cmd_executions(limit: int) -> int:
    from .db import connect
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, broker, broker_order_id, symbol, side, qty, filled_avg_price, filled_at
            FROM executions
            ORDER BY id DESC
            LIMIT ?;
            """,
            (limit,),
        ).fetchall()

    if not rows:
        log.info("No executions.")
        return 0

    for r in rows:
        log.info(
            "#%s %s %s %s %s qty=%s avg=%s filled_at=%s",
            r["id"],
            r["broker"],
            r["broker_order_id"],
            r["side"].upper(),
            r["symbol"],
            r["qty"],
            r["filled_avg_price"],
            r["filled_at"],
        )
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

def cmd_sync_orders(limit: int) -> int:
    from .broker.orders_sync import sync_orders
    res = sync_orders(limit=limit)
    log.info("Synced orders: scanned=%s matched=%s updated=%s", res.scanned, res.matched, res.updated)
    return 0

def cmd_load_universe(universe: str, file: str) -> int:
    from .universe.load_universe import load_universe_csv
    n = load_universe_csv(universe=universe, csv_path=file)
    log.info("Loaded universe=%s symbols=%s from %s", universe, n, file)
    return 0


def cmd_sync_assets() -> int:
    from .universe.sync_assets import sync_assets_cache
    n = sync_assets_cache()
    log.info("Synced assets_cache rows=%s", n)
    return 0


def cmd_build_universe(universe: str, asof: str | None, top: int, min_adv20: float) -> int:
    from .universe.build_universe import build_universe_daily
    resolved_asof, n = build_universe_daily(universe=universe, asof=asof, top=top, min_adv20=min_adv20)
    log.info(
        "Built universe_daily: universe=%s asof=%s rows=%s top=%s min_adv20=%s",
        universe, resolved_asof, n, top, min_adv20
    )
    return 0


def cmd_show_universe(universe: str, asof: str | None, limit: int) -> int:
    from .db import connect

    with connect() as conn:
        if not asof:
            r = conn.execute(
                "SELECT MAX(asof_date) AS d FROM universe_daily WHERE universe=? AND include=1;",
                (universe,),
            ).fetchone()
            asof = r["d"] if r and r["d"] else None

        if not asof:
            log.info("No universe_daily rows for universe=%s", universe)
            return 0

        rows = conn.execute(
            """
            SELECT symbol, score, ret60, adv20, close, include, reason
            FROM universe_daily
            WHERE asof_date=? AND universe=?
            ORDER BY include DESC, score DESC
            LIMIT ?;
            """,
            (asof, universe, limit),
        ).fetchall()

    log.info("Universe=%s asof=%s rows=%s", universe, asof, len(rows))
    for r in rows:
        log.info(
            "%s %s score=%s ret60=%s adv20=%s close=%s | %s",
            "IN " if r["include"] == 1 else "OUT",
            r["symbol"],
            r["score"],
            r["ret60"],
            r["adv20"],
            r["close"],
            r["reason"],
        )
    return 0


def main() -> int:
    setup_logging()

    p = argparse.ArgumentParser(prog="trading", description="Personal trading system (Phase 1)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("healthcheck", help="Verify config + initialize DB schema")
    sub.add_parser("brokercheck", help="Check broker connection and store account snapshot")

    # load-universe
    p_lu = sub.add_parser("load-universe", help="Load a universe membership list from CSV (expects column: symbol)")
    p_lu.add_argument("--universe", default="sp500", help="Universe name (default: sp500)")
    p_lu.add_argument("--file", required=True, help="Path to CSV (must contain 'symbol' column)")

    # sync-assets
    p_sa = sub.add_parser("sync-assets", help="Sync Alpaca assets metadata into assets_cache")

    #sub.add_parser("status", help="Show quick DB status counts")
    #sub.add_parser("compliance-test", help="Test if complaince is working")

    #p_watch = sub.add_parser("load-watchlist", help="Load symbols from a watchlist CSV into the DB")
    #p_watch.add_argument("--csv", default="data/watchlist.csv", help="Path to watchlist CSV (default: data/watchlist.csv)")

    p_fb = sub.add_parser("fetch-bars", help="Fetch and store daily OHLCV bars for active symbols")
    p_fb.add_argument("--days", type=int, default=365, help="Lookback days (default 365)")
    p_fb.add_argument("--universe", type=str, default="sp500", help="Lookback universe (default sp500)")
    p_fb.add_argument("--limit", type=int, default=500, help="Lookback limit (default 500)")
    p_fb.add_argument("--sleep_ms", type=int, default=50, help="Lookback time (default 50ms)")

    # build-universe
    p_bu = sub.add_parser("build-universe", help="Build daily universe snapshot + select top N")
    p_bu.add_argument("--universe", default="sp500", help="Universe name (default: sp500)")
    p_bu.add_argument("--asof", required=False, help="Asof date (YYYY-MM-DD), must exist in bars_daily")
    p_bu.add_argument("--top", type=int, default=200, help="Keep top N by score among included")
    p_bu.add_argument("--min-adv20", type=float, default=20_000_000.0, help="Minimum 20-day average dollar volume")

    # show-universe
    p_su = sub.add_parser("show-universe", help="Show universe snapshot rows")
    p_su.add_argument("--universe", default="sp500", help="Universe name (default: sp500)")
    p_su.add_argument("--asof", required=False, help="Asof date (YYYY-MM-DD)")
    p_su.add_argument("--limit", type=int, default=25, help="Rows to print")

    sub.add_parser("sync-positions", help="Sync broker positions into SQLite")

    p_so = sub.add_parser("sync-orders", help="Sync recent Alpaca orders into SQLite (executions + order status)")
    p_so.add_argument("--limit", type=int, default=200)

    p_o = sub.add_parser("orders", help="Show recent internal orders")
    p_o.add_argument("--limit", type=int, default=20)

    p_e = sub.add_parser("executions", help="Show recent broker executions snapshots")
    p_e.add_argument("--limit", type=int, default=20)

    p_run = sub.add_parser("run", help="Execute one trading cycle (Phase 1 skeleton)")
    p_run.add_argument("--notes", default="", help="Optional notes to store in runs table")

    p_ro = sub.add_parser("run-once", help="Orchestrated daily run (Phase 2): sync, universe, plan, execute, final sync")
    p_ro.add_argument("--notes", default="", help="Optional notes to store in runs table")
    p_ro.add_argument("--universe", default="sp500")
    p_ro.add_argument("--top", type=int, default=200)
    p_ro.add_argument("--min-adv20", type=float, default=20_000_000.0)
    p_ro.add_argument("--fast", type=int, default=20)
    p_ro.add_argument("--slow", type=int, default=50)
    p_ro.add_argument("--execute", action="store_true", help="Attempt execution (still paper-gated)")

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
    p_exec.add_argument("--retry-failed", action="store_true", help="Allow resubmitting orders that previously failed")

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
    
    if args.cmd == "run-once":
        res = run_once(
            notes=args.notes or None,
            universe=args.universe,
            top=args.top,
            min_adv20=args.min_adv20,
            fast=args.fast,
            slow=args.slow,
            execute_requested=args.execute,
        )
        log.info("run-once completed. status=%s run_id=%s asof=%s reason=%s", res.status, res.run_id, res.asof, res.reason)
        return 0 if res.status in ("success", "skipped") else 2
    
    if args.cmd == "status":
        return cmd_status()
    
    if args.cmd == "brokercheck":
        return cmd_broker_check()

    if args.cmd == "sync-positions":
        return cmd_sync_positions()
    
    if args.cmd == "fetch-bars":
        return cmd_fetch_bars(args.days, args.universe, args.limit, args.sleep_ms)
    
    if args.cmd == "plan":
        return cmd_plan(args.fast, args.slow)
    
    if args.cmd == "show-intents":
        return cmd_show_intents(args.run_id)
    
    if args.cmd == "explain":
        return cmd_explain(args.symbol, args.fast, args.slow)

    if args.cmd == "execute":
        return cmd_execute(args.run_id, args.qty, args.submit, args.retry_failed)

    if args.cmd == "seed-intent":
        return cmd_seed_intent(args.run_id, args.symbol, args.action, args.reason)
    
    if args.cmd == "sync-orders":
        return cmd_sync_orders(args.limit)
    
    if args.cmd == "orders":
        return cmd_orders(args.limit)
    
    if args.cmd == "executions":
        return cmd_executions(args.limit)
    
    if args.cmd == "load-universe":
        return cmd_load_universe(args.universe, args.file)

    if args.cmd == "sync-assets":
        return cmd_sync_assets()

    if args.cmd == "build-universe":
        return cmd_build_universe(args.universe, args.asof, args.top, args.min_adv20)

    if args.cmd == "show-universe":
        return cmd_show_universe(args.universe, args.asof, args.limit)


    return 1