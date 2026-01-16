from __future__ import annotations
from datetime import date
from ..db import connect

def build_universe_daily(universe: str, asof: str, top: int, min_adv20: float = 20_000_000.0) -> int:
    """
    asof: 'YYYY-MM-DD' close date to evaluate (must exist in bars_daily for symbols)
    Score = ret60 (simple v1). Later we improve.
    Filters:
      - assets_cache.tradable=1
      - assets_cache.fractionable=1
      - ADV20 >= min_adv20
      - has enough history for ret60 and adv20 window
    """
    # pull symbols from membership
    with connect() as conn:
        members = conn.execute(
            "SELECT symbol FROM universe_membership WHERE universe=?;",
            (universe,),
        ).fetchall()
    symbols = [r["symbol"] for r in members]
    if not symbols:
        return 0

    inserted = 0
    with connect() as conn:
        # wipe existing snapshot for that day/universe (idempotent rebuild)
        conn.execute("DELETE FROM universe_daily WHERE asof_date=? AND universe=?;", (asof, universe))

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
                    (asof, universe, sym, "not tradable/fractionable"),
                )
                continue

            # latest close on asof
            row_close = conn.execute(
                """
                SELECT c
                FROM bars_daily
                WHERE symbol=? AND t=?
                """,
                (sym, asof),
            ).fetchone()
            if not row_close:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO universe_daily(asof_date, universe, symbol, include, reason)
                    VALUES (?, ?, ?, 0, ?);
                    """,
                    (asof, universe, sym, "no close on asof"),
                )
                continue
            close = float(row_close["c"])

            # ADV20: mean(close*volume) over 20 bars ending asof
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
                (sym, asof),
            ).fetchone()
            adv20 = float(adv["adv20"]) if adv and adv["adv20"] is not None else 0.0

            # ret60: (close / close_60d_ago - 1)
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
                conn.execute(
                    """
                    INSERT OR REPLACE INTO universe_daily(asof_date, universe, symbol, close, adv20, include, reason)
                    VALUES (?, ?, ?, ?, ?, 0, ?);
                    """,
                    (asof, universe, sym, close, adv20, "insufficient history"),
                )
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
                INSERT OR REPLACE INTO universe_daily(asof_date, universe, symbol, close, adv20, ret60, include, reason, score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (asof, universe, sym, close, adv20, ret60, include, reason, score),
            )
            inserted += 1

        # optional: keep only top N includes by score (set others include=0)
        if top and top > 0:
            keep = conn.execute(
                """
                SELECT symbol
                FROM universe_daily
                WHERE asof_date=? AND universe=? AND include=1 AND score IS NOT NULL
                ORDER BY score DESC
                LIMIT ?;
                """,
                (asof, universe, top),
            ).fetchall()
            keep_set = {r["symbol"] for r in keep}

            conn.execute(
                """
                UPDATE universe_daily
                SET include=0, reason='below topN'
                WHERE asof_date=? AND universe=? AND include=1 AND symbol NOT IN (
                  SELECT symbol FROM universe_daily
                  WHERE asof_date=? AND universe=? AND include=1 AND score IS NOT NULL
                  ORDER BY score DESC
                  LIMIT ?
                );
                """,
                (asof, universe, asof, universe, top),
            )

    return inserted
