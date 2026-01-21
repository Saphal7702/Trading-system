from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timezone

from .db import connect, init_db
from .cooldown import is_in_cooldown
import logging

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
) -> None:
    updates = ["finished_at = datetime('now')", "status = ?"]
    params: list = [status]

    optional = {
        "asof_date": asof,
        "reason": reason,
        "summary_json": json.dumps(summary) if summary is not None else None,
    }

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
    """
    Uses O_EXCL create for atomic lock.
    Returns (acquired, path).
    """
    path = _lock_path()
    owner = f"{socket.gethostname()} pid={os.getpid()} utc={datetime.now(timezone.utc).isoformat()}"

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(owner + "\n")
        return True, path
    except FileExistsError:
        return False, path


def release_lock(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _is_weekend(yyyy_mm_dd: str) -> bool:
    # yyyy_mm_dd expected from asof resolver
    d = datetime.strptime(yyyy_mm_dd, "%Y-%m-%d").date()
    return d.weekday() >= 5  # 5=Sat, 6=Sun


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

    got_lock, lock_path = acquire_lock()
    if not got_lock:
        log.info("run_once skipped: lock already taken (%s)", lock_path)
        return RunResult(run_id=-1, status="skipped", reason="lock_taken", summary={"lock": lock_path})

    run_id = start_run(notes=notes)
    asof: str | None = None
    summary: dict = {"steps": {}, "params": {"universe": universe, "top": top, "min_adv20": min_adv20, "fast": fast, "slow": slow}}

    def step(name: str, fn):
        log.info("step=%s start run_id=%s", name, run_id)
        out = fn() or {}
        summary["steps"][name] = out
        log.info("step=%s done run_id=%s", name, run_id)

    try:
        # 1) Resolve asof_date once (DB-based, you already have this)
        from .asof import resolve_asof_date

        with connect() as conn:
            asof = resolve_asof_date(conn, None)

            # weekend gating MVP (holidays later via Alpaca calendar)
            if _is_weekend(asof):
                finish_run(run_id, status="skipped", asof=asof, summary=summary, reason="weekend")
                log.info("Run skipped (weekend). run_id=%s asof=%s", run_id, asof)
                return RunResult(run_id=run_id, status="skipped", asof=asof, summary=summary, reason="weekend")

            # Try to store asof_date if column exists
            try:
                conn.execute("UPDATE runs SET asof_date=? WHERE id=?;", (asof, run_id))
            except Exception:
                pass

        # 2) Broker check + account snapshot
        from trading.broker.alpaca_broker import AlpacaPaperBroker
        from trading.broker.sync import upsert_account, sync_positions
        from .broker.orders_sync import sync_orders

        broker = AlpacaPaperBroker()

        def _brokercheck():
            upsert_account(broker)
            a = broker.get_account()
            return {"broker": a.broker, "status": a.status, "buying_power": a.buying_power, "equity": a.equity}

        step("brokercheck", _brokercheck)

        # 3) Pre-sync orders/positions
        step("sync_orders_pre", lambda: vars(sync_orders(limit=200)))
        step("sync_positions_pre", lambda: {"positions_synced": sync_positions(broker)})

        # 4) Build universe snapshot
        from .universe.build_universe import build_universe_daily
        def _build_universe():
            resolved_asof, n = build_universe_daily(universe=universe, asof=asof, top=top, min_adv20=min_adv20)
            return {"asof": resolved_asof, "rows": n}

        step("build_universe", _build_universe)

        # 5) Plan intents (reuse your existing pieces, but not CLI)
        from .strategy_sma import generate_signals_sma
        from .planner import plan_intents, save_intents

        def _plan():
            signals = generate_signals_sma(fast=fast, slow=slow, universe=universe, asof=asof)
            intents = plan_intents(signals)

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
                "cooldown_days": cooldown_days,
            }

        step("plan", _plan)

        # 6) Execute (env gated)
        from .execution import execute_run
        def _execute():
            if not execute_requested:
                return {"skipped": True, "reason": "execute_not_requested"}

            out = execute_run(
                run_id=run_id,
                qty_default=1.0,
                submit=True,
                retry_failed=False,
            )
            return out

        step("execute", _execute)

        # 7) Final sync
        step("sync_orders_post", lambda: vars(sync_orders(limit=200)))
        step("sync_positions_post", lambda: {"positions_synced": sync_positions(broker)})

        # 8) Finish + one-line report
        finish_run(run_id, status="success", asof=asof, summary=summary)
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
        log.info(
            "RUN OK run_id=%s asof=%s universe=%s top=%s intents=%s buys=%s sells=%s exec_submitted=%s exec_skipped=%s",
            run_id, asof, universe, top,
            summary["steps"].get("plan", {}).get("intents"),
            summary["steps"].get("plan", {}).get("buys"),
            summary["steps"].get("plan", {}).get("sells"),
            exec_out.get("submitted"),
            exec_out.get("skipped_reason"),
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
