from __future__ import annotations

from ..asof import resolve_asof_date
from ..db import connect

def build_universe_daily(
    universe: str,
    asof: str | None,
    top: int,
    min_adv20: float = 20_000_000.0,
) -> tuple[str, int]:
    """
    Returns (resolved_asof_yyyy_mm_dd, rows_written)

    asof: 'YYYY-MM-DD' close date to evaluate
      - if None: use latest available trading day in bars_daily
      - if provided: snap to last trading day <= asof
    """

    with connect() as conn:
        resolved_asof = resolve_asof_date(conn, asof)   # ✅ always resolves
        members = conn.execute(
            "SELECT symbol FROM universe_membership WHERE universe=?;",
            (universe,),
        ).fetchall()

    symbols = [r["symbol"] for r in members]
    if not symbols:
        return resolved_asof, 0

    inserted = 0
    with connect() as conn:
        # idempotent rebuild
        conn.execute(
            "DELETE FROM universe_daily WHERE asof_date=? AND universe=?;",
            (resolved_asof, universe),
        )

        for sym in symbols:
            # asset gate
            asset = conn.execute(
                "SELECT tradable, fractionable FROM assets_cache WHERE symbol=?;",
                (sym,),
            ).fetchone()
            if not asset or asset["tradable"] != 1 or asset["fractionable"] != 1:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO universe_daily(asof_date, universe, symbol, include, reason)
                    VALUES (?, ?, ?, 0, ?);
                    """,
                    (resolved_asof, universe, sym, "not tradable/fractionable"),
                )
                inserted += 1
                continue

            # close on resolved_asof
            row_close = conn.execute(
                "SELECT c FROM bars_daily WHERE symbol=? AND t=?;",
                (sym, resolved_asof),
            ).fetchone()
            if not row_close or row_close["c"] is None:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO universe_daily(asof_date, universe, symbol, include, reason)
                    VALUES (?, ?, ?, 0, ?);
                    """,
                    (resolved_asof, universe, sym, "no close on asof"),
                )
                inserted += 1
                continue

            close = float(row_close["c"])

            # ADV20
            adv = conn.execute(
                """
                SELECT AVG(c * v) AS adv20
                FROM (
                  SELECT c, v
                  FROM bars_daily
                  WHERE symbol=? AND t<=?
                  ORDER BY t DESC
                  LIMIT 20
                );
                """,
                (sym, resolved_asof),
            ).fetchone()
            adv20 = float(adv["adv20"]) if adv and adv["adv20"] is not None else 0.0

            # ret60
            prev = conn.execute(
                """
                SELECT c
                FROM bars_daily
                WHERE symbol=? AND t<=?
                ORDER BY t DESC
                LIMIT 1 OFFSET 60;
                """,
                (sym, resolved_asof),
            ).fetchone()

            if not prev or prev["c"] is None:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO universe_daily(asof_date, universe, symbol, close, adv20, include, reason)
                    VALUES (?, ?, ?, ?, ?, 0, ?);
                    """,
                    (resolved_asof, universe, sym, close, adv20, "insufficient history"),
                )
                inserted += 1
                continue

            prev_close = float(prev["c"])
            ret60 = (close / prev_close) - 1.0 if prev_close > 0 else None

            include = 1
            reason = "ok"
            if adv20 < min_adv20:
                include = 0
                reason = f"adv20<{min_adv20}"

            score = ret60 if ret60 is not None else None

            conn.execute(
                """
                INSERT OR REPLACE INTO universe_daily(
                  asof_date, universe, symbol, close, adv20, ret60, include, reason, score
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (resolved_asof, universe, sym, close, adv20, ret60, include, reason, score),
            )
            inserted += 1

        # keep top N
        if top and top > 0:
            conn.execute(
                """
                UPDATE universe_daily
                SET include=0, reason='below topN'
                WHERE asof_date=? AND universe=? AND include=1 AND symbol NOT IN (
                  SELECT symbol
                  FROM universe_daily
                  WHERE asof_date=? AND universe=? AND include=1 AND score IS NOT NULL
                  ORDER BY score DESC
                  LIMIT ?
                );
                """,
                (resolved_asof, universe, resolved_asof, universe, top),
            )

    return resolved_asof, inserted