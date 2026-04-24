from __future__ import annotations

import pytest
from tests.conftest import insert_bars, insert_universe

ASOF = "2024-05-01"

def _buy_prices(n: int = 122) -> list[float]:
    """n-1 bars at 90, last bar at 110 → SMA20 crosses above SMA50 on final bar."""
    return [90.0] * (n - 1) + [110.0]

def _sell_prices(n: int = 122) -> list[float]:
    """n-1 bars at 110, last bar at 90 → SMA20 crosses below SMA50 on final bar."""
    return [110.0] * (n - 1) + [90.0]

def test_buy_signal_on_crossover(db):
    insert_bars(db, "AAPL", _buy_prices(), end=ASOF)
    insert_universe(db, "AAPL", asof=ASOF)

    from trading.strategy_sma import generate_signals_sma
    signals = generate_signals_sma(asof=ASOF)

    assert len(signals) == 1
    s = signals[0]
    assert s.symbol == "AAPL"
    assert s.action == "buy"
    assert s.strength is not None and s.strength > 0

def test_sell_signal_on_crossover_down(db):
    insert_bars(db, "AAPL", _sell_prices(), end=ASOF)
    insert_universe(db, "AAPL", asof=ASOF)

    from trading.strategy_sma import generate_signals_sma
    signals = generate_signals_sma(asof=ASOF)

    assert len(signals) == 1
    assert signals[0].action == "sell"

def test_hold_when_no_crossover(db):
    insert_bars(db, "AAPL", [100.0] * 122, end=ASOF)
    insert_universe(db, "AAPL", asof=ASOF)

    from trading.strategy_sma import generate_signals_sma
    signals = generate_signals_sma(asof=ASOF)

    assert signals[0].action == "hold"

def test_hold_when_insufficient_data(db):
    insert_bars(db, "AAPL", [100.0] * 50, end=ASOF)  # less than lookback_min=120
    insert_universe(db, "AAPL", asof=ASOF)

    from trading.strategy_sma import generate_signals_sma
    signals = generate_signals_sma(asof=ASOF)

    assert signals[0].action == "hold"
    assert "Not enough data" in signals[0].reason

def test_empty_result_when_no_universe_symbols(db):
    # No universe_daily rows inserted → no symbols → no signals
    insert_bars(db, "AAPL", _buy_prices(), end=ASOF)

    from trading.strategy_sma import generate_signals_sma
    signals = generate_signals_sma(asof=ASOF)

    assert signals == []


def test_hold_when_last_bar_close_is_null(db):
    # Simulates an intraday bar whose close has not yet been written.
    # NULL close is filtered; remaining 122 flat bars produce HOLD with no crash.
    insert_bars(db, "AAPL", [100.0] * 122, end="2024-04-30")
    insert_universe(db, "AAPL", asof=ASOF)

    from trading.db import connect
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO bars_daily(symbol, t, o, h, l, c, v)"
            " VALUES ('AAPL', ?, 100, 100, 100, NULL, 0);",
            (ASOF,),
        )

    from trading.strategy_sma import generate_signals_sma
    signals = generate_signals_sma(asof=ASOF)

    assert len(signals) == 1
    assert signals[0].action == "hold"