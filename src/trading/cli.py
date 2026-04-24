import argparse
import logging
import re
from .logging_setup import setup_logging
from .config import get_settings
from .db import init_db, connect
from datetime import datetime, timedelta, timezone
from .compliance import can_sell
from .universe_watchlist import load_watchlist_csv
from .runloop import run_once
from trading.broker.alpaca_broker import AlpacaBroker
from trading.broker.sync import upsert_account, sync_positions
from .exits_advisor import evaluate_exit_advice, ExitRuleConfig, emit_sell_intents, exit_rule_config_from_env,_format_policy_exit_annotation
from trading.analytics.mfe_mae import compute_excursions_for_closings
from collections import defaultdict
from trading.policy.loader import load_latest_policy

log = logging.getLogger("trading")

def _make_broker():
    """Return broker instance (env-driven via .env settings)."""
    from trading.broker.alpaca_broker import AlpacaBroker  # env-driven (paper/live)
    return AlpacaBroker()

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
    b = _make_broker()
    upsert_account(b)
    a = b.get_account()
    log.info("Broker OK: %s | status=%s | buying_power=%s | equity=%s", a.broker, a.status, a.buying_power, a.equity)
    return 0

def cmd_sync_positions() -> int:
    b = _make_broker()
    n = sync_positions(b)
    log.info("Synced %s positions from %s", n, b.name)
    return 0

def cmd_risk_status() -> int:
    from .risk.state import get_effective_state
    from .risk.limits import get_limits
    from .risk.circuit_breaker import compute_peak_and_dd, explain_risk_status
    s = get_settings()
    env = getattr(s, "env", "paper")

    state = get_effective_state(env)
    limits = get_limits(env)

    # Try to compute dd using latest snapshot if available
    with connect() as conn:
        row = conn.execute(
            "SELECT asof_date, equity FROM account_snapshots_daily ORDER BY asof_date DESC LIMIT 1;"
        ).fetchone()
    equity = float(row["equity"]) if row else None
    peak, dd = compute_peak_and_dd(env=env, equity=equity)

    log.info(
        "RISK env=%s state=%s allow_buys=%s allow_sells=%s allow_broker=%s",
        env,
        state["state"],
        state["allow_buys"],
        state["allow_sells"],
        state["allow_broker"],
    )
    if equity is not None:
        log.info("RISK equity=%.2f peak=%.2f drawdown=%.2f%%", equity, peak, dd * 100.0)

    log.info(
        "LIMITS pause_buys=%.2f%% sell_only=%.2f%% halt_all=%.2f%% reset<=%.2f%%",
        limits["max_dd_pause_buys_pct"] * 100.0,
        limits["max_dd_sell_only_pct"] * 100.0,
        limits["max_dd_halt_all_pct"] * 100.0,
        limits["hysteresis_reset_pct"] * 100.0,
    )

    # ---- NEW: streaks + held_ok + would_auto_state ----
    if equity is not None:
        diag = explain_risk_status(
            env=env,
            equity=float(equity),
            peak_equity=float(peak),
            drawdown_pct=float(dd),
            effective_state=state,
            limits=limits,
        )

        log.info(
            "AUTO instant=%s streak_candidate=%s would_auto=%s operator_override=%s",
            diag["instant_state"],
            diag["streak_candidate"],
            diag["would_auto_state"],
            diag["operator_override"],
        )

        log.info(
            "STREAKS pause_ge=%s/%s sell_ge=%s/%s halt_ge=%s/%s clear_le=%s/%s",
            diag["streaks"]["pause_ge"],
            diag["cfg"]["pause_streak_days"],
            diag["streaks"]["sell_ge"],
            diag["cfg"]["sell_streak_days"],
            diag["streaks"]["halt_ge"],
            diag["cfg"]["halt_streak_days"],
            diag["streaks"]["clear_le"],
            diag["cfg"]["clear_streak_days"],
        )

        log.info(
            "RECOVERY held_ok=%s age_min=%s can_deescalate=%s min_hold_min=%s reset<=%.2f%% set_at=%s",
            diag["held_ok"],
            diag.get("age_minutes"),
            diag["can_deescalate"],
            diag["cfg"]["min_hold_minutes"],
            diag["limits"]["reset"] * 100.0,
            diag.get("set_at"),
        )

        log.info(
            "DECISION desired=%s reason=%s blocked_by_hold=%s",
            diag["desired_state"],
            diag["decision_reason"],
            diag["blocked_by_hold"],
        )

        hr = diag.get("headroom") or {}
        log.info(
            "HEADROOM to_pause=%.2f%% to_sell=%.2f%% to_halt=%.2f%% to_reset=%.2f%%",
            float(hr.get("to_pause_buys", 0.0)) * 100.0,
            float(hr.get("to_sell_only", 0.0)) * 100.0,
            float(hr.get("to_halt_all", 0.0)) * 100.0,
            float(hr.get("to_reset_band", 0.0)) * 100.0,
        )

    # Last 5 events
    with connect() as conn:
        ev = conn.execute(
            "SELECT ts, event_type, prev_state, new_state, reason, actor FROM risk_events WHERE env=? ORDER BY ts DESC LIMIT 5;",
            (env,),
        ).fetchall()
    for r in ev:
        log.info(
            "EVENT %s | %s %s->%s | actor=%s | %s",
            r["ts"],
            r["event_type"],
            r["prev_state"],
            r["new_state"],
            r["actor"],
            r["reason"],
        )

    return 0

def cmd_risk_set(state: str, reason: str, expires_minutes: int | None = None) -> int:
    from .risk.state import set_operator_override
    s = get_settings()
    env = getattr(s, "env", "paper")
    set_operator_override(env=env, state=state, reason=reason, expires_minutes=expires_minutes)
    log.warning("RISK override set env=%s state=%s reason=%s", env, state, reason)
    return 0

def cmd_risk_clear() -> int:
    from .risk.state import clear_operator_override
    s = get_settings()
    env = getattr(s, "env", "paper")
    clear_operator_override(env=env)
    log.warning("RISK override cleared env=%s", env)
    return 0

def cmd_risk_reset_peak(reason: str) -> int:
    from .risk.state import reset_peak
    s = get_settings()
    env = getattr(s, "env", "paper")
    reset_peak(env=env, reason=reason)
    log.warning("RISK peak reset env=%s reason=%s", env, reason)
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
        policy = load_latest_policy(policies_dir="policies")
        intents = plan_intents(signals, policy=policy)
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
            tn = getattr(i, "target_notional", None)

            # --- policy overlay (advisory only) ---
            extra_parts: list[str] = []

            policy_rec = getattr(i, "policy_rec", None)
            if policy_rec:
                extra_parts.append(f"policy={policy_rec}")

                policy_score = getattr(i, "policy_score", None)
                if policy_score is not None:
                    extra_parts.append(f"score={policy_score:+.2f}")

                policy_trades = getattr(i, "policy_trades", None)
                if policy_trades is not None:
                    extra_parts.append(f"tr={policy_trades}")

                policy_reco_notional = getattr(i, "policy_reco_notional", None)
                if policy_reco_notional is not None:
                    enf = "enf" if getattr(i, "policy_enforceable", False) else "soft"
                    extra_parts.append(f"reco=${policy_reco_notional:.2f}({enf})")

                policy_best = getattr(i, "policy_best_exits", None)
                if policy_best:
                    extra_parts.append(f"best=[{policy_best}]")

            extra = f" | {' '.join(extra_parts)}" if extra_parts else ""

            # --- final log ---
            if tn is not None:
                log.info(
                    "BUY  %s | strength=%s | $%.2f | %s%s",
                    i.symbol,
                    i.strength,
                    tn,
                    i.reason,
                    extra,
                )
            else:
                log.info(
                    "BUY  %s | strength=%s | %s%s",
                    i.symbol,
                    i.strength,
                    i.reason,
                    extra,
                )


        finish_run(run_id, status="success")
        return 0

    except Exception:
        finish_run(run_id, status="failed")
        raise


def cmd_show_intents(run_id: int) -> int:
    from .db import connect
    import sqlite3

    with connect() as conn:
        try:
            rows = conn.execute(
                """
                SELECT
                  symbol, action, strength, reason, created_at,
                  target_notional,
                  policy_asof, policy_rec, policy_score, policy_trades,
                  policy_reco_notional, policy_best_exits
                FROM intents
                WHERE run_id = ?
                ORDER BY action DESC, symbol ASC;
                """,
                (run_id,),
            ).fetchall()
            has_policy = True
        except sqlite3.OperationalError:
            rows = conn.execute(
                """
                SELECT symbol, action, strength, reason, created_at
                FROM intents
                WHERE run_id = ?
                ORDER BY action DESC, symbol ASC;
                """,
                (run_id,),
            ).fetchall()
            has_policy = False

    if not rows:
        log.info("No intents found for run_id=%s", run_id)
        return 0

    # Helper to safely read from sqlite3.Row
    def _col(r: sqlite3.Row, name: str, default=None):
        try:
            return r[name]
        except Exception:
            return default

    for r in rows:
        action = str(_col(r, "action", "") or "").upper().ljust(4)
        symbol = _col(r, "symbol", "")
        strength = _col(r, "strength", None)
        reason = _col(r, "reason", "")

        base = f"{action} {symbol} | strength={strength} | {reason}"

        if has_policy:
            policy_rec = _col(r, "policy_rec", None)
            if policy_rec:
                extra = f" | pol_asof={_col(r, 'policy_asof', '')} policy={policy_rec}"

                policy_score = _col(r, "policy_score", None)
                if policy_score is not None:
                    extra += f" score={float(policy_score):+.2f}"

                policy_trades = _col(r, "policy_trades", None)
                if policy_trades is not None:
                    extra += f" tr={int(policy_trades)}"

                reco = _col(r, "policy_reco_notional", None)
                if reco is not None:
                    extra += f" reco=${float(reco):.2f}"

                best = _col(r, "policy_best_exits", None)
                if best:
                    extra += f" best=[{best}]"

                log.info("%s%s", base, extra)
                continue

        log.info("%s", base)

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

def cmd_perf_by_signal(since: str | None, to_date: str | None) -> int:
    from .db import connect
    from datetime import datetime, timedelta

    def _today() -> str:
        return datetime.utcnow().strftime("%Y-%m-%d")

    if not to_date:
        to_date = _today()

    if not since:
        since = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")

    sql = """
    SELECT
      COALESCE(i.signal_key, 'unknown') AS entry_signal_key,
      COUNT(*) AS trades,
      SUM(CASE WHEN lc.realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
      ROUND(SUM(lc.realized_pnl), 2) AS pnl,
      ROUND(AVG(lc.realized_pnl), 2) AS avg_pnl,
      ROUND(AVG(((lc.exit_price - lc.entry_price) / NULLIF(lc.entry_price, 0.0)) * 100.0), 3) AS avg_ret_pct,
      ROUND(AVG(julianday(substr(lc.exit_filled_at,1,10)) - julianday(substr(lc.entry_filled_at,1,10))), 2) AS avg_hold_days
    FROM lot_closings lc
    LEFT JOIN executions e ON e.id = lc.entry_execution_id
    LEFT JOIN orders o     ON o.id = e.order_id
    LEFT JOIN intents i    ON i.id = o.intent_id
    WHERE substr(lc.exit_filled_at, 1, 10) BETWEEN ? AND ?
    GROUP BY COALESCE(i.signal_key, 'unknown')
    ORDER BY SUM(lc.realized_pnl) DESC;
    """

    with connect() as conn:
        rows = conn.execute(sql, (since, to_date)).fetchall()

    if not rows:
        log.info("PERF-BY-SIGNAL none | range=%s→%s (no lot_closings)", since, to_date)
        return 0

    total_trades = sum(int(r["trades"]) for r in rows)
    total_pnl = sum(float(r["pnl"] or 0.0) for r in rows)
    log.info("PERF-BY-SIGNAL (realized) %s→%s | keys=%s trades=%s pnl=%+.2f", since, to_date, len(rows), total_trades, total_pnl)

    for r in rows:
        trades = int(r["trades"])
        wins = int(r["wins"] or 0)
        win_pct = (wins / trades * 100.0) if trades else 0.0
        log.info(
            "  %-28s trades=%-4d win=%5.1f%% pnl=%+9.2f avg=%+7.2f avgRet=%+6.2f%% hold=%4.1fd",
            r["entry_signal_key"],
            trades,
            win_pct,
            float(r["pnl"] or 0.0),
            float(r["avg_pnl"] or 0.0),
            float(r["avg_ret_pct"] or 0.0),
            float(r["avg_hold_days"] or 0.0),
        )

    return 0

def cmd_perf_by_signal_open(asof: str | None) -> int:
    from .db import connect
    from .asof import resolve_asof_date

    with connect() as conn:
        asof = resolve_asof_date(conn, asof)

        sql = """
        WITH last_bar AS (
          SELECT b.symbol, b.c AS last_close
          FROM bars_daily b
          JOIN (
            SELECT symbol, MAX(t) AS t
            FROM bars_daily
            WHERE t <= ?
            GROUP BY symbol
          ) m
            ON m.symbol = b.symbol AND m.t = b.t
        )
        SELECT
          COALESCE(p.entry_signal_key, 'unknown') AS entry_signal_key,
          COUNT(*) AS positions,
          ROUND(SUM(p.qty * p.avg_entry_price), 2) AS cost,
          ROUND(SUM(p.qty * lb.last_close), 2) AS mv,
          ROUND(SUM((p.qty * lb.last_close) - (p.qty * p.avg_entry_price)), 2) AS upnl,
          ROUND(
            CASE
              WHEN SUM(p.qty * p.avg_entry_price) = 0 THEN 0
              ELSE (SUM((p.qty * lb.last_close) - (p.qty * p.avg_entry_price)) / SUM(p.qty * p.avg_entry_price)) * 100.0
            END
          , 3) AS uret_pct,
          ROUND(AVG(julianday(?) - julianday(substr(p.opened_at,1,10))), 2) AS avg_hold_days
        FROM positions p
        JOIN last_bar lb ON lb.symbol = p.symbol
        WHERE p.qty > 0 AND p.avg_entry_price IS NOT NULL
        GROUP BY COALESCE(p.entry_signal_key, 'unknown')
        ORDER BY upnl DESC;
        """

        rows = conn.execute(sql, (asof, asof)).fetchall()

    if not rows:
        log.info("PERF-BY-SIGNAL-OPEN none | asof=%s (no open positions)", asof)
        return 0

    total_cost = sum(float(r["cost"] or 0.0) for r in rows)
    total_upnl = sum(float(r["upnl"] or 0.0) for r in rows)
    total_mv = sum(float(r["mv"] or 0.0) for r in rows)

    tot_ret = 0.0
    if total_cost > 0:
        tot_ret = (total_upnl / total_cost) * 100.0

    log.info(
        "PERF-BY-SIGNAL-OPEN (unrealized) asof=%s | keys=%s pos=%s cost=%.2f mv=%.2f upnl=%+.2f uret=%+.2f%%",
        asof, len(rows), sum(int(r["positions"]) for r in rows), total_cost, total_mv, total_upnl, tot_ret
    )

    for r in rows:
        log.info(
            "  %-28s pos=%-3d cost=%9.2f mv=%9.2f upnl=%+9.2f uret=%+6.2f%% hold=%4.1fd",
            r["entry_signal_key"],
            int(r["positions"]),
            float(r["cost"] or 0.0),
            float(r["mv"] or 0.0),
            float(r["upnl"] or 0.0),
            float(r["uret_pct"] or 0.0),
            float(r["avg_hold_days"] or 0.0),
        )

    return 0

def cmd_perf_by_signal_total(since: str | None, to_date: str | None, asof: str | None) -> int:
    from .db import connect
    from .asof import resolve_asof_date
    from datetime import datetime, timedelta

    def _today() -> str:
        return datetime.utcnow().strftime("%Y-%m-%d")

    if not to_date:
        to_date = _today()
    if not since:
        since = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")

    # default unrealized snapshot date
    asof = asof or to_date

    with connect() as conn:
        asof = resolve_asof_date(conn, asof)

        sql = """
        WITH
        realized AS (
          SELECT
            COALESCE(i.signal_key, 'unknown') AS entry_signal_key,
            COUNT(*) AS trades,
            SUM(CASE WHEN lc.realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(lc.realized_pnl) AS rpnl,
            AVG(lc.realized_pnl) AS avg_rpnl,
            AVG(((lc.exit_price - lc.entry_price) / NULLIF(lc.entry_price,0.0)) * 100.0) AS avg_ret_pct,
            AVG(julianday(substr(lc.exit_filled_at,1,10)) - julianday(substr(lc.entry_filled_at,1,10))) AS avg_hold_days
          FROM lot_closings lc
          LEFT JOIN executions e ON e.id = lc.entry_execution_id
          LEFT JOIN orders o     ON o.id = e.order_id
          LEFT JOIN intents i    ON i.id = o.intent_id
          WHERE substr(lc.exit_filled_at, 1, 10) BETWEEN ? AND ?
          GROUP BY COALESCE(i.signal_key, 'unknown')
        ),
        last_bar AS (
          SELECT b.symbol, b.c AS last_close
          FROM bars_daily b
          JOIN (
            SELECT symbol, MAX(t) AS t
            FROM bars_daily
            WHERE t <= ?
            GROUP BY symbol
          ) m
            ON m.symbol = b.symbol AND m.t = b.t
        ),
        openpos AS (
          SELECT
            COALESCE(p.entry_signal_key, 'unknown') AS entry_signal_key,
            COUNT(*) AS positions,
            SUM(p.qty * p.avg_entry_price) AS cost,
            SUM(p.qty * lb.last_close) AS mv,
            SUM((p.qty * lb.last_close) - (p.qty * p.avg_entry_price)) AS upnl
          FROM positions p
          JOIN last_bar lb ON lb.symbol = p.symbol
          WHERE p.qty > 0 AND p.avg_entry_price IS NOT NULL
          GROUP BY COALESCE(p.entry_signal_key, 'unknown')
        ),
        keys AS (
          SELECT entry_signal_key FROM realized
          UNION
          SELECT entry_signal_key FROM openpos
        )
        SELECT
          k.entry_signal_key,
          COALESCE(r.trades, 0) AS trades,
          COALESCE(r.wins, 0) AS wins,
          COALESCE(r.rpnl, 0.0) AS realized_pnl,
          COALESCE(o.positions, 0) AS positions,
          COALESCE(o.cost, 0.0) AS open_cost,
          COALESCE(o.mv, 0.0) AS open_mv,
          COALESCE(o.upnl, 0.0) AS unrealized_pnl,
          (COALESCE(r.rpnl, 0.0) + COALESCE(o.upnl, 0.0)) AS total_pnl
        FROM keys k
        LEFT JOIN realized r ON r.entry_signal_key = k.entry_signal_key
        LEFT JOIN openpos o  ON o.entry_signal_key = k.entry_signal_key
        ORDER BY total_pnl DESC;
        """

        rows = conn.execute(sql, (since, to_date, asof)).fetchall()

    if not rows:
        log.info("PERF-BY-SIGNAL-TOTAL none | realized=%s→%s asof=%s", since, to_date, asof)
        return 0

    tot_r = sum(float(r["realized_pnl"] or 0.0) for r in rows)
    tot_u = sum(float(r["unrealized_pnl"] or 0.0) for r in rows)
    tot_t = sum(float(r["total_pnl"] or 0.0) for r in rows)

    log.info(
        "PERF-BY-SIGNAL-TOTAL realized=%s→%s asof=%s | keys=%s rpnl=%+.2f upnl=%+.2f total=%+.2f",
        since, to_date, asof, len(rows), tot_r, tot_u, tot_t
    )

    for r in rows:
        trades = int(r["trades"])
        wins = int(r["wins"])
        win_pct = (wins / trades * 100.0) if trades else 0.0
        log.info(
            "  %-28s trades=%-4d win=%5.1f%% rpnl=%+9.2f pos=%-3d upnl=%+9.2f total=%+9.2f",
            r["entry_signal_key"],
            trades,
            win_pct,
            float(r["realized_pnl"] or 0.0),
            int(r["positions"]),
            float(r["unrealized_pnl"] or 0.0),
            float(r["total_pnl"] or 0.0),
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

def _safe_mean(vals: list[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    return (sum(vals) / len(vals)) if vals else None

def _attach_mfe_stats(
    *,
    stats: dict[str, dict[str, object]],
    filtered_rows,
    mode: str,
    conn,
) -> dict[str, dict[str, object]]:
    """
    Mutates stats in-place by adding:
      avg_mfe_pct, avg_mae_pct, avg_capture_pct

    Note: capture% is only meaningful for winners (realized_ret > 0), so we
    aggregate capture_pct using winning trades only.
    """
    # Build dict-like closings for mfe_mae
    closings = [r.__dict__ if hasattr(r, "__dict__") else r for r in filtered_rows]
    exc_map = compute_excursions_for_closings(conn, closings=closings)

    buckets_mfe: dict[str, list[float]] = defaultdict(list)
    buckets_mae: dict[str, list[float]] = defaultdict(list)
    buckets_cap: dict[str, list[float]] = defaultdict(list)

    for r in filtered_rows:
        # bucket key must match how your attrib_* groups
        if mode == "exit":
            k = (r.exit_signal_key if hasattr(r, "exit_signal_key") else r.get("exit_signal_key")) or "UNKNOWN"
        elif mode == "entry":
            k = (r.entry_signal_key if hasattr(r, "entry_signal_key") else r.get("entry_signal_key")) or "UNKNOWN"
        elif mode == "combo":
            ek = (r.entry_signal_key if hasattr(r, "entry_signal_key") else r.get("entry_signal_key")) or "UNKNOWN"
            xk = (r.exit_signal_key if hasattr(r, "exit_signal_key") else r.get("exit_signal_key")) or "UNKNOWN"
            k = f"{ek} × {xk}"
        else:
            k = "UNKNOWN"

        closing_id = int(r.closing_id if hasattr(r, "closing_id") else r["closing_id"])
        ex = exc_map.get(closing_id)
        if not ex:
            continue

        # MFE/MAE always aggregate if present
        if ex.mfe_pct is not None:
            buckets_mfe[k].append(ex.mfe_pct)
        if ex.mae_pct is not None:
            buckets_mae[k].append(ex.mae_pct)

        # Capture%: winners-only (realized_ret is a fraction, e.g. 0.1655)
        realized_ret = (r.realized_ret if hasattr(r, "realized_ret") else r.get("realized_ret"))
        if ex.capture_pct is not None and realized_ret is not None and realized_ret > 0:
            buckets_cap[k].append(ex.capture_pct)

    # Merge into stats
    for k, m in stats.items():
        m["avg_mfe_pct"] = _safe_mean(buckets_mfe.get(k, []))
        m["avg_mae_pct"] = _safe_mean(buckets_mae.get(k, []))
        m["avg_capture_pct"] = _safe_mean(buckets_cap.get(k, []))

    return stats

def _print_attrib_table(title: str, stats: dict[str, dict[str, object]], *, top: int = 25) -> None:
    show_mfe_cols = any(("avg_mfe_pct" in m) for m in stats.values())

    def fmt_pf(x):
        # Handles inf nicely
        if x is None:
            return "  0.00"
        try:
            if x == float("inf"):
                return "   inf"
        except Exception:
            pass
        return f"{float(x):6.2f}"

    def fmt7(x):
        return "   n/a" if x is None else f"{float(x):7.2f}"

    print(title)
    print("-" * len(title))

    hdr = (
        f"{'key':50s}  {'trades':>6s}  {'win%':>6s}  {'total_pnl':>12s}  "
        f"{'avg%':>7s}  {'med%':>7s}  {'std%':>7s}  {'best%':>7s}  {'worst%':>7s}  {'pf':>6s}  {'hold':>5s}"
    )
    if show_mfe_cols:
        hdr += f"  {'mfe%':>7s}  {'mae%':>7s}  {'cap%':>7s}"
    print(hdr)

    i = 0
    for k, m in stats.items():
        i += 1
        if i > top:
            break

        win_rate = m.get("win_rate")
        line = (
            f"{k[:50]:50s}  "
            f"{int(m.get('trades') or 0):6d}  "
            f"{((win_rate * 100) if win_rate is not None else 0):6.1f}  "
            f"{float(m.get('total_pnl') or 0.0):12.2f}  "
            f"{((m.get('avg_ret') or 0.0) * 100):7.2f}  "
            f"{((m.get('median_ret') or 0.0) * 100):7.2f}  "
            f"{((m.get('std_ret') or 0.0) * 100):7.2f}  "
            f"{((m.get('best_ret') or 0.0) * 100):7.2f}  "
            f"{((m.get('worst_ret') or 0.0) * 100):7.2f}  "
            f"{fmt_pf(m.get('profit_factor'))}  "
            f"{(float(m.get('avg_hold_days') or 0.0)):5.1f}"
        )

        if show_mfe_cols:
            line += (
                f"  {fmt7(m.get('avg_mfe_pct'))}"
                f"  {fmt7(m.get('avg_mae_pct'))}"
                f"  {fmt7(m.get('avg_capture_pct'))}"
            )

        print(line)

def _row_key_entry(r) -> str:
    return r.entry_signal_key or "UNKNOWN"

def _row_key_exit(r) -> str:
    return r.exit_signal_key or "UNKNOWN"

def _row_key_combo(r) -> str:
    return f"{_row_key_entry(r)} × {_row_key_exit(r)}"

def _parse_combo_key(key: str) -> tuple[str, str] | None:
    """Parse a combo key into (entry_key, exit_key).

    Accepts:
      - "ENTRY × EXIT" (preferred)
      - "ENTRY x EXIT" / "ENTRY X EXIT"
      - "ENTRY | EXIT"
      - "ENTRY  EXIT" (2+ spaces)
    """
    if not key:
        return None
    s = key.strip()

    # Split on: ×, x/X with surrounding whitespace, |, or 2+ spaces.
    parts = re.split(r"\s*[×|]\s*|\s+[xX]\s+|\s{2,}", s, maxsplit=1)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) != 2:
        return None
    return parts[0], parts[1]

def _filter_journal_rows(rows, *, mode: str, key: str | None):
    if not key:
        return rows
    key = key.strip()
    if mode == "entry":
        return [r for r in rows if _row_key_entry(r) == key]
    if mode == "exit":
        return [r for r in rows if _row_key_exit(r) == key]
    if mode == "combo":
        # allow exact combo key string, or parse into entry/exit pieces
        parsed = _parse_combo_key(key)
        if parsed:
            ek, xk = parsed
            return [r for r in rows if _row_key_entry(r) == ek and _row_key_exit(r) == xk]
        return [r for r in rows if _row_key_combo(r) == key]
    raise ValueError(f"unknown mode: {mode}")

def _fmt_pct(x: float | None) -> str:
    return "   n/a" if x is None else f"{x:7.2f}"

def _print_closings(rows, *, show: int, excursions: dict[int, object] | None = None) -> None:
    excursions = excursions or {}

    print("\nCLOSINGS (drill-down)")
    print("---------------------")
    print("exit_date   symbol         qty            entry->exit         pnl      ret%  hold  closing_id   mfe%    mae%    cap%")

    for r in rows[:show]:
        # adapt these field names if your row is dataclass vs dict
        closing_id = r.closing_id if hasattr(r, "closing_id") else r["closing_id"]
        symbol = r.symbol if hasattr(r, "symbol") else r["symbol"]
        qty = r.qty_closed if hasattr(r, "qty_closed") else r["qty_closed"]
        entry_price = r.entry_price if hasattr(r, "entry_price") else r["entry_price"]
        exit_price = r.exit_price if hasattr(r, "exit_price") else r["exit_price"]
        pnl = r.realized_pnl if hasattr(r, "realized_pnl") else r["realized_pnl"]
        ret = r.realized_ret if hasattr(r, "realized_ret") else r["realized_ret"]
        hold = r.holding_days if hasattr(r, "holding_days") else r["holding_days"]
        exit_dt = r.exit_filled_at if hasattr(r, "exit_filled_at") else r["exit_filled_at"]
        exit_date = (exit_dt[:10] if exit_dt else "")

        ex = excursions.get(int(closing_id))
        mfe = getattr(ex, "mfe_pct", None) if ex else None
        mae = getattr(ex, "mae_pct", None) if ex else None
        cap = getattr(ex, "capture_pct", None) if ex else None

        ret_pct = None if ret is None else ret * 100.0

        print(
            f"{exit_date:10s}  {symbol:6s}  {qty:10.6f}  "
            f"{entry_price:8.2f}-> {exit_price:8.2f}  "
            f"{pnl:8.2f}  {ret_pct:7.2f}  {hold:4d}  {closing_id:10d}  "
            f"{_fmt_pct(mfe)}  {_fmt_pct(mae)}  {_fmt_pct(cap)}"
        )

def cmd_attrib_entry(since: str | None, limit: int | None, top: int, key: str | None, show: int) -> int:
    from trading.db import connect
    from trading.analytics.journal import fetch_trade_journal, attrib_by_entry

    with connect() as conn:
        rows = fetch_trade_journal(conn, since=since, limit=limit)
        filtered = _filter_journal_rows(rows, mode="entry", key=key)

        stats = attrib_by_entry(filtered)
        _print_attrib_table("ENTRY SIGNAL ATTRIBUTION", stats, top=top)

        if show and filtered:
            closings = [r.__dict__ if hasattr(r, "__dict__") else r for r in filtered]
            exc_map = compute_excursions_for_closings(conn, closings=closings)

            _print_closings(filtered, show=show, excursions=exc_map)
        elif show:
            _print_closings([], show=show, excursions={})

    return 0

def cmd_attrib_exit(since: str | None, limit: int | None, top: int, key: str | None, show: int, mfe: bool) -> int:
    from trading.db import connect
    from trading.analytics.journal import fetch_trade_journal, attrib_by_exit

    with connect() as conn:
        rows = fetch_trade_journal(conn, since=since, limit=limit)
        filtered = _filter_journal_rows(rows, mode="exit", key=key)

        stats = attrib_by_exit(filtered)

        if mfe and filtered:
            stats = _attach_mfe_stats(stats=stats, filtered_rows=filtered, mode="exit", conn=conn)

        _print_attrib_table("EXIT RULE ATTRIBUTION", stats, top=top)

        if show and filtered:
            closings = [r.__dict__ if hasattr(r, "__dict__") else r for r in filtered]
            exc_map = compute_excursions_for_closings(conn, closings=closings)
            _print_closings(filtered, show=show, excursions=exc_map)
        elif show:
            _print_closings([], show=show, excursions={})

    return 0

def cmd_attrib_combo(since: str | None, limit: int | None, top: int, key: str | None, show: int, mfe: bool) -> int:
    from trading.db import connect
    from trading.analytics.journal import fetch_trade_journal, attrib_by_combo

    with connect() as conn:
        rows = fetch_trade_journal(conn, since=since, limit=limit)
        filtered = _filter_journal_rows(rows, mode="combo", key=key)

        stats = attrib_by_combo(filtered)

        if mfe and filtered:
            stats = _attach_mfe_stats(stats=stats, filtered_rows=filtered, mode="combo", conn=conn)

        _print_attrib_table("ENTRY × EXIT COMBO ATTRIBUTION", stats, top=top)

        if show and filtered:
            closings = [r.__dict__ if hasattr(r, "__dict__") else r for r in filtered]
            exc_map = compute_excursions_for_closings(conn, closings=closings)
            _print_closings(filtered, show=show, excursions=exc_map)
        elif show:
            _print_closings([], show=show, excursions={})

    return 0

def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()

def _since_from_lookback(days: int) -> str:
    d = datetime.now(timezone.utc).date() - timedelta(days=days)
    return d.isoformat()

def _score_bucket(avg_ret_pct: float, avg_mae_pct: float | None, avg_capture_pct: float | None) -> float:
    """
    Simple v1 scoring:
      + reward returns
      + reward capture a bit
      - penalize downside (MAE magnitude)
    """
    mae_pen = 0.0 if avg_mae_pct is None else 0.25 * abs(avg_mae_pct)
    cap_bonus = 0.0 if avg_capture_pct is None else 0.02 * avg_capture_pct
    return avg_ret_pct + cap_bonus - mae_pen

def _recommend_label(*, trades: int, avg_ret_pct: float, profit_factor: float | None) -> str:
    pf = profit_factor if profit_factor is not None else 0.0

    # v1 rules (keep it simple and explainable)
    if trades < 1:
        return "N/A"
    if avg_ret_pct >= 0.50 and pf >= 1.30:
        return "BOOST"
    if avg_ret_pct > 0.00 and pf >= 1.00:
        return "KEEP"
    if avg_ret_pct <= -0.50 and pf < 1.00:
        return "REDUCE"
    if avg_ret_pct <= -1.00 and pf < 0.80:
        return "DISABLE"
    return "WATCH"

def _print_policy_table(title: str, rows: list[dict], *, top: int) -> None:
    def fmt7(x: object | None) -> str:
        return "   n/a" if x is None else f"{float(x):7.2f}"
    print()
    print(title)
    print("-" * len(title))
    print(f"{'key':50s}  {'tr':>4s}  {'avg%':>7s}  {'pf':>6s}  {'mfe%':>7s}  {'mae%':>7s}  {'cap%':>7s}  {'score':>8s}  {'rec':>7s}")

    for r in rows[:top]:
        pf = r.get("pf")
        pf_s = "inf" if pf == float("inf") else f"{(pf if pf is not None else 0.0):.2f}"
        print(
            f"{r['key'][:50]:50s}  "
            f"{int(r.get('trades') or 0):4d}  "
            f"{float(r.get('avg_ret_pct') or 0.0):7.2f}  "
            f"{pf_s:>6s}  "
            f"{fmt7(r.get('avg_mfe_pct'))}  "
            f"{fmt7(r.get('avg_mae_pct'))}  "
            f"{fmt7(r.get('avg_capture_pct'))}  "
            f"{float(r.get('score') or 0.0):8.2f}  "
            f"{r.get('rec', ''):>7s}"
        )


def cmd_policy_recommend(
    *,
    since: str | None,
    lookback: int,
    limit: int | None,
    min_entry_trades: int,
    min_exit_trades: int,
    min_combo_trades: int,
    top: int,
    mfe: bool,
    top_exits_per_entry: int,
    emit_json: str | None,
) -> int:
    import json
    from pathlib import Path

    from trading.db import connect
    from trading.analytics.journal import fetch_trade_journal, attrib_by_entry, attrib_by_exit, attrib_by_combo

    def _policy_payload(
        *,
        since: str,
        asof: str,
        mfe: bool,
        min_entry_trades: int,
        min_exit_trades: int,
        min_combo_trades: int,
        entry_rows: list[dict],
        exit_rows: list[dict],
        per_entry: dict[str, list[dict]],
    ) -> dict:
        return {
            "meta": {
                "since": since,
                "asof": asof,
                "mfe": mfe,
                "min_entry_trades": min_entry_trades,
                "min_exit_trades": min_exit_trades,
                "min_combo_trades": min_combo_trades,
            },
            "entry_policy": entry_rows,
            "exit_policy": exit_rows,
            "best_exits_per_entry": per_entry,
        }

    if since is None:
        since = _since_from_lookback(lookback)

    with connect() as conn:
        journal_rows = fetch_trade_journal(conn, since=since, limit=limit)

        if not journal_rows:
            print(f"POLICY RECOMMENDATIONS: no realized trades since {since}")
            return 0

        entry_stats = attrib_by_entry(journal_rows)
        exit_stats = attrib_by_exit(journal_rows)
        combo_stats = attrib_by_combo(journal_rows)

        if mfe:
            entry_stats = _attach_mfe_stats(stats=entry_stats, filtered_rows=journal_rows, mode="entry", conn=conn)
            exit_stats  = _attach_mfe_stats(stats=exit_stats,  filtered_rows=journal_rows, mode="exit",  conn=conn)
            combo_stats = _attach_mfe_stats(stats=combo_stats, filtered_rows=journal_rows, mode="combo", conn=conn)

    asof = _today_utc()

    print()
    print(
        f"POLICY RECOMMENDATIONS (advisory) since={since} asof={asof} "
        f"| min_entry={min_entry_trades} min_exit={min_exit_trades} min_combo={min_combo_trades} "
        f"| mfe={mfe}"
    )

    # ---- Entry recommendations ----
    entry_rows: list[dict] = []
    for k, m in entry_stats.items():
        trades = int(m.get("trades") or 0)
        if trades < min_entry_trades:
            continue

        avg_ret_pct = float((m.get("avg_ret") or 0.0) * 100.0)
        pf = m.get("profit_factor")
        avg_mae = m.get("avg_mae_pct")
        avg_mfe = m.get("avg_mfe_pct")
        avg_cap = m.get("avg_capture_pct")

        score = _score_bucket(avg_ret_pct, avg_mae, avg_cap)
        rec = _recommend_label(trades=trades, avg_ret_pct=avg_ret_pct, profit_factor=pf)

        entry_rows.append({
            "key": k,
            "trades": trades,
            "avg_ret_pct": avg_ret_pct,
            "pf": pf,
            "avg_mfe_pct": avg_mfe,
            "avg_mae_pct": avg_mae,
            "avg_capture_pct": avg_cap,
            "score": score,
            "rec": rec,
        })

    entry_rows.sort(key=lambda r: (r["rec"] != "BOOST", -(r["score"] or 0.0)))
    _print_policy_table("ENTRY SIGNAL POLICY", entry_rows, top=top)

    # ---- Exit recommendations ----
    exit_rows: list[dict] = []
    for k, m in exit_stats.items():
        trades = int(m.get("trades") or 0)
        if trades < min_exit_trades:
            continue

        avg_ret_pct = float((m.get("avg_ret") or 0.0) * 100.0)
        pf = m.get("profit_factor")
        avg_mae = m.get("avg_mae_pct")
        avg_mfe = m.get("avg_mfe_pct")
        avg_cap = m.get("avg_capture_pct")

        score = _score_bucket(avg_ret_pct, avg_mae, avg_cap)
        rec = _recommend_label(trades=trades, avg_ret_pct=avg_ret_pct, profit_factor=pf)

        exit_rows.append({
            "key": k,
            "trades": trades,
            "avg_ret_pct": avg_ret_pct,
            "pf": pf,
            "avg_mfe_pct": avg_mfe,
            "avg_mae_pct": avg_mae,
            "avg_capture_pct": avg_cap,
            "score": score,
            "rec": rec,
        })

    exit_rows.sort(key=lambda r: (r["rec"] != "BOOST", -(r["score"] or 0.0)))
    _print_policy_table("EXIT RULE POLICY", exit_rows, top=top)

    # ---- Best exits per entry (combo intelligence) ----
    print()
    print("BEST EXITS PER ENTRY (from combos)")
    print("---------------------------------")

    per_entry: dict[str, list[dict]] = defaultdict(list)
    for combo_key, m in combo_stats.items():
        trades = int(m.get("trades") or 0)
        if trades < min_combo_trades:
            continue

        if "×" in combo_key:
            entry_key, exit_key = [x.strip() for x in combo_key.split("×", 1)]
        else:
            continue

        avg_ret_pct = float((m.get("avg_ret") or 0.0) * 100.0)
        avg_mae = m.get("avg_mae_pct")
        avg_cap = m.get("avg_capture_pct")
        score = _score_bucket(avg_ret_pct, avg_mae, avg_cap)

        per_entry[entry_key].append({
            "exit_key": exit_key,
            "trades": trades,
            "avg_ret_pct": avg_ret_pct,
            "avg_mae_pct": avg_mae,
            "avg_capture_pct": avg_cap,
            "score": score,
        })

    entry_keys_ranked = [r["key"] for r in entry_rows[:top]]
    for ek in entry_keys_ranked:
        xs = per_entry.get(ek, [])
        if not xs:
            continue
        xs.sort(key=lambda r: -(r["score"] or 0.0))
        print(f"\n{ek}")
        for r in xs[:top_exits_per_entry]:
            mae = r.get("avg_mae_pct")
            cap = r.get("avg_capture_pct")
            cap_s = "n/a" if cap is None else f"{cap:.2f}"
            print(
                f"  - {r['exit_key']} | trades={r['trades']} "
                f"| avg%={r['avg_ret_pct']:+.2f} "
                f"| mae%={(mae if mae is not None else 0.0):+.2f} "
                f"| cap%={cap_s} "
                f"| score={r['score']:+.2f}"
            )

    print()

    if emit_json:
        # Save all policy snapshots under ./policies (project-relative)
        base_dir = Path("policies")
        out_path = base_dir / Path(emit_json)

        # Allow nested paths like "weekly/policy_....json"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        payload = _policy_payload(
            since=since,
            asof=asof,
            mfe=mfe,
            min_entry_trades=min_entry_trades,
            min_exit_trades=min_exit_trades,
            min_combo_trades=min_combo_trades,
            entry_rows=entry_rows,
            exit_rows=exit_rows,
            per_entry=per_entry,
        )

        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote policy snapshot: {out_path.as_posix()}")

    print("NOTE: This command is advisory only. No trading behavior is changed.")
    return 0

def cmd_backtest_fetch_bars(
    start: str,
    end: str,
    universe: str = "sp500",
    symbols: list[str] | None = None,
    db: str | None = None,
    batch: int = 50,
    sleep_ms: int = 100,
) -> int:
    """
    Fetch split-adjusted daily bars from yfinance into the backtest DB.
 
    Examples:
        # Fetch all S&P 500 symbols for a 2-year backtest window
        trading backtest-fetch-bars --start 2023-01-01 --end 2025-12-31
 
        # Fetch specific symbols only
        trading backtest-fetch-bars --start 2023-01-01 --end 2025-12-31 --symbols AAPL MSFT SPY
 
        # Use a custom backtest DB path
        trading backtest-fetch-bars --start 2023-01-01 --end 2025-12-31 --db /data/bt.sqlite
    """
    import logging
    log = logging.getLogger("trading")
 
    from .backtest.yf_loader import fetch_bars_yfinance, symbols_from_universe, bar_coverage_summary
 
    # Resolve symbol list
    if symbols:
        sym_list = [s.strip().upper() for s in symbols]
        # Always include SPY for the market gate
        if "SPY" not in sym_list:
            sym_list.append("SPY")
        log.info("Fetching %d explicit symbols", len(sym_list))
    else:
        sym_list = symbols_from_universe(universe)
        log.info("Fetching %d symbols from universe '%s'", len(sym_list), universe)
 
    log.info("Date range: %s → %s", start, end)
 
    result = fetch_bars_yfinance(
        symbols=sym_list,
        start=start,
        end=end,
        backtest_db_path=db,
        sleep_ms=sleep_ms,
        batch_size=batch,
    )
 
    # Print summary
    print(f"\n=== Backtest Bar Fetch Complete ===")
    print(f"  Symbols requested : {result['total_symbols']}")
    print(f"  Symbols OK        : {result['ok']}")
    print(f"  Bars stored       : {result['bars_stored']:,}")
    if result["failed"]:
        print(f"  Failed ({len(result['failed'])})        : {', '.join(result['failed'][:20])}")
        if len(result["failed"]) > 20:
            print(f"                      ... and {len(result['failed']) - 20} more")
    if result["skipped"]:
        print(f"  Skipped ({len(result['skipped'])})       : {', '.join(result['skipped'][:20])}")
 
    # Show current DB coverage
    cov = bar_coverage_summary(db)
    print(f"\n=== Backtest DB Coverage ===")
    print(f"  Symbols with data : {cov['symbol_count']}")
    print(f"  Date range        : {cov['min_date']} → {cov['max_date']}")
    print(f"  Total bar rows    : {cov['total_rows']:,}")
    print()
 
    return 0 if not result["failed"] else 1

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

    # risk (Phase 8)
    p_risk = sub.add_parser("risk", help="Institutional risk controls (state machine + circuit breaker)")
    risk_sub = p_risk.add_subparsers(dest="risk_cmd", required=True)

    risk_sub.add_parser("status", help="Show current risk state, drawdown, limits, and recent risk events")

    p_rset = risk_sub.add_parser("set", help="Set an operator override state (SELL_ONLY or HALT_ALL recommended)")
    p_rset.add_argument("--state", required=True, choices=["NORMAL", "PAUSE_BUYS", "SELL_ONLY", "HALT_ALL"])
    p_rset.add_argument("--reason", required=True)
    p_rset.add_argument("--expires-minutes", type=int, default=None, help="Optional TTL for override")

    p_rclr = risk_sub.add_parser("clear", help="Clear operator override (system re-evaluates state next run)")
    p_rclr.add_argument("--reason", default="operator_clear", help="Optional note for audit log")

    p_rpk = risk_sub.add_parser("reset-peak", help="Reset peak equity anchor used for drawdown calculations")
    p_rpk.add_argument("--reason", required=True)

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
    p_ex.add_argument("--emit-intents", action="store_true", help="Write SELL intents for advisor SELL rows (no orders placed)")

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

    p_pbs = sub.add_parser("perf-by-signal", help="Realized performance grouped by ENTRY signal_key (from lot_closings).")
    p_pbs.add_argument("--since", default=None, help="Start date YYYY-MM-DD (inclusive). Default: 90d ago.")
    p_pbs.add_argument("--to", dest="to_date", default=None, help="End date YYYY-MM-DD (inclusive). Default: today.")

    p_pbso = sub.add_parser("perf-by-signal-open", help="Unrealized performance grouped by entry_signal_key (positions).")
    p_pbso.add_argument("--asof", default=None, help="As-of date YYYY-MM-DD (default: latest market date in DB).")

    p_pbst = sub.add_parser("perf-by-signal-total", help="Combined realized+unrealized performance by entry_signal_key.")
    p_pbst.add_argument("--since", default=None, help="Start date YYYY-MM-DD (inclusive). Default: 90d ago.")
    p_pbst.add_argument("--to", dest="to_date", default=None, help="End date YYYY-MM-DD (inclusive). Default: today.")
    p_pbst.add_argument("--asof", default=None, help="As-of date for unrealized snapshot (default: --to or latest).")

    pae = sub.add_parser("attrib-entry", help="Entry signal attribution (realized trades)")
    pae.add_argument("--since", default=None, help="YYYY-MM-DD")
    pae.add_argument("--limit", type=int, default=None, help="Max journal rows to read")
    pae.add_argument("--top", type=int, default=25, help="Show top N keys")
    pae.add_argument("--key", default=None, help="Filter to a specific key (exact match)")
    pae.add_argument("--show", type=int, default=0, help="Show N underlying closings (drill-down)")

    paex = sub.add_parser("attrib-exit", help="Entry signal attribution (realized trades)")
    paex.add_argument("--since", default=None, help="YYYY-MM-DD")
    paex.add_argument("--limit", type=int, default=None, help="Max journal rows to read")
    paex.add_argument("--top", type=int, default=25, help="Show top N keys")
    paex.add_argument("--key", default=None, help="Filter to a specific key (exact match)")
    paex.add_argument("--show", type=int, default=0, help="Show N underlying closings (drill-down)")
    paex.add_argument("--mfe", action="store_true", help="Compute MFE/MAE/Capture bucket metrics (uses bars_daily)")

    pacb = sub.add_parser("attrib-combo", help="Entry signal attribution (realized trades)")
    pacb.add_argument("--since", default=None, help="YYYY-MM-DD")
    pacb.add_argument("--limit", type=int, default=None, help="Max journal rows to read")
    pacb.add_argument("--top", type=int, default=25, help="Show top N keys")
    pacb.add_argument("--key", default=None, help="Filter to a specific key (exact match)")
    pacb.add_argument("--show", type=int, default=0, help="Show N underlying closings (drill-down)")
    pacb.add_argument("--mfe", action="store_true", help="Compute MFE/MAE/Capture bucket metrics (uses bars_daily)")

    pp = sub.add_parser("policy-recommend", help="Phase 5: advisory policy recommendations from realized stats")
    pp.add_argument("--since", default=None, help="YYYY-MM-DD (optional). If omitted, uses --lookback days.")
    pp.add_argument("--lookback", type=int, default=180, help="Lookback days if --since not provided (default 180)")
    pp.add_argument("--limit", type=int, default=None, help="Max journal rows to read")
    pp.add_argument("--min-trades", type=int, default=None, help="Shortcut: sets entry/exit/combo mins")
    pp.add_argument("--min-entry-trades", type=int, default=10)
    pp.add_argument("--min-exit-trades", type=int, default=10)
    pp.add_argument("--min-combo-trades", type=int, default=10)
    pp.add_argument("--top", type=int, default=25, help="Show top N rows in each section (default 25)")
    pp.add_argument("--mfe", action="store_true", help="Include MFE/MAE/Capture in recommendations (uses bars_daily)")
    pp.add_argument("--top-exits-per-entry", type=int, default=3, help="Show top N exits per entry (default 3)")
    pp.add_argument("--emit-json", default=None, help="Write recommendations to a JSON file")

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


    p_exp = sub.add_parser("exposure", help="Exposure by symbol (market value + percentage of equity) (Phase 3)")
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

    p_bt = sub.add_parser("backtest", help="Backtest a strategy on historical data (no live orders).")
    p_bt.add_argument("--strategy", required=True, choices=["sma", "mrit"], help="Strategy to backtest")
    p_bt.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    p_bt.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    p_bt.add_argument("--universe", default="sp500", help="Universe name (default: sp500)")
    p_bt.add_argument("--initial-capital", type=float, default=100_000.0, help="Starting capital (default: 100000)")
    p_bt.add_argument("--max-positions", type=int, default=20, help="Max simultaneous positions (default: 20)")
    p_bt.add_argument("--per-position", type=float, default=None, help="Notional per position (default: capital/max-positions)")
    p_bt.add_argument("--fast", type=int, default=20, help="SMA fast period (SMA strategy, default: 20)")
    p_bt.add_argument("--slow", type=int, default=50, help="SMA slow period (SMA strategy, default: 50)")
    p_bt.add_argument("--db", default=None, help="Path to backtest DB (default: backtest.sqlite)")

    p_btfb = sub.add_parser(
        "backtest-fetch-bars",
        help="Fetch split-adjusted daily bars into the backtest DB via yfinance.",
    )
    p_btfb.add_argument("--start",    required=True,  help="Start date YYYY-MM-DD (fetch from)")
    p_btfb.add_argument("--end",      required=True,  help="End date YYYY-MM-DD (fetch to)")
    p_btfb.add_argument("--universe", default="sp500", help="Universe to fetch (default: sp500)")
    p_btfb.add_argument("--symbols",  nargs="+", default=None,
                        help="Override with specific symbols instead of universe")
    p_btfb.add_argument("--db",       default=None,   help="Backtest DB path (default: backtest.sqlite)")
    p_btfb.add_argument("--batch",    type=int, default=50,
                        help="Symbols per yfinance batch download (default: 50)")
    p_btfb.add_argument("--sleep-ms", type=int, default=100,
                        help="Sleep between batches in ms (default: 100)")

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

    if args.cmd == "backtest":
        from .backtest.engine import run_backtest
        from .backtest.report import print_report
        log.info(
            "BACKTEST starting strategy=%s start=%s end=%s universe=%s capital=%.2f",
            args.strategy, args.start, args.end, args.universe, args.initial_capital,
        )
        summary = run_backtest(
            strategy=args.strategy,
            start=args.start,
            end=args.end,
            universe=args.universe,
            initial_capital=args.initial_capital,
            max_positions=args.max_positions,
            per_position_notional=args.per_position,
            backtest_db_path=args.db,
            fast=args.fast,
            slow=args.slow,
        )
        print_report(summary, strategy=args.strategy, start=args.start, end=args.end)
        return 0
    
    if args.cmd == "backtest-fetch-bars":
        return cmd_backtest_fetch_bars(
            start=args.start,
            end=args.end,
            universe=args.universe,
            symbols=args.symbols,
            db=args.db,
            batch=args.batch,
            sleep_ms=args.sleep_ms,
        )

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
        from .db import connect

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

        sql = """
                SELECT
                COALESCE(i.signal_key, 'unknown') AS exit_signal_key,
                COUNT(*) AS trades,
                ROUND(SUM(lc.realized_pnl), 2) AS pnl
                FROM lot_closings lc
                LEFT JOIN executions e ON e.id = lc.exit_execution_id
                LEFT JOIN orders o     ON o.id = e.order_id
                LEFT JOIN intents i    ON i.id = o.intent_id
                WHERE substr(lc.exit_filled_at, 1, 10) BETWEEN ? AND ?
                GROUP BY COALESCE(i.signal_key, 'unknown')
                ORDER BY SUM(lc.realized_pnl) DESC;
                """

        with connect() as conn:
            rows = conn.execute(sql, (res.start_date, res.end_date)).fetchall()

        if rows:
            total_trades = sum(int(r["trades"]) for r in rows)
            log.info("EXIT ATTRIBUTION (realized) %s→%s | trades=%s", res.start_date, res.end_date, total_trades)
            for r in rows:
                log.info(
                    "  %-32s trades=%-3s pnl=%+8.2f",
                    r["exit_signal_key"],
                    int(r["trades"]),
                    float(r["pnl"] or 0.0),
                )

        return 0
    
    if args.cmd == "perf-by-signal":
        return cmd_perf_by_signal(args.since, args.to_date)
    
    if args.cmd == "perf-by-signal-open":
        return cmd_perf_by_signal_open(args.asof)

    if args.cmd == "perf-by-signal-total":
        return cmd_perf_by_signal_total(args.since, args.to_date, args.asof)

    if args.cmd == "attrib-entry":
        return cmd_attrib_entry(args.since, args.limit, args.top, getattr(args, 'key', None), getattr(args, 'show', 0))
    
    if args.cmd == "attrib-exit":
        return cmd_attrib_exit(args.since, args.limit, args.top, getattr(args, "key", None), getattr(args, "show", 0), getattr(args, "mfe", False))
    
    if args.cmd == "attrib-combo":
        return cmd_attrib_combo(args.since, args.limit, args.top, getattr(args, 'key', None), getattr(args, 'show', 0),getattr(args, "mfe", False))

    if args.cmd == "policy-recommend":
        me = getattr(args, "min_entry_trades", 10)
        mx = getattr(args, "min_exit_trades", 10)
        mc = getattr(args, "min_combo_trades", 10)
        mt = getattr(args, "min_trades", None)
        if mt is not None:
            me = mx = mc = mt

        return cmd_policy_recommend(
            since=args.since,
            lookback=args.lookback,
            limit=args.limit,
            min_entry_trades=me,
            min_exit_trades=mx,
            min_combo_trades=mc,
            top=args.top,
            mfe=args.mfe,
            top_exits_per_entry=args.top_exits_per_entry,
            emit_json=args.emit_json
        )

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
        
        policy = None
        try:
            policy = load_latest_policy(policies_dir="policies")
        except Exception:
            policy = None

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
            note = _format_policy_exit_annotation(
                policy,
                entry_key=r.get("entry_signal_key"),
                exit_key=r.get("exit_signal_key"),
            )

            rationale = r.get("rationale") or ""
            if note:
                rationale = f"{rationale} | {note}"

            # exact fields from your exit rows
            qty = r.get("qty_open")
            uret = r.get("unrealized_ret_pct")  # percent, already
            upnl = r.get("unrealized_pnl")

            hold = r.get("holding_days")
            dd = r.get("drawdown_from_peak_pct")
            peak = r.get("peak_gain_pct")

            vwap = r.get("vwap_entry")
            last = r.get("last_close")

            def _f(v, default=None):
                try:
                    return float(v)
                except Exception:
                    return default

            def _i(v, default=None):
                try:
                    return int(v)
                except Exception:
                    return default

            qty_f = _f(qty)
            uret_f = _f(uret)
            upnl_f = _f(upnl)
            dd_f = _f(dd)
            peak_f = _f(peak)
            vwap_f = _f(vwap)
            last_f = _f(last)
            hold_i = _i(hold)

            qty_s  = f"{qty_f:8.4f}" if qty_f is not None else "     n/a"
            uret_s = f"{uret_f:+7.2f}%" if uret_f is not None else "   n/a "
            upnl_s = f"{upnl_f:+9.2f}" if upnl_f is not None else "     n/a"
            dd_s   = f"{dd_f:+7.2f}%" if dd_f is not None else "   n/a "
            peak_s = f"{peak_f:+7.2f}%" if peak_f is not None else "   n/a "
            hold_s = f"{hold_i:3d}d" if hold_i is not None else " n/a"

            vwap_s = f"{vwap_f:8.2f}" if vwap_f is not None else "     n/a"
            last_s = f"{last_f:8.2f}" if last_f is not None else "     n/a"

            log.info(
                "EXIT  entry=%-26s sym=%-6s qty=%s uret=%s upnl=%s hold=%s dd=%s peak=%s "
                "vwap=%s last=%s exit=%-28s %s",
                r.get("entry_signal_key", ""),
                r.get("symbol", ""),
                qty_s,
                uret_s,
                upnl_s,
                hold_s,
                dd_s,
                peak_s,
                vwap_s,
                last_s,
                r.get("exit_signal_key", ""),
                rationale,
            )

        if args.emit_intents:
            summary = emit_sell_intents(asof=args.asof, cfg=cfg, dry_run=args.dry_run, policy=policy)
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

    if args.cmd == "risk":
        if args.risk_cmd == "status":
            return cmd_risk_status()
        if args.risk_cmd == "set":
            return cmd_risk_set(args.state, args.reason, args.expires_minutes)
        if args.risk_cmd == "clear":
            from .risk.events import emit_event
            s = get_settings() 
            env = getattr(s, 'env', 'paper')
            emit_event(env=env, event_type='OVERRIDE_CLEAR_NOTE', prev_state=None, new_state=None, metrics={}, reason=args.reason, actor='operator')
            return cmd_risk_clear()
        if args.risk_cmd == "reset-peak":
            return cmd_risk_reset_peak(args.reason)
        return 2

    return 1