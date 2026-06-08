from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo

from .db import connect, init_db
from .cooldown import is_in_cooldown
from .lock import acquire_lock as shared_acquire_lock, release_lock as shared_release_lock, read_lock_info
from trading.policy.loader import load_latest_policy, format_policy_summary
from trading.risk.state import seed_risk_defaults, get_effective_state
from trading.risk.circuit_breaker import evaluate_and_apply
from trading.risk.daily import upsert_risk_daily

import logging

from .market_calendar import today_ny_str, is_trading_day
from .pnl.snapshots import upsert_account_snapshot

NY = ZoneInfo("America/New_York")

log = logging.getLogger("trading")

@dataclass
class RunResult:
    run_id: int
    status: str  # success|failed|skipped
    asof: str | None = None
    summary: dict | None = None
    reason: str | None = None


def start_run(notes: str | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (notes) VALUES (?);",
            (notes,),
        )
        return int(cur.lastrowid)


def finish_run(
    run_id: int,
    status: str = "success",
    *,
    asof: str | None = None,
    reason: str | None = None,
    summary: dict | None = None,
    regime: dict | None = None,
) -> None:
    updates = ["finished_at = datetime('now')", "status = ?"]
    params: list = [status]

    optional: dict = {
        "asof_date": asof,
        "reason": reason,
        "summary_json": json.dumps(summary) if summary is not None else None,
    }

    if regime is not None:
        optional["regime"] = regime.get("regime")
        tu = regime.get("trending_up")
        optional["regime_trending_up"] = int(tu) if tu is not None else None
        mp = regime.get("momentum_positive")
        optional["regime_momentum_positive"] = int(mp) if mp is not None else None
        optional["regime_buy_notional_mult"] = regime.get("buy_notional_mult")
        optional["regime_exposure_cap_mult"] = regime.get("exposure_cap_mult")

    with connect() as conn:
        cols = conn.execute("PRAGMA table_info(runs);").fetchall()
        existing = {c["name"] for c in cols}

        for col, val in optional.items():
            if col in existing and val is not None:
                updates.append(f"{col} = ?")
                params.append(val)

        params.append(run_id)

        conn.execute(
            f"UPDATE runs SET {', '.join(updates)} WHERE id = ?;",
            params,
        )

# ---------- Cron-safe lock (file-based) ----------
def _lock_path() -> str:
    # Put lock next to the sqlite DB file for that machine
    from .config import get_settings
    s = get_settings()
    db_dir = os.path.dirname(os.path.abspath(s.db_path))
    return os.path.join(db_dir, "trading_run_once.lock")


def acquire_lock() -> tuple[bool, str]:
    """Use the shared lock implementation so all writer entrypoints honor the same lock file."""
    return shared_acquire_lock(_lock_path())


def release_lock(path: str) -> None:
    shared_release_lock(path)


def _is_weekend(yyyy_mm_dd: str) -> bool:
    # yyyy_mm_dd expected from asof resolver
    d = datetime.strptime(yyyy_mm_dd, "%Y-%m-%d").date()
    return d.weekday() >= 5  # 5=Sat, 6=Sun


def _generate_pyramid_signals(asof: str) -> list:
    """Generate pyramid add signals for open portfolio positions that have crossed a gain threshold."""
    from .pyramid import load_pyramid_config, check_pyramid_add
    from .strategy_sma import Signal
    from .db import connect

    config = load_pyramid_config()
    if config is None:
        return []

    signals = []
    with connect() as conn:
        rows = conn.execute("""
            SELECT
                p.symbol,
                p.entry_notional,
                p.original_entry_price,
                p.entry_notional_original,
                p.pyramid_rungs_hit,
                b.c AS last_close
            FROM positions p
            LEFT JOIN (
                SELECT bd.symbol, bd.c
                FROM bars_daily bd
                JOIN (
                    SELECT UPPER(symbol) AS symbol, MAX(t) AS tmax
                    FROM bars_daily
                    WHERE t <= ?
                    GROUP BY UPPER(symbol)
                ) mx ON UPPER(bd.symbol) = mx.symbol AND bd.t = mx.tmax
            ) b ON UPPER(b.symbol) = UPPER(p.symbol)
            WHERE p.qty > 0.000001
              AND p.entry_signal_key LIKE 'portfolio_%'
              AND p.entry_signal_key NOT LIKE 'portfolio_pyramid_add_%';
        """, (asof,)).fetchall()

    for r in rows:
        sym = str(r["symbol"] or "").upper().strip()
        if not sym:
            continue
        orig_ep = r["original_entry_price"]
        orig_notional = r["entry_notional_original"]
        curr_notional = r["entry_notional"]
        last_close = r["last_close"]

        if orig_ep is None or orig_notional is None or curr_notional is None or last_close is None:
            continue

        try:
            orig_ep_f = float(orig_ep)
            orig_notional_f = float(orig_notional)
            curr_notional_f = float(curr_notional)
            last_close_f = float(last_close)
        except (TypeError, ValueError):
            continue

        try:
            rungs_hit: set[int] = set(json.loads(r["pyramid_rungs_hit"] or "[]"))
        except (json.JSONDecodeError, TypeError):
            rungs_hit = set()

        result = check_pyramid_add(
            original_entry_price=orig_ep_f,
            entry_notional_original=orig_notional_f,
            current_notional=curr_notional_f,
            current_price=last_close_f,
            rungs_hit=rungs_hit,
            config=config,
        )
        if result is None:
            continue

        rung_idx, add_notional = result
        reason = f"PYRAMID:rung={rung_idx}:add_notional={add_notional:.2f}"
        signals.append(Signal(sym, "buy", reason, strength=1.0))
        log.debug("PYRAMID signal: %s rung=%d add=$%.2f", sym, rung_idx, add_notional)

    return signals


def run_once(
    *,
    notes: str | None = None,
    universe: str = "sp500",
    top: int = 200,
    min_adv20: float = 20_000_000.0,
    fast: int = 20,
    slow: int = 50,
    execute_requested: bool = False,
) -> RunResult:
    """
    Phase 2 orchestrated daily run:
      brokercheck -> sync orders -> sync positions -> build universe -> plan -> execute (gated) -> final sync
    Cron-safe via lock file.
    """

    init_db()

    env = os.environ.get("TRADING_ENV", "paper")
    seed_risk_defaults(env)

    policy = None

    from trading.broker.factory import make_broker
    broker = make_broker()

    got_lock, lock_path = acquire_lock()
    info = read_lock_info(lock_path)
    if not got_lock:
        log.info("run_once skipped: lock already taken (%s) pid=%s age=%s", info.path, info.owner_pid, info.age_seconds)
        return RunResult(run_id=-1, status="skipped", reason="lock_taken", summary={"lock": lock_path})

    run_id = start_run(notes=notes)
    asof: str | None = None
    summary: dict = {"steps": {}, "params": {"universe": universe, "top": top, "min_adv20": min_adv20, "fast": fast, "slow": slow}}

    def step(name, fn):
        log.info("step=%s start run_id=%s", name, run_id)
        res = fn()
        summary["steps"][name] = res
        log.info("step=%s done run_id=%s res=%s", name, run_id, res)
        return res

    try:
        # 1) Resolve asof_date once (DB-based, you already have this)
        from .asof import resolve_asof_date

        with connect() as conn:
            asof = resolve_asof_date(conn, None)

        asof_date = date.fromisoformat(asof)

        from .market_calendar import last_completed_trading_day
        last_trade_day = last_completed_trading_day(broker)
        if asof_date < last_trade_day:
            log.warning(
                "Bars data stale: asof=%s last_trade_day=%s",
                asof, last_trade_day,
            )
            finish_run(run_id, status="skipped")
            return RunResult(
                run_id=run_id,
                status="skipped",
                asof=asof,
                reason="stale_bars",
            )

        # weekend gating MVP (holidays later via Alpaca calendar)
        if _is_weekend(asof):
            finish_run(run_id, status="skipped", asof=asof, summary=summary, reason="weekend")
            log.info("Run skipped (weekend). run_id=%s asof=%s", run_id, asof)
            return RunResult(run_id=run_id, status="skipped", asof=asof, summary=summary, reason="weekend")

        with connect() as conn:
            # Try to store asof_date if column exists
            try:
                conn.execute("UPDATE runs SET asof_date=? WHERE id=?;", (asof, run_id))
            except Exception:
                pass

        # 1.5) Load latest policy snapshot (read-only for now)
        def _policy_load() -> dict:
            nonlocal policy
            try:
                pol = load_latest_policy()  # loader reads from /policies
                if not pol:
                    with connect() as conn:
                        conn.execute(
                            "UPDATE runs SET policy_loaded=0 WHERE id=?;",
                            (run_id,),
                        )
                    return {"loaded": False}

                policy = pol
                out = {
                    "loaded": True,
                    "name": getattr(pol, "name", None),
                    "asof": getattr(pol, "asof", None),
                    "path": getattr(pol, "path", None),
                }

                with connect() as conn:
                    conn.execute(
                        """
                        UPDATE runs
                        SET policy_loaded=1, policy_asof=?, policy_path=?, policy_error=NULL
                        WHERE id=?;
                        """,
                        (out["asof"], out["path"], run_id),
                    )

                log.info("POLICY %s", format_policy_summary(pol))
                return out

            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                with connect() as conn:
                    conn.execute(
                        """
                        UPDATE runs
                        SET policy_loaded=0, policy_error=?
                        WHERE id=?;
                        """,
                        (msg, run_id),
                    )
                log.warning("policy_load failed: %s", msg)
                return {"loaded": False, "error": msg}


        step("policy_load", _policy_load)

        # 2) Broker check + account snapshot
        from trading.broker.sync import upsert_account, sync_positions
        from .broker.orders_sync import sync_orders

        risk_pre = step('risk_state_pre', lambda: get_effective_state(env))
        if risk_pre.get('state') == 'HALT_ALL' and risk_pre.get('allow_broker') == 0:
            finish_run(run_id, status='skipped', asof=asof, summary=summary, reason='halt_all')
            log.warning('RUN SKIPPED run_id=%s reason=HALT_ALL (broker blocked)', run_id)
            return RunResult(run_id=run_id, status='skipped', asof=asof, summary=summary, reason='halt_all')

        def _brokercheck():
            upsert_account(broker)
            a = broker.get_account()
            return {"broker": a.broker, "status": a.status, "buying_power": a.buying_power, "equity": a.equity}

        step("brokercheck", _brokercheck)

        # 2.1) Portfolio circuit breaker (drawdown-based) + state transitions
        risk = step('risk_circuit_breaker', lambda: evaluate_and_apply(env=env, broker=broker, asof=asof))
        allow_buys = bool(risk.get("allow_buys", 1)) if isinstance(risk, dict) else True
        allow_sells = bool(risk.get('allow_sells', 1))
        allow_broker = bool(risk.get('allow_broker', 1))

        if not allow_broker:
            finish_run(run_id, status='skipped', asof=asof, summary=summary, reason='broker_blocked')
            log.warning('RUN SKIPPED run_id=%s reason=broker_blocked state=%s dd=%.4f', run_id, risk.get('state'), float(risk.get('drawdown_pct') or 0.0))
            return RunResult(run_id=run_id, status='skipped', asof=asof, summary=summary, reason='broker_blocked')

        def _env_bool(name: str, default: bool = False) -> bool:
            v = os.getenv(name)
            if v is None:
                return default
            return str(v).strip().lower() in ("1", "true", "yes", "y", "on")

        defer_post_sync_after_execute = _env_bool("TRADING_DEFER_POST_SYNC_AFTER_EXECUTE", True)

        def _calendar_gate() -> dict:
            ignore = _env_bool("TRADING_IGNORE_MARKET_CALENDAR", False)
            require_after_close = _env_bool("TRADING_REQUIRE_AFTER_CLOSE", False)
            grace = int(os.getenv("TRADING_AFTER_CLOSE_GRACE_MINUTES", "5"))
            trade_day = today_ny_str()

            if ignore:
                return {"skipped": False, "trade_day": trade_day, "reason": "ignored"}

            ok, md = is_trading_day(broker, trade_day)
            if not ok:
                return {"skipped": True, "trade_day": trade_day, "reason": "market_closed"}

            if require_after_close and md and md.close:
                now_ny = datetime.now(tz=NY)

                parts = str(md.close).split(":")
                hh = int(parts[0])
                mm = int(parts[1])

                close_dt = now_ny.replace(hour=hh, minute=mm, second=0, microsecond=0)
                cutoff_dt = close_dt + timedelta(minutes=grace)

                if now_ny < cutoff_dt:
                    return {"skipped": True, "trade_day": trade_day, "reason": "before_close"}

            return {
                "skipped": False,
                "trade_day": trade_day,
                "open": md.open if md else None,
                "close": md.close if md else None,
            }

        cal = step("calendar_check", _calendar_gate)
        if cal.get("skipped"):
            with connect() as conn:
                conn.execute("UPDATE runs SET asof_date=? WHERE id=?;", (cal["trade_day"], run_id))
            finish_run(run_id, status="skipped")
            log.info("RUN SKIPPED run_id=%s reason=%s trade_day=%s", run_id, cal["reason"], cal["trade_day"])
            return RunResult(run_id=run_id, status="skipped", asof=cal["trade_day"], summary=summary, reason=cal["reason"])

        asof = cal["trade_day"]
        with connect() as conn:
            conn.execute("UPDATE runs SET asof_date=? WHERE id=?;", (asof, run_id))

        # 3) Pre-sync orders/positions
        step("sync_orders_pre", lambda: vars(sync_orders(limit=200)))
        step("sync_positions_pre", lambda: {"positions_synced": sync_positions(broker)})

        # 4) Build universe snapshot
        from .universe.build_universe import build_universe_daily
        def _build_universe():
            resolved_asof, n = build_universe_daily(universe=universe, asof=asof, top=top, min_adv20=min_adv20)
            return {"asof": resolved_asof, "rows": n}

        step("build_universe", _build_universe)

        # 5) Plan intents 
        from .strategy_sma import generate_signals_sma
        from .strategy_mrit import generate_signals_mrit  # NEW
        from .planner import plan_intents, save_intents
        from .regime import detect_regime

        def _plan():
            regime = detect_regime(asof)

            mode = os.getenv("TRADING_STRATEGY_MODE", "swing").strip().lower()
            if mode not in ("swing", "portfolio"):
                log.warning("Unknown TRADING_STRATEGY_MODE=%s, defaulting to swing", mode)
                mode = "swing"

            signals = []
            if mode == "swing":
                signals += generate_signals_sma(fast=fast, slow=slow, universe=universe, asof=asof, regime=regime)
                signals += generate_signals_mrit(universe=universe, asof=asof, regime=regime)
            elif mode == "portfolio":
                from .strategy_portfolio import generate_signals_portfolio
                signals += generate_signals_portfolio(universe=universe, asof=asof, regime=regime)

            log.info("STRATEGY MODE: %s | signals=%d", mode, len(signals))
            # de-dup symbols so that it don't double-buy same ticker
            seen = set()
            deduped = []
            for s in sorted(signals, key=lambda x: (x.action != "buy", -(x.strength or 0.0))):
                # keep best BUY per symbol first; holds can be ignored
                if s.symbol in seen and s.action == "buy":
                    continue
                if s.action == "buy":
                    seen.add(s.symbol)
                deduped.append(s)
            signals = deduped

            # Pyramid adds injected AFTER de-dup (they target already-held symbols)
            if mode == "portfolio":
                pyramid_sigs = _generate_pyramid_signals(asof)
                if pyramid_sigs:
                    log.info("PYRAMID: injecting %d add signal(s)", len(pyramid_sigs))
                signals += pyramid_sigs

            intents = plan_intents(
                signals,
                policy=policy,
                allow_buys=bool(risk["allow_buys"]),
                risk_state=risk["state"],
                buy_notional_mult=float(regime.buy_notional_mult),
                exposure_cap_mult=float(regime.exposure_cap_mult),
            )

            risk_blocked = 0
            if not allow_buys:
                filtered = []
                for i in intents:
                    if i.action == 'buy':
                        risk_blocked += 1
                        continue
                    filtered.append(i)
                intents = filtered

            cooldown_days = int(float(os.getenv("TRADING_SYMBOL_COOLDOWN_DAYS", "0")))
            blocked = 0
            if cooldown_days > 0:
                filtered = []
                for i in intents:
                    if i.action == "buy" and is_in_cooldown(i.symbol, asof):
                        blocked += 1
                        continue
                    filtered.append(i)
                intents = filtered

            save_intents(run_id, intents)

            buys = sum(1 for i in intents if i.action == "buy")
            sells = sum(1 for i in intents if i.action == "sell")
            holds = sum(1 for i in intents if i.action == "hold")
            return {
                "intents": len(intents),
                "buys": buys,
                "sells": sells,
                "holds": holds,
                "cooldown_blocked": blocked,
                "risk_buys_blocked": risk_blocked,
                "risk_state": risk.get("state") if isinstance(risk, dict) else None,
                "cooldown_days": cooldown_days,
                "regime": regime.to_dict(),
            }


        step("plan", _plan)

        # 6) Execute (env gated)
        from .execution import execute_run
        def _execute():
            if not execute_requested:
                return {"skipped": True, "reason": "execute_not_requested"}

            # Risk gate: buys already filtered at planning time, but protect execution anyway.
            if not allow_broker:
                return {"skipped": True, "reason": "broker_blocked_by_risk"}

            out = execute_run(
                run_id=run_id,
                qty_default=1.0,
                submit=True,
                retry_failed=False,
                allow_buys=allow_buys,
                allow_sells=allow_sells,
                risk_state=risk.get("state") if isinstance(risk, dict) else None,
            )
            return out

        step("execute", _execute)

        # 7) Final sync
        exec_out = summary["steps"].get("execute", {}) or {}
        post_sync_deferred = bool(defer_post_sync_after_execute and int(exec_out.get("submitted", 0) or 0) > 0)
        summary["steps"]["post_sync_deferred"] = {
            "enabled": bool(defer_post_sync_after_execute),
            "deferred": post_sync_deferred,
            "submitted": int(exec_out.get("submitted", 0) or 0),
        }

        if post_sync_deferred:
            log.info(
                "Deferring post-execution order/position sync for run_id=%s because submitted=%s and TRADING_DEFER_POST_SYNC_AFTER_EXECUTE=true",
                run_id,
                exec_out.get("submitted", 0),
            )
            summary["steps"]["sync_orders_post"] = {"deferred": True}
            summary["steps"]["sync_positions_post"] = {"deferred": True}
        else:
            step("sync_orders_post", lambda: vars(sync_orders(limit=200)))
            step("sync_positions_post", lambda: {"positions_synced": sync_positions(broker)})

        # 7.5) Phase 3: Apply lots from executions (idempotent)
        def _apply_lots():
            from .pnl.lots import apply_new_executions
            r = apply_new_executions(run_id=run_id)
            return {
                "buys": r.buys_processed,
                "sells": r.sells_processed,
                "lots": r.lots_created,
                "closings": r.closings_created,
                "warnings": r.warnings,
            }

        step("lots_apply", _apply_lots)

        # 7.55) Pyramid: persist rung state for filled adds
        def _update_pyramid_rungs():
            from .pyramid import is_pyramid_enabled
            if not is_pyramid_enabled():
                return {"skipped": True, "reason": "pyramid_disabled"}

            with connect() as conn:
                rows = conn.execute("""
                    SELECT i.symbol, i.signal_key
                    FROM intents i
                    JOIN orders o ON o.intent_id = i.id
                    WHERE i.run_id = ?
                      AND i.signal_key LIKE 'portfolio_pyramid_add_rung%'
                      AND o.filled_qty > 0;
                """, (run_id,)).fetchall()

                updated = 0
                for r in rows:
                    sym = str(r["symbol"] or "").upper().strip()
                    sk = str(r["signal_key"] or "")
                    try:
                        rung_idx = int(sk[len("portfolio_pyramid_add_rung"):])
                    except (ValueError, IndexError):
                        continue

                    pos_row = conn.execute(
                        "SELECT pyramid_rungs_hit FROM positions WHERE symbol = ?",
                        (sym,),
                    ).fetchone()
                    if not pos_row:
                        continue

                    try:
                        rungs: set[int] = set(json.loads(pos_row["pyramid_rungs_hit"] or "[]"))
                    except (json.JSONDecodeError, TypeError):
                        rungs = set()

                    rungs.add(rung_idx)
                    conn.execute(
                        """
                        UPDATE positions
                        SET pyramid_rungs_hit = ?,
                            pyramid_last_check_at = datetime('now')
                        WHERE symbol = ?;
                        """,
                        (json.dumps(sorted(rungs)), sym),
                    )
                    updated += 1

            return {"updated": updated}

        step("pyramid_rungs_update", _update_pyramid_rungs)

        # 7.6) Phase 3: Daily account snapshot (equity curve)
                # 7.6) Phase 3: Daily account snapshot (equity curve)
        def _snapshot_account():
            a = broker.get_account()

            def _f(x):
                try:
                    return None if x is None else float(x)
                except Exception:
                    return None

            cash = _f(getattr(a, "cash", None))
            equity = _f(getattr(a, "equity", None))
            buying_power = _f(getattr(a, "buying_power", None))
            long_mv = _f(getattr(a, "long_market_value", None))

            if equity is None:
                return {"skipped": True, "reason": "account_missing_equity"}

            if cash is None:
                # should not happen now, but keep DB constraint safe
                cash = 0.0

            with connect() as conn:
                conn.execute(
                    """
                    INSERT INTO account_snapshots_daily(asof_date, cash, equity, buying_power, long_market_value, run_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(asof_date) DO UPDATE SET
                      cash=excluded.cash,
                      equity=excluded.equity,
                      buying_power=excluded.buying_power,
                      long_market_value=excluded.long_market_value,
                      run_id=excluded.run_id,
                      created_at=datetime('now');
                    """,
                    (asof, cash, equity, buying_power, long_mv, run_id),
                )

            return {"asof": asof, "cash": cash, "equity": equity, "buying_power": buying_power, "long_mv": long_mv}
        
        step("account_snapshot", _snapshot_account)

        # 7.7) Risk daily snapshot (state + drawdown)
        try:
            step('risk_daily', lambda: upsert_risk_daily(env=env, asof=asof, risk=risk))
        except Exception as _e:
            # do not fail the run due to risk_daily persistence issues
            log.warning('risk_daily failed: %s', _e)

        # 8) Finish + one-line report
        _plan_step = summary["steps"].get("plan") or {}
        _regime_dict = _plan_step.get("regime") if isinstance(_plan_step, dict) else None
        finish_run(run_id, status="success", asof=asof, summary=summary, regime=_regime_dict)
        one_line = (
            f"RUN OK run_id={run_id} asof={asof} "
            f"universe={universe} top={top} "
            f"intents={summary['steps'].get('plan', {}).get('intents')} "
            f"buys={summary['steps'].get('plan', {}).get('buys')} "
            f"sells={summary['steps'].get('plan', {}).get('sells')} "
            f"exec={summary['steps'].get('execute', {})}"
        )
        #log.info(one_line)

        exec_out = summary["steps"].get("execute", {})
        exec_skipped = exec_out.get("skipped_reason") or exec_out.get("reason")
        log.info(
            "RUN OK run_id=%s asof=%s universe=%s top=%s intents=%s buys=%s sells=%s holds=%s exec_submitted=%s exec_skipped=%s",
            run_id, asof, universe, top,
            summary["steps"].get("plan", {}).get("intents"),
            summary["steps"].get("plan", {}).get("buys"),
            summary["steps"].get("plan", {}).get("sells"),
            summary["steps"].get("plan", {}).get("holds"),
            exec_out.get("submitted", 0),
            exec_skipped,
        )
        return RunResult(run_id=run_id, status="success", asof=asof, summary=summary)

    except Exception as e:
        # record failure (best effort)
        summary["error"] = f"{type(e).__name__}: {e}"
        finish_run(run_id, status="failed", asof=asof, summary=summary, reason="exception")
        log.exception("run_once failed run_id=%s asof=%s", run_id, asof)
        raise

    finally:
        release_lock(lock_path)
