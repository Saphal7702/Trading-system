from __future__ import annotations

from types import SimpleNamespace

ENV = "test"


class _MockBroker:
    def __init__(self, equity: float = 10_000.0):
        self._equity = equity

    def get_account(self) -> SimpleNamespace:
        return SimpleNamespace(
            equity=self._equity,
            buying_power=self._equity,
            cash=0.0,
            long_market_value=0.0,
        )


def _insert_snapshot(date_str: str, equity: float) -> None:
    from trading.db import connect
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO account_snapshots_daily(asof_date, cash, equity, buying_power)"
            " VALUES (?,0.0,?,?);",
            (date_str, equity, equity),
        )


def _insert_risk_daily(env: str, date_str: str, drawdown_pct: float) -> None:
    from trading.db import connect
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO risk_daily"
            "(env, date, equity, peak_equity, drawdown_pct, state, buys_blocked, sells_blocked, broker_blocked)"
            " VALUES (?,?,10000.0,10000.0,?,'NORMAL',0,0,0);",
            (env, date_str, drawdown_pct),
        )


# ---------------------------------------------------------------------------
# _dd_to_state — pure threshold mapping
# ---------------------------------------------------------------------------

def test_dd_to_state_all_levels(db):
    from trading.risk.circuit_breaker import _dd_to_state
    p, s, h = 0.05, 0.08, 0.12

    assert _dd_to_state(dd=0.03, pause_th=p, sell_th=s, halt_th=h) == "NORMAL"
    assert _dd_to_state(dd=0.05, pause_th=p, sell_th=s, halt_th=h) == "PAUSE_BUYS"
    assert _dd_to_state(dd=0.07, pause_th=p, sell_th=s, halt_th=h) == "PAUSE_BUYS"
    assert _dd_to_state(dd=0.08, pause_th=p, sell_th=s, halt_th=h) == "SELL_ONLY"
    assert _dd_to_state(dd=0.11, pause_th=p, sell_th=s, halt_th=h) == "SELL_ONLY"
    assert _dd_to_state(dd=0.12, pause_th=p, sell_th=s, halt_th=h) == "HALT_ALL"
    assert _dd_to_state(dd=0.20, pause_th=p, sell_th=s, halt_th=h) == "HALT_ALL"


# ---------------------------------------------------------------------------
# compute_peak_and_dd
# ---------------------------------------------------------------------------

def test_compute_peak_dd_no_snapshots(db):
    from trading.risk.circuit_breaker import compute_peak_and_dd
    peak, dd = compute_peak_and_dd(env=ENV, equity=None)
    assert peak == 0.0
    assert dd == 0.0


def test_compute_peak_dd_new_high_no_drawdown(db):
    # Current equity above historical peak → dd=0
    _insert_snapshot("2024-04-01", 10_000.0)
    from trading.risk.circuit_breaker import compute_peak_and_dd
    peak, dd = compute_peak_and_dd(env=ENV, equity=11_000.0)
    assert peak == 11_000.0
    assert dd == 0.0


def test_compute_peak_dd_with_drawdown(db):
    _insert_snapshot("2024-04-01", 10_000.0)
    from trading.risk.circuit_breaker import compute_peak_and_dd
    peak, dd = compute_peak_and_dd(env=ENV, equity=9_200.0)
    assert peak == 10_000.0
    assert abs(dd - 0.08) < 1e-9


# ---------------------------------------------------------------------------
# _consecutive_dd_streak
# ---------------------------------------------------------------------------

def test_streak_zero_when_no_history(db):
    from trading.risk.circuit_breaker import _consecutive_dd_streak
    from trading.db import connect
    with connect() as conn:
        streak = _consecutive_dd_streak(conn=conn, env=ENV, threshold=0.05, op="ge")
    assert streak == 0


def test_streak_counts_consecutive_days(db):
    _insert_risk_daily(ENV, "2024-04-29", 0.06)
    _insert_risk_daily(ENV, "2024-04-30", 0.06)
    _insert_risk_daily(ENV, "2024-05-01", 0.06)
    from trading.risk.circuit_breaker import _consecutive_dd_streak
    from trading.db import connect
    with connect() as conn:
        streak = _consecutive_dd_streak(conn=conn, env=ENV, threshold=0.05, op="ge")
    assert streak == 3


def test_streak_stops_at_gap(db):
    _insert_risk_daily(ENV, "2024-04-28", 0.06)  # older, outside streak
    _insert_risk_daily(ENV, "2024-04-29", 0.02)  # gap: dd below threshold
    _insert_risk_daily(ENV, "2024-04-30", 0.06)
    _insert_risk_daily(ENV, "2024-05-01", 0.06)
    from trading.risk.circuit_breaker import _consecutive_dd_streak
    from trading.db import connect
    with connect() as conn:
        streak = _consecutive_dd_streak(conn=conn, env=ENV, threshold=0.05, op="ge")
    assert streak == 2


# ---------------------------------------------------------------------------
# evaluate_and_apply — state machine transitions
# ---------------------------------------------------------------------------

def test_evaluate_stays_normal_with_no_drawdown(db):
    from trading.risk.circuit_breaker import evaluate_and_apply
    result = evaluate_and_apply(env=ENV, broker=_MockBroker(10_000.0), asof="2024-05-01")
    assert result["state"] == "NORMAL"
    assert result["allow_buys"] == 1
    assert result["allow_sells"] == 1


def test_evaluate_escalates_to_pause_buys(db):
    # dd=6% ≥ pause_th=5%; pause_streak_days defaults to 1 → immediate escalation
    _insert_snapshot("2024-04-01", 10_000.0)
    from trading.risk.circuit_breaker import evaluate_and_apply
    result = evaluate_and_apply(env=ENV, broker=_MockBroker(9_400.0), asof="2024-05-01")
    assert result["state"] == "PAUSE_BUYS"
    assert result["allow_buys"] == 0
    assert result["allow_sells"] == 1


def test_evaluate_escalates_to_sell_only(db, monkeypatch):
    monkeypatch.setenv("TRADING_DD_SELL_STREAK_DAYS", "1")
    _insert_snapshot("2024-04-01", 10_000.0)
    from trading.risk.circuit_breaker import evaluate_and_apply
    result = evaluate_and_apply(env=ENV, broker=_MockBroker(9_100.0), asof="2024-05-01")
    assert result["state"] == "SELL_ONLY"


def test_evaluate_escalates_to_halt_all(db, monkeypatch):
    monkeypatch.setenv("TRADING_DD_HALT_STREAK_DAYS", "1")
    _insert_snapshot("2024-04-01", 10_000.0)
    from trading.risk.circuit_breaker import evaluate_and_apply
    result = evaluate_and_apply(env=ENV, broker=_MockBroker(8_700.0), asof="2024-05-01")
    assert result["state"] == "HALT_ALL"
    assert result["allow_broker"] == 0


def test_operator_override_blocks_system_change(db):
    # Operator forces SELL_ONLY; circuit breaker must not override it
    from trading.risk.state import seed_risk_defaults, set_operator_override
    seed_risk_defaults(ENV)  # ensure row exists before UPDATE-based override
    set_operator_override(env=ENV, state="SELL_ONLY", reason="manual test")

    from trading.risk.circuit_breaker import evaluate_and_apply
    result = evaluate_and_apply(env=ENV, broker=_MockBroker(10_000.0), asof="2024-05-01")
    assert result["state"] == "SELL_ONLY"
    assert result["set_by"] == "operator"


def test_hysteresis_blocks_deescalation(db):
    # After escalating to PAUSE_BUYS, a single day of recovery does not de-escalate.
    # Default clear_streak_days=3 and min_hold_minutes=240 both prevent instant recovery.
    _insert_snapshot("2024-04-01", 10_000.0)
    from trading.risk.circuit_breaker import evaluate_and_apply
    # Trigger PAUSE_BUYS
    evaluate_and_apply(env=ENV, broker=_MockBroker(9_400.0), asof="2024-05-01")
    # dd=1% — below reset_th=3%, but clear_streak=1 < 3 and state was just set
    result = evaluate_and_apply(env=ENV, broker=_MockBroker(9_900.0), asof="2024-05-01")
    assert result["state"] == "PAUSE_BUYS"
