from __future__ import annotations
from datetime import datetime, timedelta, timezone
from trading.compliance import can_sell

def _now() -> datetime:
    return datetime(2024, 5, 1, 12, 0, 0, tzinfo=timezone.utc)

def test_blocks_sell_within_24h():
    opened = (_now() - timedelta(hours=12)).isoformat()
    result = can_sell(opened, now=_now())
    assert not result.allowed
    assert "holding period" in result.reason

def test_allows_sell_after_24h():
    opened = (_now() - timedelta(hours=25)).isoformat()
    result = can_sell(opened, now=_now())
    assert result.allowed
    assert result.reason == "OK"

def test_allows_sell_exactly_at_boundary():
    opened = (_now() - timedelta(days=1)).isoformat()
    result = can_sell(opened, now=_now())
    assert result.allowed

def test_emergency_override_bypasses_hold():
    opened = _now().isoformat()  # just opened
    result = can_sell(opened, now=_now(), emergency_sell_only=True)
    assert result.allowed
    assert "Emergency" in result.reason

def test_naive_opened_at_treated_as_utc():
    # No timezone info — should be treated as UTC
    opened = (_now() - timedelta(hours=12)).replace(tzinfo=None).isoformat()
    result = can_sell(opened, now=_now())
    assert not result.allowed