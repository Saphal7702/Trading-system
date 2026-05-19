from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger("trading")


@dataclass
class TrimConfig:
    backtest_run_id: int
    from_universe: str = "sp500"
    # Chronic loser threshold: need this many entries before exclusion applies
    min_entries_for_exclusion: int = 8
    # Exclude if WR% < this AND net_pnl < 0 (requires min_entries_for_exclusion+)
    max_wr_chronic: float = 35.0
    # Exclude if net_pnl below this with min_entries_consistent+ entries
    min_net_pnl_consistent: float = -20.0
    min_entries_consistent: int = 8
    # Temporal safeguards
    min_years_traded_for_chronic: int = 3
    max_concentration_pct: float = 70.0
    # Whether to also exclude symbols with zero trades
    exclude_no_trades: bool = False
    # Also DELETE excluded symbols from universe_membership[from_universe]
    remove_from_universe: bool = False
    # If True, print decisions but don't write to either table
    dry_run: bool = False
    # Save per-symbol decision CSV to reports/
    save_report: bool = False


@dataclass
class SymbolDecision:
    symbol: str
    entries: int = 0
    wins: int = 0
    win_rate: float = 0.0
    net_pnl: float = 0.0
    avg_ret: float = 0.0
    worst_ret: float = 0.0
    years_traded: int = 0
    years_negative: int = 0
    largest_year_loss: float = 0.0
    largest_year_label: str = ""
    largest_year_trade_count: int = 0
    concentration_pct: float = 0.0
    should_exclude: bool = False
    reason: str = "kept"
    reason_code: str = ""
    reason_note: str = ""


def _build_reason_note(kind: str, d: SymbolDecision, run_id: int, period: str) -> str:
    yrs_label = f"{d.years_negative}/{d.years_traded}" if d.years_traded else "0/0"
    if kind == "chronic":
        return (
            f"L2b:chronic wr={d.win_rate:.0f}% pnl=${d.net_pnl:+.2f} "
            f"n={d.entries} yrs_neg={yrs_label} conc={d.concentration_pct:.0f}% "
            f"(run_id={run_id} period={period})"
        )
    if kind == "consistent":
        return (
            f"L2b:consistent pnl=${d.net_pnl:+.2f} n={d.entries} "
            f"yrs_neg={yrs_label} conc={d.concentration_pct:.0f}% "
            f"(run_id={run_id} period={period})"
        )
    if kind == "no_trades":
        return (
            f"L2b:no_trades — symbol never traded in backtest "
            f"(run_id={run_id} period={period})"
        )
    return kind


def trim_universe(cfg: TrimConfig) -> dict:
    """
    Read per-symbol performance from a backtest run, classify chronic losers
    via Layer 2b criteria (with temporal safeguards), and write them to
    symbol_exclusions. universe_membership stays intact unless
    cfg.remove_from_universe is True.
    """
    from ..db import connect
    from ..backtest.db import connect_backtest

    backtest_db_path = os.getenv("BACKTEST_DB_PATH", None)

    # Step 1: per-symbol aggregates from backtest DB
    with connect_backtest(backtest_db_path) as bt_conn:
        run_row = bt_conn.execute(
            "SELECT id, start_date, end_date, universe FROM backtest_runs WHERE id=?",
            (cfg.backtest_run_id,),
        ).fetchone()
        if not run_row:
            raise RuntimeError(
                f"Backtest run_id={cfg.backtest_run_id} not found. "
                "Check the run ID with: sqlite3 backtest.sqlite "
                "'SELECT id, start_date, end_date, universe FROM backtest_runs;'"
            )

        agg_rows = bt_conn.execute(
            """
            SELECT
                symbol,
                COUNT(*)                                            AS entries,
                SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END)   AS wins,
                ROUND(SUM(realized_pnl), 4)                         AS net_pnl,
                ROUND(AVG(return_pct), 4)                           AS avg_ret,
                ROUND(MIN(return_pct), 4)                           AS worst_ret
            FROM backtest_trades
            WHERE run_id = ? AND exit_date IS NOT NULL
            GROUP BY symbol
            """,
            (cfg.backtest_run_id,),
        ).fetchall()

        year_rows = bt_conn.execute(
            """
            SELECT
                symbol,
                strftime('%Y', exit_date) AS yr,
                SUM(realized_pnl)         AS yr_pnl,
                COUNT(*)                  AS yr_trades
            FROM backtest_trades
            WHERE run_id = ? AND exit_date IS NOT NULL
            GROUP BY symbol, yr
            """,
            (cfg.backtest_run_id,),
        ).fetchall()

    period = (
        f"{(run_row['start_date'] or '')[:4]}→{(run_row['end_date'] or '')[:4]}"
    )

    # Bucket yearly stats per symbol
    yearly: dict[str, list[tuple[str, float, int]]] = {}
    for r in year_rows:
        yearly.setdefault(r["symbol"], []).append(
            (r["yr"] or "", float(r["yr_pnl"] or 0.0), int(r["yr_trades"] or 0))
        )

    sym_stats: dict[str, SymbolDecision] = {}
    for r in agg_rows:
        entries = r["entries"] or 0
        wins = r["wins"] or 0
        wr = round(100.0 * wins / entries, 1) if entries > 0 else 0.0
        net_pnl = float(r["net_pnl"] or 0.0)

        years = yearly.get(r["symbol"], [])
        years_traded = len(years)
        negative_years = [(y, p, n) for y, p, n in years if p < 0]
        years_negative = len(negative_years)
        # Share of the total loss dollars that came from the single worst year.
        # Using |net_pnl| as the denominator (old formula) lets profitable
        # offsetting years inflate the ratio past 100%, which made the
        # single_year_blowout carve-out fire on chronic names.
        total_loss_dollars = sum(abs(p) for _, p, _ in negative_years)
        if negative_years:
            worst = max(negative_years, key=lambda x: abs(x[1]))
            largest_year_loss = abs(worst[1])
            largest_year_label = worst[0]
            largest_year_trade_count = worst[2]
        else:
            largest_year_loss = 0.0
            largest_year_label = ""
            largest_year_trade_count = 0
        concentration_pct = (
            100.0 * largest_year_loss / total_loss_dollars
            if total_loss_dollars > 0
            else 0.0
        )

        sym_stats[r["symbol"]] = SymbolDecision(
            symbol=r["symbol"],
            entries=entries,
            wins=wins,
            win_rate=wr,
            net_pnl=net_pnl,
            avg_ret=float(r["avg_ret"] or 0.0),
            worst_ret=float(r["worst_ret"] or 0.0),
            years_traded=years_traded,
            years_negative=years_negative,
            largest_year_loss=largest_year_loss,
            largest_year_label=largest_year_label,
            largest_year_trade_count=largest_year_trade_count,
            concentration_pct=concentration_pct,
        )

    # Step 2: current exclusions / reinstated set
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

    # Step 3: universe membership
    with connect() as conn:
        universe_rows = conn.execute(
            "SELECT DISTINCT symbol FROM universe_membership WHERE universe=?",
            (cfg.from_universe,),
        ).fetchall()
    all_symbols = [r["symbol"] for r in universe_rows]
    if not all_symbols:
        raise RuntimeError(
            f"No symbols found in universe '{cfg.from_universe}'. "
            "Run build-custom-universe or update-universe-sp500 first."
        )

    # Step 4: per-symbol decisions
    decisions: list[SymbolDecision] = []
    exclusion_queue: list[SymbolDecision] = []
    reinstated_warnings: list[tuple[str, str]] = []
    blowouts: list[SymbolDecision] = []

    def _classify(d: SymbolDecision) -> tuple[str, str]:
        """Return (kind, decision_reason). kind in {'chronic', 'consistent',
        'single_year_blowout', 'kept'}; decision_reason is the short label."""
        if d.entries == 0:
            return ("no_trades", "no_trades")

        # Single-year blowout carve-out: net negative, enough trades, most of
        # the loss dollars came from one year, AND the symbol has at most two
        # losing years overall. 3+ losing years means the damage isn't really
        # concentrated in a single event regardless of dollar share.
        if (
            d.entries >= cfg.min_entries_for_exclusion
            and d.net_pnl < 0
            and d.years_negative <= 2
            and d.concentration_pct >= cfg.max_concentration_pct
        ):
            return ("single_year_blowout", "single_year_blowout")

        years_neg_ratio = (
            d.years_negative / d.years_traded if d.years_traded else 0.0
        )
        chronic_gate = (
            d.entries >= cfg.min_entries_for_exclusion
            and d.win_rate < cfg.max_wr_chronic
            and d.net_pnl < 0
            and d.years_traded >= cfg.min_years_traded_for_chronic
            and years_neg_ratio > 0.5
        )
        if chronic_gate:
            return ("chronic", "chronic_loser")

        consistent_gate = (
            d.entries >= cfg.min_entries_consistent
            and d.net_pnl < cfg.min_net_pnl_consistent
            and d.years_traded >= cfg.min_years_traded_for_chronic
            and years_neg_ratio > 0.5
        )
        if consistent_gate:
            return ("consistent", "consistent_loser")

        return ("kept", "kept")

    for sym in all_symbols:
        # Already excluded → skip checks, no rewrite
        if sym in currently_excluded:
            d = sym_stats.get(sym) or SymbolDecision(symbol=sym)
            d.symbol = sym
            d.should_exclude = False
            d.reason = "already_excluded"
            d.reason_code = currently_excluded[sym]
            decisions.append(d)
            continue

        d = sym_stats.get(sym) or SymbolDecision(symbol=sym)
        kind, label = _classify(d)

        # Previously reinstated → skip checks, warn if classification is bad
        if sym in previously_reinstated:
            d.should_exclude = False
            d.reason = "previously_reinstated_kept"
            if kind in ("chronic", "consistent"):
                note = _build_reason_note(kind, d, cfg.backtest_run_id, period)
                log.warning(
                    "Reinstated symbol %s would now classify %s: %s",
                    sym, kind, note,
                )
                reinstated_warnings.append((sym, f"{label} ({note})"))
                d.reason_note = f"WARNING: {note}"
            decisions.append(d)
            continue

        if kind == "no_trades":
            if cfg.exclude_no_trades:
                d.should_exclude = True
                d.reason = "no_trades"
                d.reason_code = "chronic_loser"
                d.reason_note = _build_reason_note(
                    "no_trades", d, cfg.backtest_run_id, period,
                )
                exclusion_queue.append(d)
            else:
                d.should_exclude = False
                d.reason = "no_trades_kept"
        elif kind in ("chronic", "consistent"):
            d.should_exclude = True
            d.reason = label
            d.reason_code = "chronic_loser"
            d.reason_note = _build_reason_note(kind, d, cfg.backtest_run_id, period)
            exclusion_queue.append(d)
        elif kind == "single_year_blowout":
            d.should_exclude = False
            d.reason = "single_year_blowout"
            log.info(
                "Single-year blowout (kept): %s conc=%.0f%% worst_year=%s pnl=$%+.2f",
                sym, d.concentration_pct, d.largest_year_label,
                -d.largest_year_loss,
            )
            blowouts.append(d)
        else:
            d.should_exclude = False
            d.reason = "kept"
        decisions.append(d)

    # Step 5: write or dry-run
    chronic_count = sum(1 for d in exclusion_queue if d.reason == "chronic_loser")
    consistent_count = sum(1 for d in exclusion_queue if d.reason == "consistent_loser")
    no_trade_excluded = sum(1 for d in exclusion_queue if d.reason == "no_trades")

    summary: dict = {
        "from_universe": cfg.from_universe,
        "backtest_run_id": cfg.backtest_run_id,
        "run_period": f"{run_row['start_date']} → {run_row['end_date']}",
        "run_universe": run_row["universe"],
        "total_in": len(all_symbols),
        "already_excluded_count": sum(
            1 for d in decisions if d.reason == "already_excluded"
        ),
        "previously_reinstated_count": sum(
            1 for d in decisions if d.reason == "previously_reinstated_kept"
        ),
        "reinstated_warnings": reinstated_warnings,
        "excluded": len(exclusion_queue),
        "chronic_losers": chronic_count,
        "consistent_losers": consistent_count,
        "no_trades_excluded": no_trade_excluded,
        "single_year_blowouts": [
            {
                "symbol": d.symbol,
                "concentration_pct": d.concentration_pct,
                "year": d.largest_year_label,
                "year_pnl": -d.largest_year_loss,
                "year_trades": d.largest_year_trade_count,
            }
            for d in blowouts
        ],
        "no_trades_kept": sum(1 for d in decisions if d.reason == "no_trades_kept"),
        "passing": sum(1 for d in decisions if d.reason == "kept"),
        "exclusions": [
            {"symbol": d.symbol, "reason_code": d.reason_code,
             "reason_note": d.reason_note, "reason": d.reason}
            for d in exclusion_queue
        ],
        "dry_run": cfg.dry_run,
        "removed_from_universe": 0,
        "report_path": None,
        "decisions": decisions,
    }

    if not cfg.dry_run:
        with connect() as conn:
            for d in exclusion_queue:
                conn.execute(
                    """INSERT INTO symbol_exclusions
                       (symbol, reason_code, reason_note, excluded_by, excluded_at)
                       VALUES (?, ?, ?, 'system', datetime('now'))
                       ON CONFLICT(symbol) DO NOTHING""",
                    (d.symbol, d.reason_code, d.reason_note),
                )
            if cfg.remove_from_universe and exclusion_queue:
                syms = [d.symbol for d in exclusion_queue]
                placeholders = ",".join("?" * len(syms))
                conn.execute(
                    f"DELETE FROM universe_membership "
                    f"WHERE universe=? AND symbol IN ({placeholders})",
                    [cfg.from_universe] + syms,
                )
                summary["removed_from_universe"] = len(syms)

    # Step 6: CSV report
    if cfg.save_report:
        report_path = (
            Path("reports")
            / f"trim_{cfg.from_universe}_{datetime.now().strftime('%Y-%m-%d')}.csv"
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)

        def _sort_key(d: SymbolDecision) -> tuple:
            # excluded first (worst pnl first), then blowouts, then kept
            if d.should_exclude:
                bucket = 0
            elif d.reason == "single_year_blowout":
                bucket = 1
            else:
                bucket = 2
            return (bucket, d.net_pnl)

        with report_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "symbol", "entries", "wins", "win_rate", "net_pnl",
                "avg_ret", "worst_ret", "years_traded", "years_negative",
                "concentration_pct", "should_exclude", "reason",
            ])
            for d in sorted(decisions, key=_sort_key):
                w.writerow([
                    d.symbol, d.entries, d.wins,
                    f"{d.win_rate:.1f}",
                    f"{d.net_pnl:.2f}",
                    f"{d.avg_ret:.2f}",
                    f"{d.worst_ret:.2f}",
                    d.years_traded, d.years_negative,
                    f"{d.concentration_pct:.1f}",
                    int(d.should_exclude),
                    d.reason,
                ])
        summary["report_path"] = str(report_path)

    return summary
