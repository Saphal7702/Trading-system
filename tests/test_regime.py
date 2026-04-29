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
    assert r.trending_up is True
    assert r.momentum_positive is True
    assert r.is_crash is False
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
    assert r.trending_up is False
    assert r.is_crash is False
    assert r.buy_notional_mult == 0.75

def test_crash_on_large_daily_drop(db):
    # 200 bars at 100, then a -3% day (below crash_day_pct=-2%)
    insert_bars(db, PROXY, _flat(200, 100.0) + [97.0], end=ASOF)

    from trading.regime import detect_regime
    r = detect_regime(ASOF)

    assert r.regime == "crash"
    assert r.is_crash is True
    assert r.buy_notional_mult == 0.50

def test_defensive_when_insufficient_proxy_history(db):
    # Only 150 bars — less than 200 required
    insert_bars(db, PROXY, _flat(150, 100.0), end=ASOF)

    from trading.regime import detect_regime
    r = detect_regime(ASOF)

    assert r.regime == "defensive"
    assert r.trending_up is False
    assert r.is_crash is False

def test_five_day_return_pct_computed(db):
    # 200 flat bars at 100, then 5 bars ending at 105 → 5-day return = 5%
    prices = _flat(200, 100.0) + [100.0, 101.0, 102.0, 103.0, 105.0]
    insert_bars(db, PROXY, prices, end=ASOF)

    from trading.regime import detect_regime
    r = detect_regime(ASOF)

    assert r.five_day_return_pct is not None
    assert abs(r.five_day_return_pct - 0.05) < 0.001

def test_to_dict_has_new_fields_and_not_old(db):
    insert_bars(db, PROXY, _flat(200, 100.0) + [101.0], end=ASOF)

    from trading.regime import detect_regime
    d = detect_regime(ASOF).to_dict()

    assert "trending_up" in d
    assert "momentum_positive" in d
    assert "five_day_return_pct" in d
    assert "is_crash" in d
    assert "allow_mrit" not in d
    assert "blocked_signal_keys" not in d
