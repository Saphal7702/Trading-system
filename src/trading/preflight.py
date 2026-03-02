from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .db import connect
from .asof import resolve_asof_date
from .market_calendar import today_ny_str, is_trading_day

NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    asof: str
    staleness_days: int
    market_open_day: bool
    market_reason: str | None
    buying_power: float | None
    equity: float | None
    open_internal_orders: int
    cooldown_active: int
    daily_cap: int

    # NEW: execution safety
    exec_safe: bool
    cap_remaining: int
    min_bp_required: float
    exec_blockers: list[str]

    notes: str | None = None


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return int(default)
    try:
        return int(float(v))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return float(default)
    try:
        return float(v)
    except Exception:
        return float(default)


def run_preflight(*, universe: str = "sp500") -> PreflightResult:
    # 1) DB asof + staleness
    with connect() as conn:
        asof = resolve_asof_date(conn, None)

    today_ny = datetime.now(tz=NY).date()
    asof_date = datetime.strptime(asof, "%Y-%m-%d").date()
    staleness_days = (today_ny - asof_date).days

    # 2) Broker + calendar
    from trading.broker.sync import upsert_account

    from trading.cli import _make_broker
    broker = _make_broker()
    upsert_account(broker)
    acct = broker.get_account()

    trade_day = today_ny_str()
    ignore_cal = _env_bool("TRADING_IGNORE_MARKET_CALENDAR", False)
    if ignore_cal:
        market_open_day = True
        market_reason = "ignored"
    else:
        ok, _md = is_trading_day(broker, trade_day)
        market_open_day = bool(ok)
        market_reason = None if ok else "market_closed"

    buying_power = float(acct.buying_power) if acct.buying_power is not None else None
    equity = float(acct.equity) if acct.equity is not None else None

    # 3) Internal state checks
    with connect() as conn:
        open_orders = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM orders
            WHERE status IN ('created', 'submitting', 'submitted');
            """
        ).fetchone()["c"]

        cooldown_active = 0
        try:
            cooldown_active = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM symbol_cooldowns
                WHERE until_date >= ?;
                """,
                (asof,),
            ).fetchone()["c"]
        except Exception:
            cooldown_active = 0

        # Determine cap usage for "today" (asof day).
        # This is approximate when you run multiple times/day: it counts all submitted/filled orders
        # whose run's asof_date == asof.
        already_submitted_today = 0
        try:
            already_submitted_today = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM orders o
                JOIN runs r ON r.id = o.run_id
                WHERE r.asof_date = ?
                  AND o.status IN ('submitted', 'filled');
                """,
                (asof,),
            ).fetchone()["c"]
        except Exception:
            already_submitted_today = 0

    daily_cap = _env_int("TRADING_DAILY_TRADE_CAP", 999999)
    cap_remaining = max(0, int(daily_cap) - int(already_submitted_today or 0))

    # 4) Existing "ok" (data + trading day)
    max_stale = _env_int("TRADING_MAX_BAR_STALENESS_DAYS", 2)
    ok = (staleness_days <= max_stale) and market_open_day

    # 5) Execution-safe evaluation
    exec_blockers: list[str] = []

    # Paper gate (execution would be blocked)
    if not _env_bool("TRADING_ALLOW_PAPER_ORDERS", False):
        exec_blockers.append("paper_gated(TRADING_ALLOW_PAPER_ORDERS=false)")

    # Cap remaining
    if cap_remaining <= 0:
        exec_blockers.append(f"daily_trade_cap_reached(cap={daily_cap})")

    # Open internal orders
    if int(open_orders or 0) > 0:
        exec_blockers.append(f"open_internal_orders({int(open_orders)})")

    # Buying power realism
    per_position = _env_float("TRADING_PER_POSITION", 100.0)
    haircut = _env_float("TRADING_NOTIONAL_HAIRCUT", 0.98)
    cash_buffer = _env_float("TRADING_CASH_BUFFER", 0.0)
    min_bp_required = float(per_position) * float(haircut) + float(cash_buffer)

    if buying_power is None:
        exec_blockers.append("buying_power_unknown")
    else:
        if buying_power < min_bp_required:
            exec_blockers.append(f"insufficient_buying_power(bp={buying_power:.2f} < min_required={min_bp_required:.2f})")

    # If data/market not ok, execution isn't safe either
    if not ok:
        exec_blockers.append("preflight_not_ok(data_or_market)")

    exec_safe = (len(exec_blockers) == 0)

    return PreflightResult(
        ok=ok,
        asof=asof,
        staleness_days=staleness_days,
        market_open_day=market_open_day,
        market_reason=market_reason,
        buying_power=buying_power,
        equity=equity,
        open_internal_orders=int(open_orders or 0),
        cooldown_active=int(cooldown_active or 0),
        daily_cap=int(daily_cap),

        exec_safe=exec_safe,
        cap_remaining=int(cap_remaining),
        min_bp_required=float(min_bp_required),
        exec_blockers=exec_blockers,

        notes=f"trade_day={trade_day} universe={universe}",
    )