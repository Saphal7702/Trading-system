from __future__ import annotations

import csv
import logging
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("trading")


@dataclass
class SlippageConfig:
    start: str | None = None
    end: str | None = None
    broker: str | None = None
    report: bool = False
    side: str | None = None       # 'buy' | 'sell' | None
    bucket_by: str = "all"        # 'all' | 'month'
    symbols: list[str] = field(default_factory=list)


# ── stats helpers ────────────────────────────────────────────────────────────

def _mean(vals: list[float]) -> float | None:
    return (sum(vals) / len(vals)) if vals else None


def _median(vals: list[float]) -> float | None:
    return statistics.median(vals) if vals else None


def _percentile(vals: list[float], p: float) -> float | None:
    """Linear-interpolation percentile (0 ≤ p ≤ 1)."""
    if not vals:
        return None
    s = sorted(vals)
    idx = (len(s) - 1) * p
    lo = int(idx)
    hi = lo + 1
    if hi >= len(s):
        return s[lo]
    return s[lo] + (idx - lo) * (s[hi] - s[lo])


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ── data fetching ─────────────────────────────────────────────────────────────

def _fetch_raw(conn: Any, cfg: SlippageConfig) -> list[dict]:
    """
    Fetch every execution row with the full LEFT JOIN chain.
    Returns list of plain dicts so caller can do all classification in Python.
    """
    params: dict[str, Any] = {
        "start": cfg.start,
        "end": cfg.end or _today(),
        "broker": cfg.broker,
    }

    extra_filters: list[str] = []

    if cfg.symbols:
        placeholders = ",".join(f":sym{i}" for i in range(len(cfg.symbols)))
        extra_filters.append(f"e.symbol IN ({placeholders})")
        for i, s in enumerate(cfg.symbols):
            params[f"sym{i}"] = s

    if cfg.side:
        extra_filters.append("e.side = :side")
        params["side"] = cfg.side

    where_extra = ("AND " + " AND ".join(extra_filters)) if extra_filters else ""

    sql = f"""
    SELECT
        e.id                AS exec_id,
        e.broker,
        e.broker_order_id,
        e.symbol,
        e.side,
        e.qty,
        e.filled_avg_price,
        e.filled_at,
        e.order_id,
        o.intent_id         AS o_intent_id,
        i.id                AS intent_id,
        i.signal_key,
        i.run_id            AS i_run_id,
        r.id                AS run_id,
        r.asof_date         AS signal_date,
        b_sig.c             AS signal_close,
        b_fill.o            AS fill_open,
        b_fill.h            AS fill_high,
        b_fill.l            AS fill_low
    FROM executions e
    LEFT JOIN orders      o     ON o.id         = e.order_id
    LEFT JOIN intents     i     ON i.id         = o.intent_id
    LEFT JOIN runs        r     ON r.id         = i.run_id
    LEFT JOIN bars_daily  b_sig ON b_sig.symbol = e.symbol
                               AND b_sig.t      = r.asof_date
    LEFT JOIN bars_daily  b_fill ON b_fill.symbol = e.symbol
                                AND b_fill.t      = date(e.filled_at)
    WHERE (:start IS NULL OR date(e.filled_at) >= :start)
      AND date(e.filled_at) <= :end
      AND (:broker IS NULL OR e.broker = :broker)
      {where_extra}
    ORDER BY e.order_id, e.id
    """
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ── classification ────────────────────────────────────────────────────────────

def _classify(raw_rows: list[dict], skip_counts: dict[str, int]) -> list[dict]:
    """
    Group raw execution rows by order_id, aggregate partial fills, apply all
    skip rules, and compute per-execution slippage metrics.

    Modifies skip_counts in-place.  Returns list of analyzed execution dicts.
    """
    # Group by order_id; executions without an order_id get their own bucket.
    groups: dict[Any, list[dict]] = {}
    for r in raw_rows:
        key = r["order_id"] if r["order_id"] is not None else f"_exec_{r['exec_id']}"
        groups.setdefault(key, []).append(r)

    analyzed: list[dict] = []

    for _, execs in groups.items():
        # ── pre-filters (per raw execution) ──────────────────────────────
        null_broker = [e for e in execs if e["broker_order_id"] is None]
        valid = [e for e in execs if e["broker_order_id"] is not None]
        skip_counts["broker_order_id_null"] = (
            skip_counts.get("broker_order_id_null", 0) + len(null_broker)
        )
        if not valid:
            continue

        no_fill = [e for e in valid if e["filled_avg_price"] is None]
        filled = [e for e in valid if e["filled_avg_price"] is not None]
        skip_counts["no_fill_price"] = skip_counts.get("no_fill_price", 0) + len(no_fill)
        if not filled:
            continue

        # ── chain integrity (use first filled exec for metadata) ─────────
        ref = filled[0]

        if ref["order_id"] is None or ref["o_intent_id"] is None:
            skip_counts["no_intent"] = skip_counts.get("no_intent", 0) + 1
            continue

        if ref["intent_id"] is None or ref["i_run_id"] is None:
            skip_counts["no_intent"] = skip_counts.get("no_intent", 0) + 1
            continue

        if ref["run_id"] is None or ref["signal_date"] is None:
            skip_counts["no_run"] = skip_counts.get("no_run", 0) + 1
            continue

        if ref["signal_close"] is None:
            skip_counts["no_signal_bar"] = skip_counts.get("no_signal_bar", 0) + 1
            continue

        # ── aggregate partial fills ───────────────────────────────────────
        total_qty = sum(float(e["qty"]) for e in filled)
        fill_price = (
            sum(float(e["qty"]) * float(e["filled_avg_price"]) for e in filled)
            / total_qty
        )
        filled_at = max(e["filled_at"] for e in filled if e["filled_at"])
        fill_date = filled_at[:10]      # YYYY-MM-DD
        signal_date: str = ref["signal_date"]

        # ── edge-case: signal generated after fill ────────────────────────
        if signal_date > fill_date:
            skip_counts["signal_after_fill"] = skip_counts.get("signal_after_fill", 0) + 1
            continue

        # ── compute metrics ───────────────────────────────────────────────
        signal_close = float(ref["signal_close"])
        slippage_abs = fill_price - signal_close
        slippage_pct = (slippage_abs / signal_close * 100.0) if signal_close else 0.0
        side: str = ref["side"]
        # positive = unfavorable: paid more on buy, sold for less on sell
        signed_slippage_pct = slippage_pct if side == "buy" else -slippage_pct

        try:
            timing_gap_days = (
                date.fromisoformat(fill_date) - date.fromisoformat(signal_date)
            ).days
        except ValueError:
            timing_gap_days = None

        # ── bar-quality check on fill date ────────────────────────────────
        fill_inside_bar: bool | None = None
        if ref["fill_high"] is not None and ref["fill_low"] is not None:
            fill_inside_bar = float(ref["fill_low"]) <= fill_price <= float(ref["fill_high"])

        analyzed.append({
            "execution_id": ref["exec_id"],
            "symbol": ref["symbol"],
            "side": side,
            "qty": total_qty,
            "signal_date": signal_date,
            "fill_date": fill_date,
            "timing_gap_days": timing_gap_days,
            "signal_close": signal_close,
            "fill_price": fill_price,
            "slippage_abs": slippage_abs,
            "slippage_pct": slippage_pct,
            "signed_slippage_pct": signed_slippage_pct,
            "fill_date_open": ref["fill_open"],
            "fill_date_high": ref["fill_high"],
            "fill_date_low": ref["fill_low"],
            "fill_inside_bar": fill_inside_bar,
            "order_id": ref["order_id"],
            "intent_id": ref["intent_id"],
            "signal_key": ref["signal_key"],
            "run_id": ref["run_id"],
            "broker": ref["broker"],
        })

    return analyzed


# ── aggregate statistics ──────────────────────────────────────────────────────

def _agg(rows: list[dict]) -> dict:
    if not rows:
        return {
            "count": 0,
            "mean_signed_pct": None,
            "median_signed_pct": None,
            "p90_signed_pct": None,
            "dollar_impact": None,
            "mean_timing_gap": None,
        }
    signed = [r["signed_slippage_pct"] for r in rows]
    timing = [r["timing_gap_days"] for r in rows if r["timing_gap_days"] is not None]
    # dollar_impact: negative when unfavorable (= cost to P&L)
    dollar_impact = sum(
        -r["signed_slippage_pct"] / 100.0 * r["qty"] * r["fill_price"] for r in rows
    )
    return {
        "count": len(rows),
        "mean_signed_pct": _mean(signed),
        "median_signed_pct": _median(signed),
        "p90_signed_pct": _percentile(signed, 0.9),
        "dollar_impact": dollar_impact,
        "mean_timing_gap": _mean(timing),
    }


# ── formatting ────────────────────────────────────────────────────────────────

def _fp(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.2f}%"


def _fd(v: float | None) -> str:
    return "n/a" if v is None else f"${v:+.2f}"


def _fg(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.2f}"


# ── output ────────────────────────────────────────────────────────────────────

def _print_summary(
    analyzed: list[dict],
    skip_counts: dict[str, int],
    period_start: str,
    period_end: str,
) -> None:
    buys = [r for r in analyzed if r["side"] == "buy"]
    sells = [r for r in analyzed if r["side"] == "sell"]

    total_skipped = sum(skip_counts.values())
    skip_parts = "  ".join(
        f"{k}={v}" for k, v in sorted(skip_counts.items()) if v > 0
    )

    sb = _agg(buys)
    ss = _agg(sells)
    sa = _agg(analyzed)

    gaps = [r["timing_gap_days"] for r in analyzed if r["timing_gap_days"] is not None]
    gap_dist = {
        "1d":  sum(1 for t in gaps if t == 1),
        "2d":  sum(1 for t in gaps if t == 2),
        "3d":  sum(1 for t in gaps if t == 3),
        "4d+": sum(1 for t in gaps if t >= 4),
    }

    flagged = sum(1 for r in analyzed if r["fill_inside_bar"] is False)

    print()
    print("=== Slippage Analysis ===")
    print(f"Period:               {period_start} → {period_end}")
    print(f"Executions analyzed:  {len(analyzed)}")
    if total_skipped:
        print(f"Skipped:              {total_skipped} ({skip_parts})")
    else:
        print("Skipped:              0")
    print()

    print("Timing:")
    print(f"  Avg gap days:       {_fg(sa['mean_timing_gap'])}")
    print(
        f"  Distribution:       "
        f"1d={gap_dist['1d']}  2d={gap_dist['2d']}  "
        f"3d={gap_dist['3d']}  4d+={gap_dist['4d+']}"
    )
    print()

    col_w = 16
    print("Slippage (signed; positive = unfavorable):")
    print(
        f"{'':24s}"
        f"{'Buy':>{col_w}s}  {'Sell':>{col_w}s}  {'Overall':>{col_w}s}"
    )
    print(
        f"  Mean %:             "
        f"{_fp(sb['mean_signed_pct']):>{col_w}s}  "
        f"{_fp(ss['mean_signed_pct']):>{col_w}s}  "
        f"{_fp(sa['mean_signed_pct']):>{col_w}s}"
    )
    print(
        f"  Median %:           "
        f"{_fp(sb['median_signed_pct']):>{col_w}s}  "
        f"{_fp(ss['median_signed_pct']):>{col_w}s}  "
        f"{_fp(sa['median_signed_pct']):>{col_w}s}"
    )
    print(
        f"  90th %:             "
        f"{_fp(sb['p90_signed_pct']):>{col_w}s}  "
        f"{_fp(ss['p90_signed_pct']):>{col_w}s}  "
        f"{_fp(sa['p90_signed_pct']):>{col_w}s}"
    )
    print(
        f"  Dollar impact:      "
        f"{_fd(sb['dollar_impact']):>{col_w}s}  "
        f"{_fd(ss['dollar_impact']):>{col_w}s}  "
        f"{_fd(sa['dollar_impact']):>{col_w}s}"
    )
    print()

    worst10 = sorted(analyzed, key=lambda r: r["signed_slippage_pct"], reverse=True)[:10]
    if worst10:
        print("Worst 10 slippage events:")
        print(
            f"  {'SYM':<6s}  {'Side':<5s}  {'Signal':<12s}  {'Fill':<12s}  "
            f"{'SigClose':>10s}  {'FillPx':>10s}  {'Slip%':>8s}"
        )
        for r in worst10:
            print(
                f"  {r['symbol']:<6s}  {r['side']:<5s}  "
                f"{r['signal_date']:<12s}  {r['fill_date']:<12s}  "
                f"${r['signal_close']:>9.2f}  ${r['fill_price']:>9.2f}  "
                f"{_fp(r['signed_slippage_pct']):>8s}"
            )
        print()

    print(f"Fill-inside-bar issues:    {flagged} execution(s) flagged")


def _print_monthly(analyzed: list[dict]) -> None:
    from collections import defaultdict

    by_month: dict[str, list[dict]] = defaultdict(list)
    for r in analyzed:
        by_month[r["fill_date"][:7]].append(r)

    print()
    print("=== Monthly Breakdown ===")
    for month in sorted(by_month):
        s = _agg(by_month[month])
        print(
            f"{month}  n={s['count']:3d}  "
            f"mean={_fp(s['mean_signed_pct']):>8s}  "
            f"median={_fp(s['median_signed_pct']):>8s}  "
            f"p90={_fp(s['p90_signed_pct']):>8s}  "
            f"$impact={_fd(s['dollar_impact']):>10s}"
        )


# ── CSV report ────────────────────────────────────────────────────────────────

_REPORT_FIELDS = [
    "execution_id", "symbol", "side", "qty",
    "signal_date", "fill_date", "timing_gap_days",
    "signal_close", "fill_price", "slippage_abs", "slippage_pct", "signed_slippage_pct",
    "fill_date_open", "fill_date_high", "fill_date_low", "fill_inside_bar",
    "order_id", "intent_id", "signal_key", "run_id", "broker",
]


def _write_csv(analyzed: list[dict], end_date: str) -> str:
    Path("reports").mkdir(exist_ok=True)
    path = f"reports/slippage_{end_date}.csv"
    rows_sorted = sorted(analyzed, key=lambda r: r["signed_slippage_pct"], reverse=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_REPORT_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows_sorted:
            w.writerow({k: r.get(k) for k in _REPORT_FIELDS})
    return path


# ── public entry point ────────────────────────────────────────────────────────

def analyze_slippage(cfg: SlippageConfig) -> dict:
    """
    Run the slippage analysis and print a summary to stdout.

    Returns a dict with keys: analyzed (int), skipped (dict), rows (list), report_path (str|None).
    """
    from trading.db import connect

    end_date = cfg.end or _today()
    skip_counts: dict[str, int] = {}

    with connect() as conn:
        raw_rows = _fetch_raw(conn, cfg)

    analyzed = _classify(raw_rows, skip_counts)

    period_start = (
        min(r["fill_date"] for r in analyzed) if analyzed else (cfg.start or end_date)
    )
    if cfg.start:
        period_start = cfg.start

    _print_summary(analyzed, skip_counts, period_start, end_date)

    report_path: str | None = None
    if cfg.report:
        report_path = _write_csv(analyzed, end_date)
        print(f"\n  Report saved: {report_path}")

    if cfg.bucket_by == "month" and analyzed:
        _print_monthly(analyzed)

    return {
        "analyzed": len(analyzed),
        "skipped": skip_counts,
        "rows": analyzed,
        "report_path": report_path,
    }
