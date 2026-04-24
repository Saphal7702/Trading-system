from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

from tests.conftest import insert_bars


def _today() -> str:
    return date.today().isoformat()


def _make_mock_broker(buying_power: float = 10_000.0, equity: float = 10_000.0):
    class _Broker:
        def get_account(self) -> SimpleNamespace:
            return SimpleNamespace(buying_power=buying_power, equity=equity, cash=0.0)
    return _Broker()


def _patch_broker(monkeypatch, **kwargs) -> None:
    import trading.cli as cli
    import trading.broker.sync as sync
    monkeypatch.setattr(cli, "_make_broker", lambda: _make_mock_broker(**kwargs))
    monkeypatch.setattr(sync, "upsert_account", lambda broker: None)


def _patch_market_open(monkeypatch, *, open: bool = True) -> None:
    import trading.preflight as pf
    monkeypatch.setattr(pf, "is_trading_day", lambda broker, day: (open, None))


# ---------------------------------------------------------------------------
# exec_blockers
# ---------------------------------------------------------------------------

def test_exec_blocked_by_paper_gate(db, monkeypatch):
    # Explicitly disable paper orders to test the blocker (the .env default is true)
    monkeypatch.setenv("TRADING_ALLOW_PAPER_ORDERS", "false")
    insert_bars(db, "SPY", [100.0], end=_today())
    _patch_broker(monkeypatch)
    _patch_market_open(monkeypatch)

    from trading.preflight import run_preflight
    result = run_preflight()

    assert any("paper_gated" in b for b in result.exec_blockers)
    assert result.exec_safe is False


def test_exec_blocked_by_daily_cap_reached(db, monkeypatch):
    insert_bars(db, "SPY", [100.0], end=_today())
    _patch_broker(monkeypatch)
    _patch_market_open(monkeypatch)
    monkeypatch.setenv("TRADING_ALLOW_PAPER_ORDERS", "true")
    monkeypatch.setenv("TRADING_DAILY_TRADE_CAP", "0")

    from trading.preflight import run_preflight
    result = run_preflight()

    assert any("daily_trade_cap_reached" in b for b in result.exec_blockers)


def test_exec_blocked_by_open_orders(db, monkeypatch):
    insert_bars(db, "SPY", [100.0], end=_today())
    _patch_broker(monkeypatch)
    _patch_market_open(monkeypatch)
    monkeypatch.setenv("TRADING_ALLOW_PAPER_ORDERS", "true")

    from trading.db import connect
    with connect() as conn:
        conn.execute(
            "INSERT INTO orders(symbol, side, qty, status) VALUES ('AAPL','buy',1.0,'submitted');"
        )

    from trading.preflight import run_preflight
    result = run_preflight()

    assert any("open_internal_orders" in b for b in result.exec_blockers)


def test_exec_blocked_by_insufficient_buying_power(db, monkeypatch):
    insert_bars(db, "SPY", [100.0], end=_today())
    _patch_broker(monkeypatch, buying_power=10.0)  # far below per_position
    _patch_market_open(monkeypatch)
    monkeypatch.setenv("TRADING_ALLOW_PAPER_ORDERS", "true")
    monkeypatch.setenv("TRADING_PER_POSITION", "500.0")

    from trading.preflight import run_preflight
    result = run_preflight()

    assert any("insufficient_buying_power" in b for b in result.exec_blockers)


# ---------------------------------------------------------------------------
# ok flag (data freshness + market calendar)
# ---------------------------------------------------------------------------

def test_ok_false_when_data_stale(db, monkeypatch):
    stale_date = (date.today() - timedelta(days=5)).isoformat()
    insert_bars(db, "SPY", [100.0], end=stale_date)
    _patch_broker(monkeypatch)
    _patch_market_open(monkeypatch)
    monkeypatch.setenv("TRADING_MAX_BAR_STALENESS_DAYS", "2")

    from trading.preflight import run_preflight
    result = run_preflight()

    assert result.ok is False
    assert result.staleness_days >= 5


def test_ok_false_when_market_closed(db, monkeypatch):
    insert_bars(db, "SPY", [100.0], end=_today())
    _patch_broker(monkeypatch)
    _patch_market_open(monkeypatch, open=False)

    from trading.preflight import run_preflight
    result = run_preflight()

    assert result.ok is False
    assert result.market_open_day is False
