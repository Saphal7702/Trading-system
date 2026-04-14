from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import os

from .db import connect

@dataclass(frozen=True)
class RegimeState:
    asof: str
    proxy_symbol: str

    close: float | None
    sma50: float | None
    sma200: float | None
    day_return_pct: float | None
    drawdown_20d_pct: float | None

    regime: str  # bull | pullback | defensive | crash
    allow_buys: bool = True
    allow_mrit: bool = True
    buy_notional_mult: float = 1.0
    exposure_cap_mult: float = 1.0

    blocked_signal_keys: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asof": self.asof,
            "proxy_symbol": self.proxy_symbol,
            "close": self.close,
            "sma50": self.sma50,
            "sma200": self.sma200,
            "day_return_pct": self.day_return_pct,
            "drawdown_20d_pct": self.drawdown_20d_pct,
            "regime": self.regime,
            "allow_buys": self.allow_buys,
            "allow_mrit": self.allow_mrit,
            "buy_notional_mult": self.buy_notional_mult,
            "exposure_cap_mult": self.exposure_cap_mult,
            "blocked_signal_keys": list(self.blocked_signal_keys),
            "notes": list(self.notes),
        }

def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return float(default)
    try:
        return float(v)
    except ValueError:
        return float(default)


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return int(default)
    try:
        return int(v)
    except ValueError:
        return int(default)


def _fetch_recent_closes(symbol: str, asof: str, lookback: int) -> list[float]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT c
            FROM bars_daily
            WHERE symbol = ?
              AND t <= ?
            ORDER BY t DESC
            LIMIT ?;
            """,
            (symbol, asof, lookback),
        ).fetchall()

    closes = [float(r["c"]) for r in rows if r["c"] is not None]
    closes.reverse()  # oldest -> newest
    return closes


def _sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / float(n)


def detect_regime(asof: str) -> RegimeState:
    """
    Clean, conservative regime detector.

    Inputs from env:
      TRADING_REGIME_PROXY=SPY
      TRADING_REGIME_LOOKBACK=220
      TRADING_REGIME_PULLBACK_DAY_PCT=-0.010
      TRADING_REGIME_CRASH_DAY_PCT=-0.020
      TRADING_REGIME_CRASH_20D_DD_PCT=-0.080
    """
    proxy = os.getenv("TRADING_REGIME_PROXY", "SPY").strip().upper() or "SPY"
    lookback = _env_int("TRADING_REGIME_LOOKBACK", 220)

    pullback_day_pct = _env_float("TRADING_REGIME_PULLBACK_DAY_PCT", -0.010)
    crash_day_pct = _env_float("TRADING_REGIME_CRASH_DAY_PCT", -0.020)
    crash_20d_dd_pct = _env_float("TRADING_REGIME_CRASH_20D_DD_PCT", -0.080)

    closes = _fetch_recent_closes(proxy, asof, lookback)

    if len(closes) < 200:
        return RegimeState(
            asof=asof,
            proxy_symbol=proxy,
            close=closes[-1] if closes else None,
            sma50=None,
            sma200=None,
            day_return_pct=None,
            drawdown_20d_pct=None,
            regime="defensive",
            allow_buys=True,
            allow_mrit=False,
            buy_notional_mult=0.75,
            exposure_cap_mult=0.75,
            blocked_signal_keys=("mrit_rsi2_pullback_uptrend",),
            notes=("insufficient_proxy_history",),
        )

    close = closes[-1]
    prev_close = closes[-2] if len(closes) >= 2 else None
    sma50 = _sma(closes, 50)
    sma200 = _sma(closes, 200)

    day_return_pct = None
    if prev_close and prev_close != 0:
        day_return_pct = (close / prev_close) - 1.0

    trailing_20 = closes[-20:]
    peak20 = max(trailing_20) if trailing_20 else None
    drawdown_20d_pct = None
    if peak20 and peak20 != 0:
        drawdown_20d_pct = (close / peak20) - 1.0

    notes: list[str] = []

    # Highest priority: crash regime
    is_crash = False
    if day_return_pct is not None and day_return_pct <= crash_day_pct:
        is_crash = True
        notes.append("proxy_daily_crash")
    if drawdown_20d_pct is not None and drawdown_20d_pct <= crash_20d_dd_pct:
        is_crash = True
        notes.append("proxy_20d_drawdown_crash")

    if is_crash:
        return RegimeState(
            asof=asof,
            proxy_symbol=proxy,
            close=close,
            sma50=sma50,
            sma200=sma200,
            day_return_pct=day_return_pct,
            drawdown_20d_pct=drawdown_20d_pct,
            regime="crash",
            allow_buys=True,   # keep trend entries possible; planner will block MRIT only
            allow_mrit=False,
            buy_notional_mult=0.50,
            exposure_cap_mult=0.50,
            blocked_signal_keys=("mrit_rsi2_pullback_uptrend",),
            notes=tuple(notes),
        )

    # Below 200 SMA = defensive
    if sma200 is not None and close < sma200:
        notes.append("below_sma200")
        return RegimeState(
            asof=asof,
            proxy_symbol=proxy,
            close=close,
            sma50=sma50,
            sma200=sma200,
            day_return_pct=day_return_pct,
            drawdown_20d_pct=drawdown_20d_pct,
            regime="defensive",
            allow_buys=True,
            allow_mrit=False,
            buy_notional_mult=0.75,
            exposure_cap_mult=0.75,
            blocked_signal_keys=("mrit_rsi2_pullback_uptrend",),
            notes=tuple(notes),
        )

    # Below 50 SMA or weak day = pullback
    if (sma50 is not None and close < sma50) or (
        day_return_pct is not None and day_return_pct <= pullback_day_pct
    ):
        if sma50 is not None and close < sma50:
            notes.append("below_sma50")
        if day_return_pct is not None and day_return_pct <= pullback_day_pct:
            notes.append("weak_day")
        return RegimeState(
            asof=asof,
            proxy_symbol=proxy,
            close=close,
            sma50=sma50,
            sma200=sma200,
            day_return_pct=day_return_pct,
            drawdown_20d_pct=drawdown_20d_pct,
            regime="pullback",
            allow_buys=True,
            allow_mrit=False,
            buy_notional_mult=0.85,
            exposure_cap_mult=0.85,
            blocked_signal_keys=("mrit_rsi2_pullback_uptrend",),
            notes=tuple(notes),
        )

    # Otherwise: bull
    return RegimeState(
        asof=asof,
        proxy_symbol=proxy,
        close=close,
        sma50=sma50,
        sma200=sma200,
        day_return_pct=day_return_pct,
        drawdown_20d_pct=drawdown_20d_pct,
        regime="bull",
        allow_buys=True,
        allow_mrit=True,
        buy_notional_mult=1.0,
        exposure_cap_mult=1.0,
        blocked_signal_keys=(),
        notes=tuple(notes),
    )