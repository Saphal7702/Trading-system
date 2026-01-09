from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MIN_HOLDING_PERIOD = timedelta(days=1)

@dataclass(frozen=True)
class ComplianceDecision:
    allowed: bool
    reason: str

def can_sell(opened_at_iso: str, now: datetime | None = None, emergency_sell_only: bool = False) -> ComplianceDecision:
    if emergency_sell_only:
        return ComplianceDecision(True, "Emergency sell-only override enabled.")

    if now is None:
        now = datetime.now(timezone.utc)

    opened_at = datetime.fromisoformat(opened_at_iso)
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)

    if now - opened_at < MIN_HOLDING_PERIOD:
        return ComplianceDecision(False, "Blocked: minimum 1-day holding period not met.")

    return ComplianceDecision(True, "OK")
