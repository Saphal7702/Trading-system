from __future__ import annotations
import pytest
from tests.conftest import insert_buying_power
from trading.strategy_sma import Signal

def _buy(symbol: str, strength: float = 0.5) -> Signal:
    return Signal(symbol, "buy", "SMA20 crossed above SMA50", strength=strength)

def _sell(symbol: str) -> Signal:
    return Signal(symbol, "sell", "SMA20 crossed below SMA50", strength=0.5)

def test_basic_buy_intent(db):
    insert_buying_power(db, 500.0)

    from trading.planner import plan_intents
    intents = plan_intents([_buy("AAPL")], allow_buys=True)

    buys = [i for i in intents if i.action == "buy"]
    assert len(buys) == 1
    assert buys[0].symbol == "AAPL"
    assert buys[0].target_notional is not None

def test_max_positions_cap(db, monkeypatch):
    monkeypatch.setenv("TRADING_MAX_POSITIONS", "2")
    insert_buying_power(db, 10_000.0)

    from trading.planner import plan_intents
    signals = [_buy("AAPL"), _buy("MSFT"), _buy("GOOG")]
    intents = plan_intents(signals, allow_buys=True)

    buys = [i for i in intents if i.action == "buy"]
    assert len(buys) == 2

def test_insufficient_buying_power_blocks_buy(db, monkeypatch):
    monkeypatch.setenv("TRADING_PER_POSITION", "200.0")
    insert_buying_power(db, 50.0)  # less than per_position

    from trading.planner import plan_intents
    intents = plan_intents([_buy("AAPL")], allow_buys=True)

    buys = [i for i in intents if i.action == "buy"]
    assert len(buys) == 0

def test_risk_blocked_buys(db):
    insert_buying_power(db, 10_000.0)

    from trading.planner import plan_intents
    intents = plan_intents([_buy("AAPL")], allow_buys=False, risk_state="SELL_ONLY")

    holds = [i for i in intents if i.action == "hold"]
    assert len(holds) == 1
    assert "risk" in holds[0].reason.lower()

def test_already_holding_skips_buy(db):
    insert_buying_power(db, 10_000.0)

    # Insert an open position
    from trading.db import connect
    with connect() as conn:
        conn.execute(
            "INSERT INTO positions(symbol, qty, opened_at) VALUES ('AAPL', 1.0, datetime('now', '-2 days'));",
        )

    from trading.planner import plan_intents
    intents = plan_intents([_buy("AAPL")], allow_buys=True)

    buys = [i for i in intents if i.action == "buy"]
    assert len(buys) == 0
    holds = [i for i in intents if i.symbol == "AAPL" and i.action == "hold"]
    assert len(holds) == 1


def test_exposure_cap_blocks_buy(db, monkeypatch):
    # AAPL already uses $250 of the $300 cap for sma20_cross_up_sma50;
    # adding MSFT ($100) would push it to $350 → blocked
    monkeypatch.setenv("TRADING_PER_POSITION", "100.0")
    monkeypatch.setenv("TRADING_MAX_EXPOSURE_PER_SIGNAL_KEY", "300.0")
    insert_buying_power(db, 10_000.0)

    from trading.db import connect
    with connect() as conn:
        conn.execute(
            "INSERT INTO positions(symbol, qty, opened_at, entry_signal_key, entry_notional)"
            " VALUES ('AAPL', 1.0, datetime('now','-2 days'), 'sma20_cross_up_sma50', 250.0);"
        )

    from trading.planner import plan_intents
    intents = plan_intents([_buy("MSFT")], allow_buys=True)

    buys = [i for i in intents if i.action == "buy"]
    assert len(buys) == 0
    holds = [i for i in intents if i.symbol == "MSFT" and i.action == "hold"]
    assert len(holds) == 1
    assert "xposure" in holds[0].reason