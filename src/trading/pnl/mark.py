from __future__ import annotations

from typing import Optional
from ..db import connect


def get_close(symbol: str, asof: str) -> Optional[float]:
    """
    Returns close price for symbol at date `asof` (YYYY-MM-DD).
    If that exact date isn't present, returns the most recent close <= asof.
    """
    sym = symbol.strip().upper()

    with connect() as conn:
        row = conn.execute(
            """
            SELECT c
            FROM bars_daily
            WHERE symbol = ? AND t <= ?
            ORDER BY t DESC
            LIMIT 1;
            """,
            (sym, asof),
        ).fetchone()

    if not row:
        return None
    return float(row["c"]) if row["c"] is not None else None
