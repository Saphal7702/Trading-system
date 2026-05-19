from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..asof import resolve_asof_date
from ..db import connect

log = logging.getLogger("trading")


@dataclass
class BuilderConfig:
    universe_name: str = "custom"
    source: str = "existing"
    top: int = 300
    min_adv20: float = 1_000_000.0
    min_price: float = 10.0
    min_pct_above_sma200: float = 0.55
    max_gap_down_freq: float = 0.03
    min_rsi2_recovery_rate: float = 0.55
    lookback_days: int = 756
    exclude_symbols: list[str] = field(default_factory=list)
    save_report: bool = False
    use_backtest_db: bool = False
    # Audit mode — runs the same screening against an EXISTING universe and
    # writes failures to symbol_exclusions instead of building a new universe.
    audit_mode: bool = False
    audit_universe: str = ""
    remove_from_universe: bool = False
    dry_run: bool = False

    @classmethod
    def for_audit(cls, **kwargs) -> "BuilderConfig":
        """Defaults tuned for permanent exclusion decisions (audit mode).

        Differences vs. build defaults:
          - min_pct_above_sma200 = 0.0 (disabled): regime-sensitive, belongs
            in Layer 3 daily scoring, not in permanent exclusion decisions.
          - max_gap_down_freq = 0.05 (looser): only structural gap problems
            warrant a permanent exclusion.
          - min_rsi2_recovery_rate kept at 0.55 (3yr behavioral metric).
        """
        defaults = dict(
            min_pct_above_sma200=0.0,
            max_gap_down_freq=0.05,
            min_rsi2_recovery_rate=0.55,
            min_adv20=1_000_000.0,
            min_price=10.0,
        )
        defaults.update(kwargs)
        return cls(**defaults)


@dataclass
class SymbolMetrics:
    symbol: str
    bars_count: int = 0
    adv20: Optional[float] = None
    price: Optional[float] = None
    pct_above_sma200: Optional[float] = None
    gap_down_freq: Optional[float] = None
    atr_pct: Optional[float] = None
    rsi2_recovery_rate: Optional[float] = None
    rsi2_events: int = 0
    composite_score: Optional[float] = None
    included: bool = False
    reason: str = ""


def _rsi2_wilder(closes: list[float]) -> list[Optional[float]]:
    """Wilder's RSI period=2, aligned to closes (None until available)."""
    period = 2
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if n < period + 1:
        return out

    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        ch = closes[i] - closes[i - 1]
        gains[i] = ch if ch > 0 else 0.0
        losses[i] = (-ch) if ch < 0 else 0.0

    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period

    def _rsi_from(g: float, l: float) -> float:
        if l <= 0:
            return 100.0
        rs = g / l
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = _rsi_from(avg_gain, avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = _rsi_from(avg_gain, avg_loss)

    return out


def _sma(values: list[float], window: int) -> list[Optional[float]]:
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if window <= 0:
        return out
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= window:
            s -= values[i - window]
        if i >= window - 1:
            out[i] = s / window
    return out


def _atr14_last(
    highs: list[Optional[float]],
    lows: list[Optional[float]],
    closes: list[float],
) -> Optional[float]:
    """Wilder ATR(14), returns the most recent value or None if data is missing."""
    period = 14
    n = len(closes)
    if n < period + 1:
        return None
    if any(h is None for h in highs[-period - 1 :]):
        return None
    if any(l is None for l in lows[-period - 1 :]):
        return None

    trs: list[float] = [0.0]
    for i in range(1, n):
        h = highs[i]
        l = lows[i]
        pc = closes[i - 1]
        if h is None or l is None:
            trs.append(0.0)
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    avg = sum(trs[1 : period + 1]) / period
    for i in range(period + 1, n):
        avg = (avg * (period - 1) + trs[i]) / period
    return avg


REGIME_PROXIES = {"SPY", "QQQ", "IWM", "VTI"}

def _candidate_symbols(conn, source: str) -> list[str]:
    if source == "alpaca":
        rows = conn.execute(
            """
            SELECT symbol FROM assets_cache
            WHERE tradable=1 AND fractionable=1
              AND (symbol IN ('SPY', 'QQQ', 'IWM', 'VTI')
                   OR exchange NOT IN ('ARCA', 'BATS'))
            ORDER BY symbol;
            """
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM universe_membership ORDER BY symbol;"
        ).fetchall()
    return [r["symbol"] for r in rows]

def _exclude_set(cli_excludes: list[str]) -> set[str]:
    env_str = os.getenv("TRADING_EXCLUDE_SYMBOLS", "")
    parts: list[str] = []
    if env_str:
        parts.extend(env_str.split(","))
    parts.extend(cli_excludes)
    return {p.strip().upper() for p in parts if p and p.strip()}


def _compute_metrics(conn, symbol: str, asof: str, lookback_days: int) -> SymbolMetrics:
    FETCH_LIMIT = 1050
    rows = conn.execute(
        """SELECT t, o, h, l, c, v FROM bars_daily
        WHERE symbol=? AND t<=? AND c IS NOT NULL
        ORDER BY t DESC LIMIT ?""",
        (symbol, asof, FETCH_LIMIT),
    ).fetchall()
    rows.reverse()

    m = SymbolMetrics(symbol=symbol, bars_count=len(rows))
    n = len(rows)
    if n == 0:
        m.reason = "no_bars"
        return m
    if n < 252:
        m.reason = f"insufficient_history({n})"
        return m

    closes = [float(r["c"]) for r in rows]
    opens = [float(r["o"]) if r["o"] is not None else None for r in rows]
    highs = [float(r["h"]) if r["h"] is not None else None for r in rows]
    lows = [float(r["l"]) if r["l"] is not None else None for r in rows]
    volumes = [float(r["v"]) if r["v"] is not None else 0.0 for r in rows]

    m.price = closes[-1]
    last20_dollar = [closes[i] * volumes[i] for i in range(n - 20, n)]
    m.adv20 = sum(last20_dollar) / 20.0

    sma200 = _sma(closes, 200)
    above = 0
    total = 0
    for i in range(max(0, n - 252), n):
        if sma200[i] is None:
            continue
        total += 1
        if closes[i] > sma200[i]:
            above += 1
    if total == 0:
        m.reason = "insufficient_sma200"
        return m
    m.pct_above_sma200 = above / total

    gap_total = 0
    gap_down = 0
    for i in range(max(1, n - 252), n):
        prev_close = closes[i - 1]
        op = opens[i]
        if op is None or prev_close <= 0:
            continue
        gap_total += 1
        if op / prev_close - 1.0 < -0.05:
            gap_down += 1
    m.gap_down_freq = (gap_down / gap_total) if gap_total > 0 else 0.0

    atr = _atr14_last(highs, lows, closes)
    if atr is not None and m.price and m.price > 0:
        m.atr_pct = atr / m.price

    rsi2 = _rsi2_wilder(closes)
    start = max(2, n - lookback_days)
    events = 0
    recovered = 0
    for i in range(start, n):
        cur = rsi2[i]
        prev = rsi2[i - 1]
        if cur is None or prev is None:
            continue
        if cur <= 10.0 and prev > 10.0:
            events += 1
            for j in range(i + 1, min(i + 16, n)):
                v = rsi2[j]
                if v is not None and v >= 50.0:
                    recovered += 1
                    break
    m.rsi2_events = events
    m.rsi2_recovery_rate = (recovered / events) if events > 0 else None
    return m


def _evaluate(m: SymbolMetrics, cfg: BuilderConfig) -> str:
    if m.reason and m.reason not in ("pass",):
        return m.reason
    if m.price is None or m.price < cfg.min_price:
        return f"price<{cfg.min_price}"
    if m.adv20 is None or m.adv20 < cfg.min_adv20:
        return f"adv20<{cfg.min_adv20:.0f}"
    if m.pct_above_sma200 is None or m.pct_above_sma200 < cfg.min_pct_above_sma200:
        return f"pct_above_sma200<{cfg.min_pct_above_sma200}"
    if m.gap_down_freq is None or m.gap_down_freq > cfg.max_gap_down_freq:
        return f"gap_down_freq>{cfg.max_gap_down_freq}"
    if m.rsi2_recovery_rate is None:
        return "rsi2_no_events"
    if m.rsi2_recovery_rate < cfg.min_rsi2_recovery_rate:
        return f"rsi2_recovery<{cfg.min_rsi2_recovery_rate}"
    return "pass"


def _atr_score(atr_pct: Optional[float]) -> float:
    if atr_pct is None:
        return 0.0
    if 0.01 <= atr_pct <= 0.03:
        return 1.0
    if 0.005 <= atr_pct < 0.01:
        return 0.5
    if 0.03 < atr_pct <= 0.04:
        return 0.5
    return 0.0


def _composite_score(m: SymbolMetrics, max_gap_freq: float) -> float:
    rsi2_rate = m.rsi2_recovery_rate or 0.0
    gap_freq = m.gap_down_freq if m.gap_down_freq is not None else max_gap_freq
    gap_safety = max(0.0, 1.0 - gap_freq / max(max_gap_freq, 1e-9))
    pct_trend = m.pct_above_sma200 or 0.0
    atr = _atr_score(m.atr_pct)
    return 0.40 * rsi2_rate + 0.25 * gap_safety + 0.20 * pct_trend + 0.15 * atr


def _write_universe(conn, universe_name: str, symbols: list[str]) -> None:
    REGIME_PROXIES = ["SPY", "QQQ", "IWM", "VTI"]
    final = list(dict.fromkeys(symbols + REGIME_PROXIES))  # dedup, proxies always included
    conn.execute("DELETE FROM universe_membership WHERE universe=?;", (universe_name,))
    for s in final:
        conn.execute(
            "INSERT OR IGNORE INTO universe_membership(universe, symbol) VALUES(?, ?);",
            (universe_name, s),
        )


def _write_csv_report(path: Path, metrics: list[SymbolMetrics]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "symbol", "adv20", "price", "pct_above_sma200", "gap_down_freq",
            "atr_pct", "rsi2_recovery_rate", "rsi2_events",
            "composite_score", "included", "reason",
        ])
        for m in metrics:
            w.writerow([
                m.symbol,
                f"{m.adv20:.2f}" if m.adv20 is not None else "",
                f"{m.price:.4f}" if m.price is not None else "",
                f"{m.pct_above_sma200:.4f}" if m.pct_above_sma200 is not None else "",
                f"{m.gap_down_freq:.4f}" if m.gap_down_freq is not None else "",
                f"{m.atr_pct:.4f}" if m.atr_pct is not None else "",
                f"{m.rsi2_recovery_rate:.4f}" if m.rsi2_recovery_rate is not None else "",
                m.rsi2_events,
                f"{m.composite_score:.4f}" if m.composite_score is not None else "",
                int(m.included),
                m.reason,
            ])


def _classify_failure(
    reason: str, m: SymbolMetrics, cfg: BuilderConfig
) -> tuple[Optional[str], str]:
    """
    Map an `_evaluate` reason string to (reason_code, reason_note) for
    symbol_exclusions. Returns (None, decision_label) when the symbol should
    be kept (e.g. rsi2_no_events).
    """
    # Layer 1 — structural data/eligibility
    if reason == "no_bars":
        return "data_anomaly", "L1:no_bars"
    if reason.startswith("insufficient_history"):
        return "data_anomaly", f"L1:{reason}"
    if reason == "insufficient_sma200":
        return "data_anomaly", "L1:insufficient_sma200"
    if reason.startswith("price<"):
        actual = f"${m.price:.2f}" if m.price is not None else "n/a"
        return (
            "data_anomaly",
            f"L1:price<${cfg.min_price:.0f} (actual={actual})",
        )
    if reason.startswith("adv20<"):
        actual = f"${m.adv20:.0f}" if m.adv20 is not None else "n/a"
        return (
            "data_anomaly",
            f"L1:adv20<${cfg.min_adv20:.0f} (actual={actual})",
        )

    # Layer 2a — strategy fit / behavioral
    if reason.startswith("pct_above_sma200<"):
        actual = (
            f"{m.pct_above_sma200:.3f}" if m.pct_above_sma200 is not None else "n/a"
        )
        return (
            "chronic_loser",
            f"L2a:pct_above_sma200={actual}<{cfg.min_pct_above_sma200}",
        )
    if reason.startswith("gap_down_freq>"):
        actual = (
            f"{m.gap_down_freq:.3f}" if m.gap_down_freq is not None else "n/a"
        )
        return (
            "chronic_loser",
            f"L2a:gap_down_freq={actual}>{cfg.max_gap_down_freq}",
        )
    if reason == "rsi2_no_events":
        # Deliberate keep — no evidence in lookback window.
        return None, "rsi2_no_events_kept"
    if reason.startswith("rsi2_recovery<"):
        actual = (
            f"{m.rsi2_recovery_rate:.2f}"
            if m.rsi2_recovery_rate is not None
            else "n/a"
        )
        return (
            "chronic_loser",
            f"L2a:rsi2_recovery={actual}<{cfg.min_rsi2_recovery_rate} "
            f"(events={m.rsi2_events})",
        )

    return None, f"unrecognized:{reason}"


def audit_existing_universe(cfg: BuilderConfig) -> dict:
    """Run the existing builder screening against an EXISTING universe.
    Failures are written to symbol_exclusions; passing symbols are kept
    in universe_membership. No new universe is created."""
    if not cfg.audit_universe:
        raise ValueError("audit_universe must be set when audit_mode=True")

    with connect() as conn:
        rows = conn.execute(
            "SELECT symbol FROM universe_membership WHERE universe=? ORDER BY symbol",
            (cfg.audit_universe,),
        ).fetchall()
    universe_symbols = [r["symbol"] for r in rows]
    if not universe_symbols:
        raise RuntimeError(
            f"universe_membership[{cfg.audit_universe!r}] is empty — nothing to audit"
        )

    with connect() as conn:
        excl_rows = conn.execute(
            "SELECT symbol, reason_code, reinstated_at FROM symbol_exclusions"
        ).fetchall()
    currently_excluded: dict[str, str] = {
        r["symbol"]: r["reason_code"] for r in excl_rows if r["reinstated_at"] is None
    }
    previously_reinstated: set[str] = {
        r["symbol"] for r in excl_rows if r["reinstated_at"] is not None
    }

    if cfg.use_backtest_db:
        from ..backtest.db import connect_backtest
        bt_path = os.getenv("BACKTEST_DB_PATH", None)
        bars_conn = connect_backtest(bt_path)
    else:
        bars_conn = connect()

    decisions: list[dict] = []
    exclusion_queue: list[tuple[str, str, str]] = []
    reinstated_warnings: list[tuple[str, str]] = []
    all_metrics: list[SymbolMetrics] = []

    try:
        asof = resolve_asof_date(bars_conn, None)

        for i, sym in enumerate(universe_symbols):
            if sym in currently_excluded:
                m = SymbolMetrics(symbol=sym)
                m.reason = f"already_excluded ({currently_excluded[sym]})"
                all_metrics.append(m)
                decisions.append({
                    "symbol": sym,
                    "decision": "already_excluded",
                    "reason_code": currently_excluded[sym],
                    "reason_note": "",
                })
                continue

            m = _compute_metrics(bars_conn, sym, asof, cfg.lookback_days)
            reason = _evaluate(m, cfg)
            m.reason = reason
            if reason == "pass":
                m.composite_score = _composite_score(m, cfg.max_gap_down_freq)
                m.included = True
            all_metrics.append(m)

            if sym in previously_reinstated:
                if reason != "pass":
                    code, note = _classify_failure(reason, m, cfg)
                    if code is not None:
                        log.warning(
                            "Reinstated symbol %s still failing: %s", sym, note
                        )
                        reinstated_warnings.append((sym, note))
                        m.included = True  # reinstated — operator override stands
                        decisions.append({
                            "symbol": sym,
                            "decision": "previously_reinstated_kept",
                            "reason_code": "",
                            "reason_note": f"WARNING: {note}",
                        })
                        continue
                m.included = True
                decisions.append({
                    "symbol": sym,
                    "decision": "previously_reinstated_kept",
                    "reason_code": "",
                    "reason_note": "",
                })
                continue

            if reason == "pass":
                decisions.append({
                    "symbol": sym,
                    "decision": "pass",
                    "reason_code": "",
                    "reason_note": "",
                })
                continue

            code, note = _classify_failure(reason, m, cfg)
            if code is None:
                # rsi2_no_events_kept (or unrecognized) — keep, do not exclude
                m.included = True
                decisions.append({
                    "symbol": sym,
                    "decision": note,
                    "reason_code": "",
                    "reason_note": "",
                })
                continue

            decisions.append({
                "symbol": sym,
                "decision": "fail",
                "reason_code": code,
                "reason_note": note,
            })
            exclusion_queue.append((sym, code, note))

            if (i + 1) % 250 == 0:
                log.info("Audited %s/%s symbols", i + 1, len(universe_symbols))
    finally:
        bars_conn.close()

    layer_1 = [(s, n) for s, c, n in exclusion_queue if c == "data_anomaly"]
    layer_2a = [(s, n) for s, c, n in exclusion_queue if c == "chronic_loser"]

    summary: dict = {
        "audit_universe": cfg.audit_universe,
        "asof": asof,
        "total_evaluated": len(universe_symbols),
        "already_excluded_count": sum(
            1 for d in decisions if d["decision"] == "already_excluded"
        ),
        "previously_reinstated_count": sum(
            1 for d in decisions if d["decision"] == "previously_reinstated_kept"
        ),
        "reinstated_warnings": reinstated_warnings,
        "layer_1_failures": {
            "count": len(layer_1),
            "symbols": [(s, n) for s, n in layer_1],
        },
        "layer_2a_failures": {
            "count": len(layer_2a),
            "symbols": [(s, n) for s, n in layer_2a],
        },
        "passing_count": sum(1 for d in decisions if d["decision"] == "pass"),
        "rsi2_no_events_kept_count": sum(
            1 for d in decisions if d["decision"] == "rsi2_no_events_kept"
        ),
        "dry_run": cfg.dry_run,
        "removed_from_universe": 0,
        "report_path": None,
        "exclusions": [
            {"symbol": s, "reason_code": c, "reason_note": n}
            for s, c, n in exclusion_queue
        ],
        "decisions": decisions,
    }

    if cfg.dry_run:
        return summary

    with connect() as conn:
        for sym, code, note in exclusion_queue:
            conn.execute(
                """INSERT INTO symbol_exclusions
                   (symbol, reason_code, reason_note, excluded_by, excluded_at)
                   VALUES (?, ?, ?, 'system', datetime('now'))
                   ON CONFLICT(symbol) DO NOTHING""",
                (sym, code, note),
            )
        if cfg.remove_from_universe and exclusion_queue:
            syms_to_remove = [s for s, _, _ in exclusion_queue]
            placeholders = ",".join("?" * len(syms_to_remove))
            conn.execute(
                f"DELETE FROM universe_membership "
                f"WHERE universe=? AND symbol IN ({placeholders})",
                [cfg.audit_universe] + syms_to_remove,
            )
            summary["removed_from_universe"] = len(syms_to_remove)

    if cfg.save_report:
        all_metrics.sort(
            key=lambda x: (
                0 if x.included else 1,
                -(x.composite_score or 0.0),
                x.symbol,
            )
        )
        report_path = (
            Path("reports")
            / f"universe_audit_{cfg.audit_universe}_{datetime.now().strftime('%Y-%m-%d')}.csv"
        )
        _write_csv_report(report_path, all_metrics)
        summary["report_path"] = str(report_path)

    return summary


def build_custom_universe(cfg: BuilderConfig) -> dict:
    """
    Run screening, write the resulting symbols to universe_membership under
    cfg.universe_name (replacing prior rows), and optionally save a CSV report.
    Returns a summary dict for the caller to print.
    """
    excludes = _exclude_set(cfg.exclude_symbols)

    summary: dict = {
        "asof": "",
        "candidates": 0,
        "excluded": 0,
        "passed_liquidity": 0,
        "passed_trend": 0,
        "passed_gap": 0,
        "passed_rsi2": 0,
        "final": 0,
        "top_n": [],
        "report_path": None,
        "universe_name": cfg.universe_name,
    }

    with connect() as live_conn:
        all_candidates = _candidate_symbols(live_conn, cfg.source)
    candidates = [s for s in all_candidates if s not in excludes]
    summary["candidates"] = len(candidates)
    summary["excluded"] = len(all_candidates) - len(candidates)

    if cfg.use_backtest_db:
        from ..backtest.db import connect_backtest
        bt_path = os.getenv("BACKTEST_DB_PATH", None)
        bars_conn = connect_backtest(bt_path)
    else:
        bars_conn = connect()

    try:
        asof = resolve_asof_date(bars_conn, None)
        summary["asof"] = asof

        all_metrics: list[SymbolMetrics] = []
        for i, sym in enumerate(candidates):
            m = _compute_metrics(bars_conn, sym, asof, cfg.lookback_days)
            all_metrics.append(m)
            if (i + 1) % 250 == 0:
                log.info("Screened %s/%s symbols", i + 1, len(candidates))
    finally:
        bars_conn.close()

    for m in all_metrics:
        liquid = (
            m.adv20 is not None and m.price is not None
            and m.adv20 >= cfg.min_adv20 and m.price >= cfg.min_price
        )
        trend = liquid and (
            m.pct_above_sma200 is not None
            and m.pct_above_sma200 >= cfg.min_pct_above_sma200
        )
        gap_ok = trend and (
            m.gap_down_freq is not None
            and m.gap_down_freq <= cfg.max_gap_down_freq
        )
        rsi_ok = gap_ok and (
            m.rsi2_recovery_rate is not None
            and m.rsi2_recovery_rate >= cfg.min_rsi2_recovery_rate
        )
        if liquid:
            summary["passed_liquidity"] += 1
        if trend:
            summary["passed_trend"] += 1
        if gap_ok:
            summary["passed_gap"] += 1
        if rsi_ok:
            summary["passed_rsi2"] += 1

        m.reason = _evaluate(m, cfg)
        if m.reason == "pass":
            m.composite_score = _composite_score(m, cfg.max_gap_down_freq)

    qualified = [m for m in all_metrics if m.reason == "pass"]
    qualified.sort(key=lambda x: (x.composite_score or 0.0), reverse=True)

    selected = qualified[: cfg.top] if cfg.top and cfg.top > 0 else qualified
    selected_set = {m.symbol for m in selected}

    for m in qualified:
        if m.symbol in selected_set:
            m.included = True
        else:
            m.reason = "below_top_n"

    summary["final"] = len(selected)
    summary["top_n"] = [
        {
            "symbol": m.symbol,
            "rsi2_rate": m.rsi2_recovery_rate,
            "pct_above_sma200": m.pct_above_sma200,
            "atr_pct": m.atr_pct,
            "score": m.composite_score,
        }
        for m in selected[:20]
    ]

    selected_symbols = [m.symbol for m in selected]
    with connect() as conn:
        _write_universe(conn, cfg.universe_name, selected_symbols)

    if cfg.save_report:
        all_metrics.sort(
            key=lambda x: (
                0 if x.included else 1,
                -(x.composite_score or 0.0),
                x.symbol,
            )
        )
        report_path = Path("reports") / f"universe_build_{datetime.now().strftime('%Y-%m-%d')}.csv"
        _write_csv_report(report_path, all_metrics)
        summary["report_path"] = str(report_path)

    return summary
