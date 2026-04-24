from __future__ import annotations

import pytest
from tests.conftest import insert_bars

ASOF = "2024-05-01"
PROXY = "SPY"

def _flat(n: int, price: float) -> list[float]:
    return [price] * n

def test_bull_regime_when_above_both_smas(db):
    # close(101) > SMA50(100) > SMA200(100): bull
    insert_bars(db, PROXY, _flat(200, 100.0) + [101.0], end=ASOF)

    from trading.regime import detect_regime
    r = detect_regime(ASOF)

    assert r.regime == "bull"
    assert r.allow_mrit is True
    assert r.buy_notional_mult == 1.0

def test_pullback_when_close_below_sma50(db):
    # 200 bars at 100, 50 bars at 80 → SMA50 around 98, close=80 < SMA50 but close > SMA200 not met
    # Use: 50 at 80, 150 at 100, last bar at 99 → close(99) < SMA50(~99.5) and close > SMA200(~93)
    prices = [80.0] * 50 + [100.0] * 150 + [99.0]
    insert_bars(db, PROXY, prices, end=ASOF)

    from trading.regime import detect_regime
    r = detect_regime(ASOF)

    assert r.regime in ("pullback", "defensive")
    assert r.buy_notional_mult <= 0.85

def test_defensive_when_below_sma200(db):
    # 200 bars at 100, last bar at 99 → day_return=-1% (above crash threshold -2%), close(99) < SMA200(~100)
    insert_bars(db, PROXY, _flat(200, 100.0) + [99.0], end=ASOF)

    from trading.regime import detect_regime
    r = detect_regime(ASOF)

    assert r.regime == "defensive"
    assert r.allow_mrit is False
    assert r.buy_notional_mult == 0.75

def test_crash_on_large_daily_drop(db):
    # 200 bars at 100, then a -3% day (below crash_day_pct=-2%)
    insert_bars(db, PROXY, _flat(200, 100.0) + [97.0], end=ASOF)

    from trading.regime import detect_regime
    r = detect_regime(ASOF)

    assert r.regime == "crash"
    assert r.allow_mrit is False
    assert r.buy_notional_mult == 0.50

def test_defensive_when_insufficient_proxy_history(db):
    # Only 150 bars — less than 200 required
    insert_bars(db, PROXY, _flat(150, 100.0), end=ASOF)

    from trading.regime import detect_regime
    r = detect_regime(ASOF)

    assert r.regime == "defensive"
    assert r.allow_mrit is False
