from __future__ import annotations
import pytest
from tests.conftest import insert_bars, insert_universe

ASOF = "2024-05-01"

def _spy_prices(n: int = 250) -> list[float]:
    """SPY above SMA200: first 200 bars at 100, rest at 101."""
    return [100.0] * 200 + [101.0] * (n - 200)

def _mrit_buy_prices(n: int = 122) -> list[float]:
    """
    Price series that triggers MRIT buy on the last bar.
    Conditions at last bar (price=95):
      - close(95) > SMA50(~91.9)  → uptrend ✓
      - close(95) < SMA10(~99.5)  → below mean ✓
      - RSI2 ≈ 0 <= 10            → oversold ✓

    Layout:
      bars 0-71:   price 80   (72 bars)
      bars 72-111: price 90   (40 bars)
      bars 112-120: price 100  (9 bars)
      bar 121:     price 95   (pullback)
    """
    return [80.0] * 72 + [90.0] * 40 + [100.0] * 9 + [95.0]

def test_buy_signal_all_conditions_met(db):
    insert_bars(db, "SPY", _spy_prices(), end=ASOF)
    insert_bars(db, "AAPL", _mrit_buy_prices(), end=ASOF)
    insert_universe(db, "AAPL", asof=ASOF)

    from trading.strategy_mrit import generate_signals_mrit
    signals = generate_signals_mrit(asof=ASOF)

    buys = [s for s in signals if s.action == "buy"]
    assert len(buys) == 1
    assert buys[0].symbol == "AAPL"
    assert buys[0].strength is not None and buys[0].strength > 0

def test_gate_blocks_when_spy_below_sma200(db):
    # SPY always below SMA200: all bars at 50, SMA200=50, close=50, not strictly greater
    insert_bars(db, "SPY", [50.0] * 250, end=ASOF)
    insert_bars(db, "AAPL", _mrit_buy_prices(), end=ASOF)
    insert_universe(db, "AAPL", asof=ASOF)

    from trading.strategy_mrit import generate_signals_mrit
    signals = generate_signals_mrit(asof=ASOF)

    assert all(s.action == "hold" for s in signals)

def test_gate_disabled_returns_holds_without_checking_spy(db, monkeypatch):
    # Gate off: code should skip gate check entirely and return holds (no conditions met anyway)
    monkeypatch.setenv("TRADING_MRIT_MARKET_GATE", "0")
    insert_bars(db, "AAPL", [100.0] * 122, end=ASOF)  # flat prices: RSI2~50, no oversold condition
    insert_universe(db, "AAPL", asof=ASOF)

    from trading.strategy_mrit import generate_signals_mrit
    signals = generate_signals_mrit(asof=ASOF)

    assert all(s.action == "hold" for s in signals)

def test_hold_when_insufficient_proxy_data(db):
    # SPY has only 100 bars — less than sma_n+5=205 minimum for gate
    insert_bars(db, "SPY", [100.0] * 100, end=ASOF)
    insert_bars(db, "AAPL", _mrit_buy_prices(), end=ASOF)
    insert_universe(db, "AAPL", asof=ASOF)

    from trading.strategy_mrit import generate_signals_mrit
    signals = generate_signals_mrit(asof=ASOF)

    assert all(s.action == "hold" for s in signals)