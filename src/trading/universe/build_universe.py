from __future__ import annotations
from datetime import date
from ..db import connect
from ..asof import resolve_asof_date

def _insert_excluded(conn, asof: str, universe: str, symbol: str, reason: str):
    conn.execute(
        """
        INSERT OR REPLACE INTO universe_daily(
            asof_date, universe, symbol, include, reason
        )
        VALUES (?, ?, ?, 0, ?);
        """,
        (asof, universe, symbol, reason),
    )


def build_universe_daily(
    universe: str,
    asof: str | None = None,
    top: int = 200,
    min_adv20: float = 20_000_000.0,
) -> int:
    """
    Daily universe snapshot builder.
    Safe for cron execution (asof auto-resolved).
    """

    with connect() as conn:
        asof = resolve_asof_date(conn, asof)

        members = conn.execute(
            "SELECT symbol FROM universe_membership WHERE universe=?;",
            (universe,),
        ).fetchall()

    symbols = [r["symbol"] for r in members]
    if not symbols:
        return 0

    inserted = 0
    with connect() as conn:
        conn.execute(
            "DELETE FROM universe_daily WHERE asof_date=? AND universe=?;",
            (asof, universe),
        )

        for sym in symbols:
            # asset gate
            asset = conn.execute(
                "SELECT tradable, fractionable FROM assets_cache WHERE symbol=?;",
                (sym,),
            ).fetchone()

            if not asset or asset["tradable"] != 1 or asset["fractionable"] != 1:
                _insert_excluded(conn, asof, universe, sym, "not tradable/fractionable")
                continue

            close_row = conn.execute(
                "SELECT c FROM bars_daily WHERE symbol=? AND t=?;",
                (sym, asof),
            ).fetchone()

            if not close_row:
                _insert_excluded(conn, asof, universe, sym, "no close on asof")
                continue

            close = float(close_row["c"])

            adv20 = conn.execute(
                """
                SELECT AVG(c * v)
                FROM (
                    SELECT c, v
                    FROM bars_daily
                    WHERE symbol=? AND t<=?
                    ORDER BY t DESC
                    LIMIT 20
                );
                """,
                (sym, asof),
            ).fetchone()[0] or 0.0

            prev = conn.execute(
                """
                SELECT c
                FROM bars_daily
                WHERE symbol=? AND t<=?
                ORDER BY t DESC
                LIMIT 1 OFFSET 60;
                """,
                (sym, asof),
            ).fetchone()

            if not prev:
                _insert_excluded(conn, asof, universe, sym, "insufficient history", close, adv20)
                continue

            ret60 = (close / float(prev["c"])) - 1.0
            include = 1 if adv20 >= min_adv20 else 0
            reason = "ok" if include else f"adv20<{min_adv20}"

            conn.execute(
                """
                INSERT OR REPLACE INTO universe_daily
                (asof_date, universe, symbol, close, adv20, ret60, score, include, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (asof, universe, sym, close, adv20, ret60, ret60, include, reason),
            )

            inserted += 1

        if top > 0:
            conn.execute(
                """
                UPDATE universe_daily
                SET include=0, reason='below topN'
                WHERE asof_date=? AND universe=? AND include=1
                AND symbol NOT IN (
                    SELECT symbol
                    FROM universe_daily
                    WHERE asof_date=? AND universe=? AND include=1
                    ORDER BY score DESC
                    LIMIT ?
                );
                """,
                (asof, universe, asof, universe, top),
            )

    return inserted