from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
import sqlite3


@dataclass(frozen=True)
class ClosingExcursions:
    closing_id: int
    max_high: float | None
    min_low: float | None
    mfe_pct: float | None
    mae_pct: float | None
    capture_pct: float | None


def _to_date_str(dt: str | None) -> str | None:
    # dt looks like "2026-01-28T15:32:00Z" or "2026-01-28 15:32:00"
    # SQLite date() can handle many forms, but we’ll just take first 10 chars safely.
    if not dt:
        return None
    return dt[:10]


def compute_excursions_for_closings(
    conn: sqlite3.Connection,
    *,
    closings: Iterable[dict],  # journal rows dict-like (must include keys below)
) -> dict[int, ClosingExcursions]:
    """
    Expects each closing row to have:
      closing_id, symbol, entry_filled_at, exit_filled_at, entry_price, realized_ret
    Returns mapping: closing_id -> excursions
    """
    out: dict[int, ClosingExcursions] = {}

    sql = """
    SELECT
      MAX(h) AS max_high,
      MIN(l) AS min_low
    FROM bars_daily
    WHERE symbol = ?
      AND date(t) >= date(?)
      AND date(t) <= date(?);
    """

    for r in closings:
        closing_id = int(r["closing_id"])
        symbol = r["symbol"]
        entry_price = r.get("entry_price")
        realized_ret = r.get("realized_ret")

        entry_date = _to_date_str(r.get("entry_filled_at"))
        exit_date = _to_date_str(r.get("exit_filled_at"))

        # Guardrails
        if not symbol or entry_price is None or entry_price == 0 or not entry_date or not exit_date:
            out[closing_id] = ClosingExcursions(closing_id, None, None, None, None, None)
            continue

        row = conn.execute(sql, (symbol, entry_date, exit_date)).fetchone()
        max_high = row[0] if row else None
        min_low = row[1] if row else None

        mfe_pct = None
        mae_pct = None
        capture_pct = None

        if max_high is not None:
            mfe_pct = ((max_high - entry_price) / entry_price) * 100.0
        if min_low is not None:
            mae_pct = ((min_low - entry_price) / entry_price) * 100.0

        # capture: realized_ret is a fraction (e.g., 0.1655) from your journal SQL
        if mfe_pct is not None and mfe_pct > 0 and realized_ret is not None:
            capture_pct = (realized_ret / (mfe_pct / 100.0)) * 100.0
        else:
            capture_pct = None

        out[closing_id] = ClosingExcursions(
            closing_id=closing_id,
            max_high=max_high,
            min_low=min_low,
            mfe_pct=mfe_pct,
            mae_pct=mae_pct,
            capture_pct=capture_pct,
        )

    return out