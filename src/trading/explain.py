from __future__ import annotations
from .db import connect
from .strategy_sma import _sma  # yes, reuse internal helper for now

def explain_symbol(symbol: str, fast: int = 20, slow: int = 50, tail: int = 5) -> dict:
    sym = symbol.strip().upper()
    with connect() as conn:
        rows = conn.execute(
            "SELECT t, c FROM bars_daily WHERE symbol=? ORDER BY t ASC;",
            (sym,),
        ).fetchall()

    closes = [float(r["c"]) for r in rows if r["c"] is not None]
    dates = [r["t"] for r in rows if r["c"] is not None]

    if len(closes) < slow + 2:
        return {"symbol": sym, "error": f"Not enough data ({len(closes)} closes)"}

    sma_fast = _sma(closes, fast)
    sma_slow = _sma(closes, slow)

    i = len(closes) - 1
    prev = i - 1

    return {
        "symbol": sym,
        "last_date": dates[i],
        "close": closes[i],
        "fast_now": sma_fast[i],
        "slow_now": sma_slow[i],
        "fast_prev": sma_fast[prev],
        "slow_prev": sma_slow[prev],
        "last_closes": list(zip(dates[-tail:], closes[-tail:])),
    }
