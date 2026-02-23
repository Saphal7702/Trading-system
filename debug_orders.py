from dotenv import load_dotenv
load_dotenv()

import os
import sqlite3
from pathlib import Path
import math

DB_PATH = os.getenv("TRADING_DB_PATH", "").strip()
if not DB_PATH:
    # fallback if you use a default path in your project
    DB_PATH = "trading.db"

def fetch_atr(conn, symbol: str, asof: str, period: int = 14, lookback_pad: int = 5):
    n = max(period + 1 + lookback_pad, period + 2)
    rows = conn.execute(
        """
        SELECT t, h, l, c
        FROM bars_daily
        WHERE symbol=? AND t<=?
        ORDER BY t DESC
        LIMIT ?;
        """,
        (symbol, asof, n),
    ).fetchall()

    if not rows or len(rows) < period + 1:
        return None

    rows = list(reversed(rows))  # chronological

    highs, lows, closes = [], [], []
    for (t, h, l, c) in rows:
        if h is None or l is None or c is None:
            continue
        highs.append(float(h))
        lows.append(float(l))
        closes.append(float(c))

    if len(closes) < period + 1:
        return None

    trs = []
    for i in range(1, len(closes)):
        h = highs[i]; l = lows[i]; pc = closes[i - 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        if tr <= 0 or math.isnan(tr):
            continue
        trs.append(float(tr))

    if len(trs) < period:
        return None

    window = trs[-period:]
    atr = sum(window) / float(period)
    return atr if atr > 0 and not math.isnan(atr) else None


tp_mult = float(os.getenv("TRADING_EXIT_PROFILE_MRIT_TP_ATR_MULT", "1.0"))
sl_mult = float(os.getenv("TRADING_EXIT_PROFILE_MRIT_SL_ATR_MULT", "1.5"))
atr_period = int(float(os.getenv("TRADING_EXIT_PROFILE_MRIT_ATR_PERIOD", "14")))

con = sqlite3.connect(DB_PATH)
con.row_factory = sqlite3.Row
asof = con.execute("SELECT MAX(t) AS t FROM bars_daily").fetchone()["t"]

rows = con.execute(
    """
    SELECT
      UPPER(pl.symbol) AS symbol,
      SUM(pl.qty_open) AS qty_open,
      SUM(pl.qty_open * pl.entry_price) / NULLIF(SUM(pl.qty_open),0) AS vwap_entry
    FROM position_lots pl
    JOIN positions p ON UPPER(p.symbol)=UPPER(pl.symbol)
    WHERE pl.qty_open > 0 AND p.qty > 0.000001
    GROUP BY UPPER(pl.symbol);
    """
).fetchall()

print("asof:", asof)
print("MRIT ATR settings:", atr_period, "TPx", tp_mult, "SLx", sl_mult)
print()

for r in rows:
    sym = r["symbol"]
    # only check your MRIT symbols quickly; add more if needed
    if sym not in ("WMT", "PFE"):
        continue

    last = con.execute(
        "SELECT c FROM bars_daily WHERE symbol=? AND t<=? ORDER BY t DESC LIMIT 1;",
        (sym, asof),
    ).fetchone()
    last_close = float(last["c"]) if last and last["c"] is not None else None

    atr = fetch_atr(con, sym, asof, period=atr_period)
    entry = float(r["vwap_entry"]) if r["vwap_entry"] is not None else None

    print(sym, "entry", entry, "last", last_close, "atr", atr)
    if entry is None or last_close is None or atr is None:
        print("  -> cannot compute TP/SL (missing entry/last/ATR)")
        continue

    tp_px = entry + tp_mult * atr
    sl_px = entry - sl_mult * atr
    print(f"  TP@ {tp_px:.2f}  SL@ {sl_px:.2f}  (need last >= TP or last <= SL)")