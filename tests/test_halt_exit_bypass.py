from __future__ import annotations

from types import SimpleNamespace

ENV = "test"


class _FakeBrokerOrder:
    def __init__(self, id_: str, symbol: str, side: str, qty: float | None):
        self.id = id_
        self.symbol = symbol
        self.side = side
        self.qty = qty

    def model_dump(self):
        return {"id": self.id, "symbol": self.symbol, "side": self.side, "qty": self.qty}


class _MockHaltBroker:
    """Records every order placed so tests can assert buys never happen."""

    def __init__(self, equity: float = 8_700.0):
        self._equity = equity
        self.orders_placed: list[tuple[str, str, float | None, float | None]] = []

    def get_account(self) -> SimpleNamespace:
        return SimpleNamespace(equity=self._equity, buying_power=self._equity, cash=0.0, long_market_value=0.0)

    def place_market_order(self, symbol: str, side: str, *, qty: float | None = None, notional: float | None = None):
        self.orders_placed.append((symbol, side, qty, notional))
        return _FakeBrokerOrder(id_=f"bo-{len(self.orders_placed)}", symbol=symbol, side=side, qty=qty)


def _start_run(asof: str) -> int:
    from trading.runloop import start_run
    from trading.db import connect
    run_id = start_run(notes="test")
    with connect() as conn:
        conn.execute("UPDATE runs SET asof_date=? WHERE id=?;", (asof, run_id))
    return run_id


def _insert_intent(run_id: int, *, symbol: str, action: str, signal_key: str, reason: str = "test") -> int:
    from trading.db import connect
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO intents(run_id, symbol, action, reason, signal_key) VALUES (?,?,?,?,?);",
            (run_id, symbol, action, reason, signal_key),
        )
        return int(cur.lastrowid)


def _insert_position(symbol: str, qty: float) -> None:
    from trading.db import connect
    with connect() as conn:
        conn.execute(
            "INSERT INTO positions(symbol, qty, avg_entry_price, opened_at) VALUES (?,?,?,datetime('now'));",
            (symbol, qty, 10.0),
        )


def _insert_order(run_id: int, *, symbol: str, side: str, status: str, intent_id: int, idem: str) -> int:
    from trading.db import connect
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO orders(run_id, symbol, side, qty, status, reason, idempotency_key, intent_id)
            VALUES (?,?,?,1.0,?, 'test', ?, ?);
            """,
            (run_id, symbol, side, status, idem, intent_id),
        )
        return int(cur.lastrowid)


def _enable_paper_submits(monkeypatch) -> None:
    monkeypatch.setenv("TRADING_ENV", "paper")
    monkeypatch.setenv("TRADING_ALLOW_PAPER_ORDERS", "true")


# ---------------------------------------------------------------------------
# Flag reader
# ---------------------------------------------------------------------------

def test_bypass_flag_defaults_off(db, monkeypatch):
    monkeypatch.delenv("TRADING_HALT_ALLOW_STOP_LOSS_EXITS", raising=False)
    from trading.risk.state import halt_stop_loss_bypass_enabled
    assert halt_stop_loss_bypass_enabled() is False


def test_bypass_flag_on_via_env(monkeypatch):
    monkeypatch.setenv("TRADING_HALT_ALLOW_STOP_LOSS_EXITS", "true")
    from trading.risk.state import halt_stop_loss_bypass_enabled
    assert halt_stop_loss_bypass_enabled() is True


# ---------------------------------------------------------------------------
# is_risk_reducing_exit_intent — pure predicate
# ---------------------------------------------------------------------------

def test_predicate_accepts_exit_sells():
    from trading.exits_advisor import is_risk_reducing_exit_intent
    assert is_risk_reducing_exit_intent(action="sell", signal_key="exit_stop_loss_15pct") is True
    assert is_risk_reducing_exit_intent(action="sell", signal_key="exit_trailing_dd4_peak8") is True
    assert is_risk_reducing_exit_intent(action="sell", signal_key="exit_time_stop_30d") is True
    assert is_risk_reducing_exit_intent(action="SELL", signal_key="EXIT_STOP_LOSS_15PCT") is True


def test_predicate_rejects_non_exit_or_non_sell():
    from trading.exits_advisor import is_risk_reducing_exit_intent
    assert is_risk_reducing_exit_intent(action="buy", signal_key="exit_stop_loss_15pct") is False
    assert is_risk_reducing_exit_intent(action="sell", signal_key="portfolio_trim") is False
    assert is_risk_reducing_exit_intent(action="sell", signal_key=None) is False
    assert is_risk_reducing_exit_intent(action="sell", signal_key="") is False
    assert is_risk_reducing_exit_intent(action="hold", signal_key="exit_stop_loss_15pct") is False


# ---------------------------------------------------------------------------
# find_pending_halt_bypass_intents — DB query
# ---------------------------------------------------------------------------

def test_find_pending_halt_bypass_intents_filters_correctly(db):
    from trading.db import connect
    from trading.exits_advisor import find_pending_halt_bypass_intents

    run_id = _start_run("2024-05-01")

    eligible_id = _insert_intent(run_id, symbol="AAPL", action="sell", signal_key="exit_stop_loss_15pct")
    _insert_intent(run_id, symbol="MSFT", action="sell", signal_key="portfolio_trim")  # not exit_* -> excluded
    _insert_intent(run_id, symbol="NVDA", action="buy", signal_key="exit_stop_loss_15pct")  # buy -> excluded

    already_filled_id = _insert_intent(run_id, symbol="GOOG", action="sell", signal_key="exit_trailing_dd4_peak8")
    _insert_order(run_id, symbol="GOOG", side="sell", status="submitted", intent_id=already_filled_id, idem="idem-goog")

    retry_id = _insert_intent(run_id, symbol="TSLA", action="sell", signal_key="exit_time_stop_30d")
    _insert_order(run_id, symbol="TSLA", side="sell", status="failed", intent_id=retry_id, idem="idem-tsla")

    with connect() as conn:
        eligible = find_pending_halt_bypass_intents(conn)

    symbols = sorted(r["symbol"] for r in eligible)
    assert symbols == ["AAPL", "TSLA"]
    ids = {r["intent_id"] for r in eligible}
    assert ids == {eligible_id, retry_id}


# ---------------------------------------------------------------------------
# _run_halt_all_exit_bypass — end-to-end (minus run_once's outer plumbing)
# ---------------------------------------------------------------------------

def test_bypass_submits_only_eligible_exit_and_records_audit_event(db, monkeypatch):
    import trading.execution as execution_mod
    from trading.db import connect
    from trading.runloop import _run_halt_all_exit_bypass

    _enable_paper_submits(monkeypatch)

    run_id = _start_run("2024-05-01")

    # Eligible risk-reducing exit intent with an open position to sell.
    exit_intent_id = _insert_intent(run_id, symbol="AAPL", action="sell", signal_key="exit_stop_loss_15pct", reason="Hard stop: ret=-16%")
    _insert_position("AAPL", qty=10.0)

    # A pre-existing, unrelated pending BUY order (simulating queued work from before HALT_ALL).
    buy_intent_id = _insert_intent(run_id, symbol="MSFT", action="buy", signal_key="portfolio_new_entry")
    buy_order_id = _insert_order(run_id, symbol="MSFT", side="buy", status="created", intent_id=buy_intent_id, idem="idem-msft-buy")

    mock_broker = _MockHaltBroker(equity=8_700.0)
    monkeypatch.setattr(execution_mod, "make_broker", lambda: mock_broker)

    result = _run_halt_all_exit_bypass(
        run_id=run_id,
        env=ENV,
        asof="2024-05-01",
        summary={"steps": {}},
        broker=mock_broker,
        risk_state={"state": "HALT_ALL", "reason": "dd=0.13"},
    )

    assert result.status == "partial_halt_exit_bypass"

    # Only the eligible exit sell was ever sent to the broker.
    assert mock_broker.orders_placed == [("AAPL", "sell", 10.0, None)]

    with connect() as conn:
        aapl_order = conn.execute(
            "SELECT status, broker_order_id FROM orders WHERE intent_id=?;", (exit_intent_id,)
        ).fetchone()
        msft_order = conn.execute(
            "SELECT status FROM orders WHERE id=?;", (buy_order_id,)
        ).fetchone()
        events = conn.execute(
            "SELECT event_type, metrics_json FROM risk_events WHERE env=? AND event_type='HALT_ALL_EXIT_BYPASS';",
            (ENV,),
        ).fetchall()

    assert aapl_order["status"] == "submitted"
    assert aapl_order["broker_order_id"] == "bo-1"

    # The pre-existing buy order was never touched by the bypass.
    assert msft_order["status"] == "created"

    assert len(events) == 1
    assert "AAPL" in events[0]["metrics_json"]
    assert "exit_stop_loss_15pct" in events[0]["metrics_json"]


def test_bypass_does_not_resubmit_already_open_order(db, monkeypatch):
    import trading.execution as execution_mod
    from trading.db import connect
    from trading.runloop import _run_halt_all_exit_bypass

    _enable_paper_submits(monkeypatch)

    run_id = _start_run("2024-05-01")
    exit_intent_id = _insert_intent(run_id, symbol="AAPL", action="sell", signal_key="exit_stop_loss_15pct")
    _insert_position("AAPL", qty=10.0)
    _insert_order(run_id, symbol="AAPL", side="sell", status="submitted", intent_id=exit_intent_id, idem="idem-aapl-existing")

    mock_broker = _MockHaltBroker()
    monkeypatch.setattr(execution_mod, "make_broker", lambda: mock_broker)

    result = _run_halt_all_exit_bypass(
        run_id=run_id,
        env=ENV,
        asof="2024-05-01",
        summary={"steps": {}},
        broker=mock_broker,
        risk_state={"state": "HALT_ALL", "reason": "dd=0.13"},
    )

    assert result.status == "partial_halt_exit_bypass"
    assert mock_broker.orders_placed == []  # idempotency preserved: nothing resubmitted

    with connect() as conn:
        events = conn.execute(
            "SELECT COUNT(*) AS c FROM risk_events WHERE env=? AND event_type='HALT_ALL_EXIT_BYPASS';",
            (ENV,),
        ).fetchone()
    assert events["c"] == 0


def test_bypass_no_eligible_intents_is_a_noop(db, monkeypatch):
    import trading.execution as execution_mod
    from trading.db import connect
    from trading.runloop import _run_halt_all_exit_bypass

    _enable_paper_submits(monkeypatch)

    run_id = _start_run("2024-05-01")
    # Only a non-exit sell and a buy pending -- neither qualifies.
    _insert_intent(run_id, symbol="MSFT", action="sell", signal_key="portfolio_trim")
    _insert_intent(run_id, symbol="NVDA", action="buy", signal_key="portfolio_new_entry")

    mock_broker = _MockHaltBroker()
    monkeypatch.setattr(execution_mod, "make_broker", lambda: mock_broker)

    result = _run_halt_all_exit_bypass(
        run_id=run_id,
        env=ENV,
        asof="2024-05-01",
        summary={"steps": {}},
        broker=mock_broker,
        risk_state={"state": "HALT_ALL", "reason": "dd=0.13"},
    )

    assert result.status == "partial_halt_exit_bypass"
    assert mock_broker.orders_placed == []

    with connect() as conn:
        events = conn.execute(
            "SELECT COUNT(*) AS c FROM risk_events WHERE env=? AND event_type='HALT_ALL_EXIT_BYPASS';",
            (ENV,),
        ).fetchone()
    assert events["c"] == 0
