from __future__ import annotations

import csv
import logging
import urllib.request

log = logging.getLogger("trading")

SP500_CSV_URL = (
    "https://raw.githubusercontent.com/datasets/"
    "s-and-p-500-companies/main/data/constituents.csv"
)

WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def _fetch_sp500_from_github() -> list[str]:
    """Fetch current S&P 500 symbols from GitHub datasets CSV."""
    req = urllib.request.Request(
        SP500_CSV_URL,
        headers={"User-Agent": "trading-system/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        content = resp.read().decode("utf-8").splitlines()
    reader = csv.DictReader(content)
    symbols = []
    for row in reader:
        sym = row.get("Symbol", "").strip().upper()
        if sym:
            symbols.append(sym)
    return sorted(set(symbols))


def _fetch_sp500_from_file(csv_path: str) -> list[str]:
    """Load S&P 500 symbols from a local CSV file."""
    symbols = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        sym_col = None
        for candidate in ["Symbol", "symbol", "Ticker", "ticker", "SYMBOL"]:
            if candidate in fieldnames:
                sym_col = candidate
                break
        for row in reader:
            if sym_col:
                sym = row.get(sym_col, "").strip().upper()
            else:
                sym = list(row.values())[0].strip().upper()
            if sym:
                symbols.append(sym)
    return sorted(set(symbols))


def update_sp500_universe(
    universe: str = "sp500",
    source: str = "github",
    csv_path: str = "",
    dry_run: bool = False,
) -> dict:
    """
    Safe, non-destructive quarterly S&P 500 update.

    Source-aware diff against universe_membership:
      sp500       baseline rows from the original load-universe CSV import
      sp500_new   added by a prior quarterly update
      sp500_rem   previously dropped from the index; kept for review
      manual      operator-curated additions; NEVER touched by this update
    """
    from ..db import connect

    if source == "file" and csv_path:
        sp500_current = set(_fetch_sp500_from_file(csv_path))
        log.info("Loaded %d symbols from %s", len(sp500_current), csv_path)
    else:
        sp500_current = set(_fetch_sp500_from_github())
        log.info("Fetched %d S&P 500 symbols from GitHub", len(sp500_current))

    with connect() as conn:
        rows = conn.execute(
            "SELECT symbol, source FROM universe_membership WHERE universe=?",
            (universe,),
        ).fetchall()

    # Bucket existing rows by source. Note: 'sp500_rem' is NOT in `active`.
    # That's the key invariant that makes the diff clean and re-runs no-op.
    active       = {r["symbol"] for r in rows if r["source"] in ("sp500", "sp500_new")}
    removed_prev = {r["symbol"] for r in rows if r["source"] == "sp500_rem"}
    manual       = {r["symbol"] for r in rows if r["source"] == "manual"}

    log.info(
        "Existing %s universe: active=%d sp500_rem=%d manual=%d total=%d",
        universe, len(active), len(removed_prev), len(manual), len(rows),
    )

    # Classification — by construction `added` and `rejoined` are disjoint.
    added    = sorted(sp500_current - active - manual - removed_prev)
    rejoined = sorted(sp500_current & removed_prev)
    potentially_removed = sorted(active - sp500_current)

    if not dry_run:
        with connect() as conn:
            for sym in added:
                conn.execute(
                    "INSERT INTO universe_membership(universe, symbol, source) "
                    "VALUES(?, ?, 'sp500_new');",
                    (universe, sym),
                )
            for sym in rejoined:
                conn.execute(
                    "UPDATE universe_membership SET source='sp500_new' "
                    "WHERE universe=? AND symbol=?;",
                    (universe, sym),
                )
            for sym in potentially_removed:
                conn.execute(
                    "UPDATE universe_membership SET source='sp500_rem' "
                    "WHERE universe=? AND symbol=?",
                    (universe, sym),
                )
        if added:
            log.info("Added %d new symbols (source='sp500_new')", len(added))
        if rejoined:
            log.info(
                "Rejoined %d symbols (source 'sp500_rem' -> 'sp500_new')",
                len(rejoined),
            )
        if potentially_removed:
            log.info(
                "Marked %d symbols source='sp500_rem' (not in current S&P 500)",
                len(potentially_removed),
            )

    with connect() as conn:
        count_row = conn.execute(
            "SELECT COUNT(*) as n FROM universe_membership WHERE universe=?",
            (universe,),
        ).fetchone()
    current_count = count_row["n"] if count_row else 0

    return {
        "universe": universe,
        "sp500_count": len(sp500_current),
        "added": added,
        "rejoined": rejoined,
        "marked_removed": potentially_removed,
        "current_count": current_count,
        "dry_run": dry_run,
    }