from __future__ import annotations
from .db import connect

def resolve_asof_date(conn, asof: str | None = None) -> str:
    """
    Returns YYYY-MM-DD trading date that exists in bars_daily.
    - If asof is None → latest available trading day
    - If asof is provided → snap to last trading day <= asof
    """
    if asof:
        r = conn.execute(
            "SELECT MAX(t) AS d FROM bars_daily WHERE t <= ?;",
            (asof[:10],),
        ).fetchone()
    else:
        r = conn.execute("SELECT MAX(t) AS d FROM bars_daily;").fetchone()

    if not r or not r["d"]:
        raise RuntimeError("No bars_daily data available")

    return r["d"][:10]

