"""
Fetch split-adjusted daily bars from Yahoo Finance (yfinance) and store
them in the backtest database's own bars_daily table.

Completely separate from trading.sqlite and the Alpaca live feed.

Usage (CLI):
    trading backtest-fetch-bars --start 2023-01-01 --end 2025-12-31
    trading backtest-fetch-bars --start 2023-01-01 --end 2025-12-31 --universe sp500
    trading backtest-fetch-bars --start 2023-01-01 --end 2025-12-31 --symbols AAPL MSFT SPY
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Sequence

log = logging.getLogger("trading.backtest.yf_loader")


# ---------------------------------------------------------------------------
# Ticker normalisation
# ---------------------------------------------------------------------------

# Some S&P 500 tickers use dots in Alpaca/CRSP universe files but Yahoo Finance
# expects hyphens.  e.g.  BRK.B → BRK-B,  BF.B → BF-B
_DOT_TO_HYPHEN: dict[str, str] = {
    "BRK.B": "BRK-B",
    "BF.B":  "BF-B",
}


def _to_yf_ticker(symbol: str) -> str:
    """Translate Alpaca-style ticker to Yahoo Finance ticker where they differ."""
    return _DOT_TO_HYPHEN.get(symbol, symbol)


def _from_yf_ticker(yf_symbol: str) -> str:
    """Reverse map: Yahoo Finance ticker back to canonical symbol for DB storage."""
    reverse = {v: k for k, v in _DOT_TO_HYPHEN.items()}
    return reverse.get(yf_symbol, yf_symbol)


# ---------------------------------------------------------------------------
# Core fetch + store
# ---------------------------------------------------------------------------

def fetch_bars_yfinance(
    symbols: Sequence[str],
    start: str,
    end: str,
    backtest_db_path: str | None = None,
    sleep_ms: int = 200,
    batch_size: int = 50,
) -> dict:
    """
    Fetch split-adjusted daily OHLCV bars for `symbols` from yfinance
    and store them in the backtest DB's bars_daily table.

    Args:
        symbols:          List of ticker symbols (e.g. ['AAPL', 'SPY']).
        start:            Start date inclusive, YYYY-MM-DD.
        end:              End date inclusive, YYYY-MM-DD.
        backtest_db_path: Path to backtest.sqlite. Uses default if None.
        sleep_ms:         Milliseconds to sleep between batches.
        batch_size:       How many symbols per yfinance call.

    Returns:
        dict with keys: total_symbols, bars_stored, ok, failed, skipped
    """
    try:
        import yfinance as yf
    except ImportError:
        raise RuntimeError(
            "yfinance is not installed. Run: pip install yfinance"
        )

    from .db import connect_backtest, init_backtest_db

    init_backtest_db(backtest_db_path)

    # yfinance end is exclusive — add one day to include the `end` date
    end_exclusive = (
        datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)
    ).strftime("%Y-%m-%d")

    symbols = [s.strip().upper() for s in symbols if s.strip()]
    if not symbols:
        return {"total_symbols": 0, "bars_stored": 0, "ok": 0, "failed": [], "skipped": []}

    total_bars = 0
    failed: list[str] = []
    skipped: list[str] = []

    for batch_start in range(0, len(symbols), batch_size):
        batch = symbols[batch_start : batch_start + batch_size]
        log.info(
            "Fetching batch %d-%d of %d  (%s … %s)",
            batch_start + 1,
            min(batch_start + batch_size, len(symbols)),
            len(symbols),
            batch[0],
            batch[-1],
        )

        # Translate any dot-notation tickers to Yahoo hyphen form
        yf_batch = [_to_yf_ticker(s) for s in batch]

        try:
            raw = yf.download(
                tickers=yf_batch,
                start=start,
                end=end_exclusive,
                auto_adjust=True,   # split-adjusted OHLC
                actions=False,
                progress=False,
                threads=False,      # disable threading — avoids yfinance cache DB lock
            )
        except Exception as exc:
            log.error("yfinance batch download failed: %s", exc)
            failed.extend(batch)
            continue

        if raw is None or raw.empty:
            log.warning("Empty result for batch %s…%s", batch[0], batch[-1])
            skipped.extend(batch)
            continue

        import pandas as pd
        is_multi = isinstance(raw.columns, pd.MultiIndex)

        conn = connect_backtest(backtest_db_path)
        try:
            for canonical_sym, yf_sym in zip(batch, yf_batch):
                try:
                    if is_multi:
                        # MultiIndex columns: (field, yf_ticker)
                        level1_vals = raw.columns.get_level_values(1)
                        if yf_sym not in level1_vals:
                            log.warning(
                                "Symbol %s (%s) not in yfinance response",
                                canonical_sym, yf_sym,
                            )
                            skipped.append(canonical_sym)
                            continue
                        df = raw.xs(yf_sym, axis=1, level=1).dropna(subset=["Close"])
                    else:
                        # Single-symbol download — flat columns
                        df = raw.dropna(subset=["Close"])

                    if df.empty:
                        log.warning("No data for %s", canonical_sym)
                        skipped.append(canonical_sym)
                        continue

                    rows = []
                    for idx, row in df.iterrows():
                        def _f(col: str) -> float | None:
                            v = row.get(col)
                            if v is None:
                                return None
                            try:
                                f = float(v)
                                return None if (f != f) else f  # NaN check
                            except (TypeError, ValueError):
                                return None

                        c = _f("Close")
                        if c is None:
                            continue
                        rows.append((
                            canonical_sym,              # store under Alpaca ticker
                            idx.strftime("%Y-%m-%d"),
                            _f("Open"),
                            _f("High"),
                            _f("Low"),
                            c,
                            _f("Volume"),
                        ))

                    if not rows:
                        skipped.append(canonical_sym)
                        continue

                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO bars_daily
                            (symbol, t, o, h, l, c, v, source, fetched_at)
                        VALUES
                            (?, ?, ?, ?, ?, ?, ?, 'yfinance', datetime('now'))
                        """,
                        rows,
                    )
                    conn.commit()
                    total_bars += len(rows)
                    log.debug("Stored %d bars for %s", len(rows), canonical_sym)

                except Exception as sym_exc:
                    log.error("Failed to store %s: %s", canonical_sym, sym_exc)
                    failed.append(canonical_sym)
        finally:
            conn.close()

        if sleep_ms > 0 and batch_start + batch_size < len(symbols):
            time.sleep(sleep_ms / 1000.0)

    result = {
        "total_symbols": len(symbols),
        "bars_stored": total_bars,
        "ok": len(symbols) - len(failed) - len(skipped),
        "failed": failed,
        "skipped": skipped,
    }
    log.info(
        "yf_loader done | symbols=%d ok=%d bars=%d failed=%d skipped=%d",
        result["total_symbols"], result["ok"], result["bars_stored"],
        len(failed), len(skipped),
    )
    return result


# ---------------------------------------------------------------------------
# Helper: load symbols from universe_membership in trading.sqlite
# ---------------------------------------------------------------------------

def symbols_from_universe(universe: str = "sp500") -> list[str]:
    """Return all symbols in the given universe from the live trading DB."""
    from ..db import connect
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM universe_membership WHERE universe=? ORDER BY symbol",
            (universe,),
        ).fetchall()
    syms = [r["symbol"] for r in rows]
    if "SPY" not in syms:
        syms.append("SPY")
    return syms


# ---------------------------------------------------------------------------
# Status helper
# ---------------------------------------------------------------------------

def bar_coverage_summary(backtest_db_path: str | None = None) -> dict:
    """Quick summary of what's stored in backtest bars_daily."""
    from .db import connect_backtest
    try:
        with connect_backtest(backtest_db_path) as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(DISTINCT symbol) AS symbol_count,
                    MIN(t)                 AS min_date,
                    MAX(t)                 AS max_date,
                    COUNT(*)               AS total_rows
                FROM bars_daily
                """
            ).fetchone()
        if row:
            return dict(row)
    except Exception:
        pass
    return {"symbol_count": 0, "min_date": None, "max_date": None, "total_rows": 0}