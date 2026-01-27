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
from .exits_advisor import evaluate_exit_advice, ExitRuleConfig, emit_sell_intents, exit_rule_config_from_env


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

def cmd_lots_rebuild() -> int:
    from .pnl.lots import rebuild_lots
    res = rebuild_lots()
    log.info(
        "Lots rebuild done | buys=%s sells=%s lots=%s closings=%s warnings=%s",
        res.buys_processed, res.sells_processed, res.lots_created, res.closings_created, res.warnings
    )
    return 0


def cmd_positions(asof: str | None) -> int:
    from .db import connect
    from .asof import resolve_asof_date
    from .pnl.lots import get_open_lots_by_symbol
    from .pnl.mark import get_close

    # Resolve asof date if not provided
    with connect() as conn:
        resolved = resolve_asof_date(conn, asof)

    lots_by_sym = get_open_lots_by_symbol()
    if not lots_by_sym:
        log.info("No open lots.")
        return 0

    total_unrl = 0.0
    log.info("OPEN POSITIONS (lots-based) asof=%s symbols=%s", resolved, len(lots_by_sym))

    for sym in sorted(lots_by_sym.keys()):
        lots = lots_by_sym[sym]

        qty = 0.0
        cost = 0.0
        for L in lots:
            q = float(L["qty_open"])
            p = float(L["entry_price"])
            qty += q
            cost += q * p

        if qty <= 0:
            continue

        avg_cost = cost / qty
        last = get_close(sym, resolved)
        if last is None:
            log.info("%s qty=%s avg_cost=%.4f last=? (no bars)", sym, qty, avg_cost)
            continue

        unrl = qty * (float(last) - avg_cost)
        total_unrl += unrl
        pct = (unrl / (qty * avg_cost)) * 100.0 if avg_cost > 0 else 0.0

        log.info(
            "%s qty=%.6f avg_cost=%.4f last=%.4f uPnL=%+.2f (%+.2f%%) lots=%s",
            sym, qty, avg_cost, float(last), unrl, pct, len(lots)
        )

    log.info("TOTAL unrealized (asof close) = %+.2f", total_unrl)
    return 0


def cmd_pnl(asof: str | None) -> int:
    from .db import connect
    from .asof import resolve_asof_date
    from .pnl.lots import get_open_lots_by_symbol, get_realized_pnl_for_date
    from .pnl.mark import get_close

    # Resolve asof date if not provided
    with connect() as conn:
        resolved = resolve_asof_date(conn, asof)

    realized = float(get_realized_pnl_for_date(resolved))

    # Compute unrealized from open lots marked to asof close
    lots_by_sym = get_open_lots_by_symbol()
    unrealized = 0.0

    for sym, lots in lots_by_sym.items():
        last = get_close(sym, resolved)
        if last is None:
            continue
        for L in lots:
            q = float(L["qty_open"])
            entry = float(L["entry_price"])
            unrealized += q * (float(last) - entry)

    total = realized + unrealized

    log.info("PNL asof=%s | realized=%+.2f | unrealized=%+.2f | total=%+.2f", resolved, realized, unrealized, total)
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

def cmd_exposure(asof: str | None, top: int = 20) -> int:
    from .db import connect
    from .asof import resolve_asof_date
    from .pnl.exposure import compute_exposure

    with connect() as conn:
        resolved = resolve_asof_date(conn, asof)

    out = compute_exposure(resolved)
    rows = out["rows"]
    equity = out["equity"]
    totals = out["totals"]

    if not rows:
        log.info("No exposure (no open lots).")
        return 0

    log.info("EXPOSURE asof=%s | equity=%s | symbols=%s", resolved, f"{equity:.2f}" if equity is not None else "?", len(rows))

    shown = rows[: max(0, int(top))]
    for r in shown:
        mv = r["mkt_value"]
        pct = r["pct_equity"]
        if mv is None:
            log.info(
                "%s qty=%.6f last=? mv=? | cost=%.2f lots=%s",
                r["symbol"], r["qty"], r["cost_basis"], r["lots"]
            )
        else:
            log.info(
                "%s mv=%.2f (%s) | qty=%.6f last=%.4f | cost=%.2f uPnL=%+.2f | lots=%s",
                r["symbol"],
                mv,
                (f"{pct*100:.2f}%" if pct is not None else "?"),
                r["qty"],
                r["last"],
                r["cost_basis"],
                r["u_pnl"],
                r["lots"],
            )

    log.info(
        "TOTAL mv=%.2f | cost=%.2f | uPnL=%+.2f",
        totals["market_value"],
        totals["cost_basis"],
        totals["u_pnl"],
    )
    return 0

def cmd_lots_reconcile(asof: str | None, dry_run: bool, apply: bool, tolerance: float) -> int:
    from .pnl.reconcile import lots_reconcile

    # default is dry-run unless --apply specified
    do_dry = True
    if apply:
        do_dry = False
    elif dry_run:
        do_dry = True

    res = lots_reconcile(asof, tolerance=tolerance, dry_run=do_dry)

    log.info(
        "LOTS RECONCILE asof=%s dry_run=%s tol=%s reduce=%s missing=%s",
        res["asof"], res["dry_run"], res["tolerance"], res["reduce_count"], res["missing_count"]
    )

    for r in res["reductions"]:
        if r.get("skipped"):
            log.info("%s reduce=%.6f skipped (%s)", r["symbol"], r["reduce"], r["reason"])
        else:
            log.info(
                "%s lots=%.6f pos=%.6f reduce=%.6f @ %.4f",
                r["symbol"], r["lots_qty"], r["pos_qty"], r["reduce"], r["price"]
            )

    for m in res["missing"]:
        log.info(
            "%s lots=%.6f pos=%.6f missing_lots=%.6f (needs history)",
            m["symbol"], m["lots_qty"], m["pos_qty"], m["missing_lots_qty"]
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

    sub.add_parser("lots-rebuild", help="Rebuild FIFO lots + closings from executions (Phase 3)")
    p_pos = sub.add_parser("positions", help="Show open positions (lots-based) + unrealized P&L")
    p_pos.add_argument("--asof", required=False, help="Asof date (YYYY-MM-DD). Default: resolve_asof_date()")

    p_pnl = sub.add_parser("pnl", help="Show realized/unrealized P&L at asof close (Phase 3)")
    p_pnl.add_argument("--asof", required=False, help="Asof date (YYYY-MM-DD). Default: resolve_asof_date()")

    p_pf = sub.add_parser("preflight", help="Sanity checks before running (calendar, bars freshness, account, open orders)")
    p_pf.add_argument("--universe", default="sp500")

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

    # exits (advisor mode)
    _env_cfg = exit_rule_config_from_env()
    p_ex = sub.add_parser("exits", help="Exit advisor: ranked SELL/WATCH/HOLD recommendations (no orders placed)")
    p_ex.add_argument("--dry-run", action="store_true", help="With --emit-intents: do not write to DB; only show what would be inserted")
    p_ex.add_argument("--asof", default=None, help="As-of date (YYYY-MM-DD). Default: latest bars_daily date")
    p_ex.add_argument("--emit-intents", action="store_true",
                  help=
                  "Write SELL intents for advisor SELL rows (no orders placed)")

    p_ex.add_argument("--no-sma", action="store_true", help="Disable SMA20/50 reversal rule")

    p_ex.add_argument("--stop-loss", type=float, default=_env_cfg.stop_loss_pct)
    p_ex.add_argument("--early-fail-days", type=int, default=_env_cfg.early_fail_days)
    p_ex.add_argument("--early-fail-max-ret", type=float, default=_env_cfg.early_fail_max_ret_pct)
    p_ex.add_argument("--trail-peak", type=float, default=_env_cfg.trail_activate_peak_gain_pct)
    p_ex.add_argument("--trail-dd", type=float, default=_env_cfg.trail_drawdown_pct)
    p_ex.add_argument("--breakeven-peak", type=float, default=_env_cfg.break_even_peak_gain_pct)
    p_ex.add_argument("--breakeven-floor", type=float, default=_env_cfg.break_even_floor_ret_pct)
    p_ex.add_argument("--take-profit", type=float, default=_env_cfg.take_profit_pct)
    p_ex.add_argument("--time-stop-days", type=int, default=_env_cfg.time_stop_days)
    p_ex.add_argument("--time-stop-min-ret", type=float, default=_env_cfg.time_stop_min_ret_pct)

    p_perf = sub.add_parser("performance", help="Equity curve performance metrics (Phase 3)")
    p_perf.add_argument("--monthly", action="store_true", help="Show month-by-month returns")
    p_perf.add_argument("--since", required=False, help="Filter snapshots from YYYY-MM-DD")
    p_perf.add_argument("--last", type=int, required=False, help="Use only last N snapshot days (non-monthly)")

    p_r = sub.add_parser("realized", help="Realized P&L reports (Phase 3)")
    p_r.add_argument("--daily", action="store_true", help="Group realized P&L by day")
    p_r.add_argument("--monthly", action="store_true", help="Group realized P&L by month")
    p_r.add_argument("--since", required=False, help="Filter events from YYYY-MM-DD (uses closed_at/created_at)")
    p_r.add_argument("--last", type=int, required=False, help="Last N rows (daily only)")
    p_r.add_argument("--symbol", required=False, help="Filter to symbol (e.g., AAPL)")

    p_t = sub.add_parser("trades", help="Trade ledger: open lots + closed trades (Phase 3)")
    p_t.add_argument("--open", action="store_true", help="Show open lots")
    p_t.add_argument("--closed", action="store_true", help="Show closed trades (lot closings)")
    p_t.add_argument("--asof", required=False, help="Asof date for open trades (default: resolve_asof_date())")
    p_t.add_argument("--since", required=False, help="Filter closed trades from YYYY-MM-DD")
    p_t.add_argument("--last", type=int, required=False, default=20, help="Last N closed trade rows")
    p_t.add_argument("--symbol", required=False, help="Filter to one symbol")
    p_t.add_argument("--sort", required=False, default="symbol",help="Sort open trades by: symbol, pnl, return, age, mv, cost")
    p_t.add_argument("--desc", action="store_true", help="Sort descending")


    p_exp = sub.add_parser("exposure", help="Exposure by symbol (market value + % of equity) (Phase 3)")
    p_exp.add_argument("--asof", required=False, help="Asof date (YYYY-MM-DD). Default: resolve_asof_date()")
    p_exp.add_argument("--top", type=int, required=False, default=20, help="Show top N symbols by market value")

    p_lr = sub.add_parser("lots-reconcile", help="Reconcile open lots to broker positions (Phase 3)")
    p_lr.add_argument("--asof", required=False, help="YYYY-MM-DD (default: resolve_asof_date())")
    p_lr.add_argument("--dry-run", action="store_true", help="Show what would change (default: True)")
    p_lr.add_argument("--apply", action="store_true", help="Actually write synthetic executions and close lots")
    p_lr.add_argument("--tolerance", type=float, default=0.001, help="Qty tolerance before reconciling")

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
    
    if args.cmd == "lots-rebuild":
        return cmd_lots_rebuild()

    if args.cmd == "positions":
        return cmd_positions(args.asof)

    if args.cmd == "pnl":
        return cmd_pnl(args.asof)
    
    if args.cmd == "exposure":
        return cmd_exposure(args.asof, args.top)
    
    if args.cmd == "lots-reconcile":
        return cmd_lots_reconcile(args.asof, args.dry_run, args.apply, args.tolerance)

    if args.cmd == "performance":
        from .pnl.performance import compute_performance, compute_monthly_performance

        if getattr(args, "monthly", False):
            rows = compute_monthly_performance(since=getattr(args, "since", None))
            if not rows:
                log.info("MONTHLY PERFORMANCE unavailable: no_snapshots")
                return 0

            log.info("MONTHLY PERFORMANCE rows=%s", len(rows))
            for r in rows:
                running = r.get("running_return")
                running_pct = (float(running) * 100.0) if running is not None else 0.0

                log.info(
                    "%s | %s→%s | start=%.2f end=%.2f | pnl=%+.2f | return=%+.2f%% | cum=%+.2f%%",
                    r["month"],
                    r["start_date"],
                    r["end_date"],
                    r["start_equity"],
                    r["end_equity"],
                    float(r["pnl_dollars"]),
                    float(r["return"]) * 100.0,
                    running_pct,
                )
            return 0

        res = compute_performance(since=getattr(args, "since", None), last=getattr(args, "last", None))
        if not res.ok:
            log.info("PERFORMANCE unavailable: %s", res.reason)
            if res.reason == "need_at_least_2_snapshots":
                log.info("Tip: run 'trading run-once' on at least two different trading days.")
            return 0

        log.info(
            "PERFORMANCE %s→%s | days=%s | start=%.2f end=%.2f | cum=%+.2f%% | maxDD=%+.2f%% | avgDaily=%+.4f%%",
            res.start_date, res.end_date, res.days,
            res.start_equity, res.end_equity,
            (res.cumulative_return or 0.0) * 100.0,
            (res.max_drawdown or 0.0) * 100.0,
            (res.avg_daily_return or 0.0) * 100.0,
        )

        if res.best_day:
            d, r = res.best_day
            log.info("Best day:  %s  %+.2f%%", d, r * 100.0)
        if res.worst_day:
            d, r = res.worst_day
            log.info("Worst day: %s  %+.2f%%", d, r * 100.0)

        return 0

    if args.cmd == "preflight":
        from .preflight import run_preflight
        r = run_preflight(universe=args.universe)
        log.info(
            "PREFLIGHT ok=%s exec_safe=%s asof=%s stale_days=%s market_open=%s reason=%s "
            "buying_power=%s equity=%s open_orders=%s cooldown_active=%s cap_remaining=%s min_bp_required=%.2f blockers=%s",
            r.ok, r.exec_safe, r.asof, r.staleness_days, r.market_open_day, r.market_reason,
            r.buying_power, r.equity, r.open_internal_orders, r.cooldown_active,
            r.cap_remaining, r.min_bp_required, r.exec_blockers
        )
        return 0 if r.ok else 2
    
    if args.cmd == "exits":
        cfg = ExitRuleConfig(
            stop_loss_pct=float(args.stop_loss),
            early_fail_days=int(args.early_fail_days),
            early_fail_max_ret_pct=float(args.early_fail_max_ret),
            trail_activate_peak_gain_pct=float(args.trail_peak),
            trail_drawdown_pct=float(args.trail_dd),
            break_even_peak_gain_pct=float(args.breakeven_peak),
            break_even_floor_ret_pct=float(args.breakeven_floor),
            take_profit_pct=float(args.take_profit),
            time_stop_days=int(args.time_stop_days),
            time_stop_min_ret_pct=float(args.time_stop_min_ret),
            enable_sma_reversal=(not args.no_sma),
        )

        rows = evaluate_exit_advice(asof=args.asof, cfg=cfg)
        if not rows:
            log.info("EXITS: none (no open lots)")
            return 0

        log.info(
            "EXITS (advisor) asof=%s rows=%s | stop_loss=%.2f%% trail=(peak>=%.2f%% dd<=-%.2f%%) "
            "breakeven=(peak>=%.2f%% ret<=%.2f%%) early_fail=%sd time_stop=%sd",
            args.asof or "latest",
            len(rows),
            cfg.stop_loss_pct,
            cfg.trail_activate_peak_gain_pct,
            cfg.trail_drawdown_pct,
            cfg.break_even_peak_gain_pct,
            cfg.break_even_floor_ret_pct,
            cfg.early_fail_days,
            cfg.time_stop_days,
        )

        for r in rows:
            log.info(
                "%-5s entry=%-22s action=%-5s prio=%3s ret=%+6.2f%% pnl=%+7.2f days=%2s peak=%+6.2f%% dd=%+6.2f%% | %s | %s",
                r["symbol"],
                r.get("entry_signal_key", "unknown"),
                r["action"],
                r["priority"],
                r["unrealized_ret_pct"],
                (r["unrealized_pnl"] or 0.0),
                r["holding_days"],
                r["peak_gain_pct"],
                r["drawdown_from_peak_pct"],
                r["exit_signal_key"],
                r["rationale"],
            )

        if args.emit_intents:
                summary = emit_sell_intents(asof=args.asof, cfg=cfg, dry_run=args.dry_run)
                if args.dry_run:
                    log.info(
                        "EXITS emit-intents (dry-run): run_id=%s asof=%s | advisor_rows=%s sell_rows=%s | would_insert=%s skipped_existing=%s",
                        summary["run_id"], summary["asof"], summary["advisor_rows"], summary["sell_rows"],
                        summary["would_insert"], summary["skipped_existing"],
                    )
                else:
                    log.info(
                        "EXITS emit-intents: run_id=%s asof=%s | advisor_rows=%s sell_rows=%s | inserted=%s skipped_existing=%s",
                        summary["run_id"], summary["asof"], summary["advisor_rows"], summary["sell_rows"],
                        summary["inserted"], summary["skipped_existing"],
                    )

        return 0

    
    if args.cmd == "realized":
        from .pnl.realized import realized_summary, realized_by_day, realized_by_month

        if args.daily:
            rows = realized_by_day(since=args.since, last=args.last, symbol=args.symbol)
            if not rows:
                log.info("REALIZED (daily) unavailable: no_realized_trades")
                return 0
            log.info("REALIZED (daily) rows=%s", len(rows))
            for r in rows:
                log.info(
                    "%s | trades=%s | pnl=%+.2f | win_rate=%.1f%% (%sW/%sL)",
                    r["day"], r["trades"], r["pnl"], r["win_rate"] * 100.0, r["wins"], r["losses"]
                )
            return 0

        if args.monthly:
            rows = realized_by_month(since=args.since, symbol=args.symbol)
            if not rows:
                log.info("REALIZED (monthly) unavailable: no_realized_trades")
                return 0
            log.info("REALIZED (monthly) rows=%s", len(rows))
            for r in rows:
                log.info(
                    "%s | trades=%s | pnl=%+.2f | win_rate=%.1f%% (%sW/%sL)",
                    r["month"], r["trades"], r["pnl"], r["win_rate"] * 100.0, r["wins"], r["losses"]
                )
            return 0

        s = realized_summary(since=args.since, symbol=args.symbol)
        if not s.ok:
            log.info("REALIZED unavailable: %s", s.reason)
            return 0

        log.info(
            "REALIZED since=%s rows=%s | pnl=%+.2f | win_rate=%.1f%% (%sW/%sL) | avg_win=%.2f avg_loss=%.2f | max_win=%.2f max_loss=%.2f",
            args.since or "-", s.rows, s.pnl, s.win_rate * 100.0, s.wins, s.losses,
            s.avg_win, s.avg_loss, s.max_win, s.max_loss
        )
        return 0
    
    if args.cmd == "trades":
        from .db import connect
        from .asof import resolve_asof_date
        from .pnl.trades import compute_open_trade_lines, fetch_closed_trades,sort_open_rows

        # default: show both
        show_open = args.open or (not args.open and not args.closed)
        show_closed = args.closed or (not args.open and not args.closed)

        if show_open:
            with connect() as conn:
                asof = resolve_asof_date(conn, args.asof)

            rows = compute_open_trade_lines(asof=asof, symbol=args.symbol)
            if not rows:
                log.info("TRADES (open) asof=%s: none", asof)
            else:
                rows = sort_open_rows(rows, args.sort, args.desc)

                # totals
                tot_cost = sum(r["cost_basis"] for r in rows if r.get("cost_basis") is not None)
                tot_mv = sum(r["mkt_value"] for r in rows if r.get("mkt_value") is not None)
                tot_upnl = sum(r["u_pnl"] for r in rows if r.get("u_pnl") is not None)

                tot_ret_pct = ((tot_mv / tot_cost) - 1.0) * 100.0 if tot_cost > 0 else 0.0

                log.info(
                    "TRADES (open) asof=%s rows=%s | total_cost=%.2f total_mv=%.2f uPnL=%+.2f total_ret=%+.2f%% | sort=%s %s",
                    asof, len(rows), tot_cost, tot_mv, tot_upnl, tot_ret_pct, args.sort, ("desc" if args.desc else "asc")
                )

                for r in rows:
                    if r["last"] is None:
                        log.info(
                            "%s lot=%s qty=%.6f entry=%.4f last=? mv=? uPnL=? ret=? age=%s",
                            r["symbol"], r["lot_id"], r["qty"], r["entry"], r["age_days"]
                        )
                    else:
                        log.info(
                            "%s lot=%s mv=%.2f | qty=%.6f entry=%.4f last=%.4f uPnL=%+.2f ret=%+.2f%% age=%s",
                            r["symbol"], r["lot_id"], r["mkt_value"],
                            r["qty"], r["entry"], r["last"], r["u_pnl"], r["ret_pct"], r["age_days"]
                        )

        if show_closed:
            rows = fetch_closed_trades(since=args.since, last=args.last, symbol=args.symbol)
            if not rows:
                log.info("TRADES (closed): none")
            else:
                log.info("TRADES (closed) rows=%s (showing last=%s)", len(rows), args.last)
                for r in rows:
                    log.info(
                        "%s qty=%.6f entry=%.4f exit=%.4f pnl=%+.2f hold_days=%s close_ts=%s",
                        r["symbol"],
                        float(r["qty_closed"]),
                        float(r["entry_price"]),
                        float(r["exit_price"]),
                        float(r["realized_pnl"]),
                        r.get("hold_days"),
                        r.get("close_ts"),
                    )

        return 0

    return 1