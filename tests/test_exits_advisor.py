from __future__ import annotations

from tests.conftest import insert_bars

ASOF = "2024-05-01"


def _insert_position(db: str, symbol: str, entry_price: float, qty: float, entry_date: str) -> None:
    """Set up positions + position_lots + executions for a single open lot."""
    from trading.db import connect
    entry_at = entry_date + "T09:30:00+00:00"
    with connect() as conn:
        conn.execute(
            "INSERT INTO executions(broker, symbol, side, qty, filled_avg_price, filled_at)"
            " VALUES ('alpaca',?,?,?,?,?);",
            (symbol, "buy", qty, entry_price, entry_at),
        )
        exec_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT INTO position_lots(symbol, qty_open, entry_price, entry_filled_at, entry_execution_id)"
            " VALUES (?,?,?,?,?);",
            (symbol, qty, entry_price, entry_at, exec_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO positions(symbol, qty, avg_entry_price, opened_at) VALUES (?,?,?,?);",
            (symbol, qty, entry_price, entry_at),
        )


def test_no_positions_returns_empty(db):
    from trading.exits_advisor import evaluate_exit_advice
    assert evaluate_exit_advice(asof=ASOF) == []


def test_hold_healthy_position(db):
    # entry=100, current=103 (+3%), 1 day held — no rule fires
    insert_bars(db, "AAPL", [100.0, 103.0], end=ASOF)
    _insert_position(db, "AAPL", entry_price=100.0, qty=1.0, entry_date="2024-04-30")

    from trading.exits_advisor import evaluate_exit_advice
    advice = evaluate_exit_advice(asof=ASOF)

    assert len(advice) == 1
    assert advice[0]["action"] == "HOLD"


def test_stop_loss_fires(db):
    # entry=100, current=94 → ret=-6% (exceeds hard-stop -5%)
    insert_bars(db, "AAPL", [100.0, 94.0], end=ASOF)
    _insert_position(db, "AAPL", entry_price=100.0, qty=1.0, entry_date="2024-04-30")

    from trading.exits_advisor import evaluate_exit_advice
    advice = evaluate_exit_advice(asof=ASOF)

    assert advice[0]["action"] == "SELL"
    assert advice[0]["exit_signal_key"].startswith("exit_stop_loss")


def test_take_profit_fires(db):
    # entry=100, current=116 → ret=+16% (exceeds +15% take-profit)
    insert_bars(db, "AAPL", [100.0, 116.0], end=ASOF)
    _insert_position(db, "AAPL", entry_price=100.0, qty=1.0, entry_date="2024-04-30")

    from trading.exits_advisor import evaluate_exit_advice
    advice = evaluate_exit_advice(asof=ASOF)

    assert advice[0]["action"] == "SELL"
    assert advice[0]["exit_signal_key"].startswith("exit_take_profit")


def test_trailing_stop_fires(db):
    # entry=100, peak=110 (peak_gain=+10% ≥ 8%), current=105 (dd=-4.5% ≤ -4%)
    insert_bars(db, "AAPL", [100.0, 110.0, 105.0], end=ASOF)
    _insert_position(db, "AAPL", entry_price=100.0, qty=1.0, entry_date="2024-04-29")

    from trading.exits_advisor import evaluate_exit_advice
    advice = evaluate_exit_advice(asof=ASOF)

    assert advice[0]["action"] == "SELL"
    assert advice[0]["exit_signal_key"].startswith("exit_trailing")


def test_early_failure_fires(db):
    # entry=100, current=99 (-1%), 6 days held (≥ early_fail_days=5), ret ≤ 0%
    insert_bars(db, "AAPL", [100.0] * 6 + [99.0], end=ASOF)
    _insert_position(db, "AAPL", entry_price=100.0, qty=1.0, entry_date="2024-04-25")

    from trading.exits_advisor import evaluate_exit_advice
    advice = evaluate_exit_advice(asof=ASOF)

    assert advice[0]["action"] == "SELL"
    assert advice[0]["exit_signal_key"].startswith("exit_early_failure")


def test_sma_reversal_fires(db):
    # 40 bars at 110, 18 bars at 90, then [89, 90]: SMA20=89.95 < SMA50=101.98
    # Position entered at 89 with ret=+1.1% — no stop or trailing trigger
    insert_bars(db, "AAPL", [110.0] * 40 + [90.0] * 18 + [89.0, 90.0], end=ASOF)
    _insert_position(db, "AAPL", entry_price=89.0, qty=1.0, entry_date="2024-04-30")

    from trading.exits_advisor import evaluate_exit_advice
    advice = evaluate_exit_advice(asof=ASOF)

    assert advice[0]["action"] == "SELL"
    assert advice[0]["exit_signal_key"] == "exit_sma_reversal_sma20_below_sma50"


def test_stop_loss_takes_priority_over_sma_reversal(db):
    # Both conditions met: ret=-6% (stop loss) and SMA20 < SMA50 (reversal)
    # Stop loss appears first in the elif chain → wins
    insert_bars(db, "AAPL", [110.0] * 40 + [90.0] * 18 + [100.0, 94.0], end=ASOF)
    _insert_position(db, "AAPL", entry_price=100.0, qty=1.0, entry_date="2024-04-30")

    from trading.exits_advisor import evaluate_exit_advice
    advice = evaluate_exit_advice(asof=ASOF)

    assert advice[0]["action"] == "SELL"
    assert advice[0]["exit_signal_key"].startswith("exit_stop_loss")


def test_env_override_raises_stop_loss_threshold(db, monkeypatch):
    # Override stop loss to -10% via env; ret=-7% should NOT trigger it
    monkeypatch.setenv("TRADING_EXIT_STOP_LOSS_PCT", "10.0")
    insert_bars(db, "AAPL", [100.0, 93.0], end=ASOF)
    _insert_position(db, "AAPL", entry_price=100.0, qty=1.0, entry_date="2024-04-30")

    from trading.exits_advisor import evaluate_exit_advice
    advice = evaluate_exit_advice(asof=ASOF)

    assert not any(
        a["exit_signal_key"].startswith("exit_stop_loss") for a in advice
    )


def test_time_stop_fires(db):
    # Held 31 days, ret=+1% (< time_stop_min_ret_pct=2%) → time stop fires
    insert_bars(db, "AAPL", [100.0] * 31 + [101.0], end=ASOF)
    _insert_position(db, "AAPL", entry_price=100.0, qty=1.0, entry_date="2024-03-31")

    from trading.exits_advisor import evaluate_exit_advice
    advice = evaluate_exit_advice(asof=ASOF)

    assert advice[0]["action"] == "SELL"
    assert advice[0]["exit_signal_key"].startswith("exit_time_stop")


def test_watch_early_weakness(db):
    # entry=100, current=98 (-2%), 2 days held (< early_fail_days=5) → WATCH
    insert_bars(db, "AAPL", [100.0, 100.0, 98.0], end=ASOF)
    _insert_position(db, "AAPL", entry_price=100.0, qty=1.0, entry_date="2024-04-29")

    from trading.exits_advisor import evaluate_exit_advice
    advice = evaluate_exit_advice(asof=ASOF)

    assert advice[0]["action"] == "WATCH"
    assert "early_weakness" in advice[0]["exit_signal_key"]
