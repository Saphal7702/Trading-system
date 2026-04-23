from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from zoneinfo import ZoneInfo

import logging

log = logging.getLogger("trading")

NY = ZoneInfo("America/New_York")

@dataclass(frozen=True)
class MarketDay:
    date: str          # YYYY-MM-DD
    open: str | None   # ISO-ish string from Alpaca (may be time)
    close: str | None


def today_ny_str(now: datetime | None = None) -> str:
    now = now or datetime.now(tz=NY)
    return now.date().isoformat()


def is_trading_day(broker, day: str) -> tuple[bool, MarketDay | None]:
    """
    Uses Alpaca trading calendar:
      GET /v2/calendar?start=YYYY-MM-DD&end=YYYY-MM-DD
    Returns (is_open, MarketDay|None)
    """
    try:
        cal = broker.get_calendar(start=day, end=day)  # you’ll implement on broker wrapper
        if not cal:
            return (False, None)

        # alpaca-py Calendar objects have .date .open .close (strings)
        c0 = cal[0]
        md = MarketDay(
            date=str(getattr(c0, "date", day)),
            open=str(getattr(c0, "open", None)),
            close=str(getattr(c0, "close", None)),
        )
        return (True, md)
    except Exception as e:
        # fail-safe behavior: if calendar call fails, do NOT trade unless explicitly overridden
        log.exception("Calendar check failed for day=%s: %s", day, e)
        return (False, None)


def last_completed_trading_day(broker, *, reference: date | None = None) -> date:
    """
    Returns the most recent fully completed US trading day,
    using Alpaca's calendar as the authoritative source.

    Handles weekends, holidays, and early closes correctly.
    Falls back to up to 7 days lookback (covers long weekends).
    """
    from datetime import timedelta
    ref = reference or datetime.now(tz=NY).date()

    # Look back up to 7 days to cover holidays + weekends
    start = (ref - timedelta(days=7)).isoformat()
    end = (ref - timedelta(days=1)).isoformat()  # never include today

    try:
        cal = broker.get_calendar(start=start, end=end)
        if not cal:
            raise RuntimeError("Empty calendar response")

        # Alpaca returns days in ascending order — take the last one
        last = cal[-1]
        return date.fromisoformat(str(getattr(last, "date", end)))

    except Exception as e:
        log.warning("last_completed_trading_day fallback: %s", e)
        # Safe fallback: walk back from yesterday
        d = ref - timedelta(days=1)
        while d.weekday() >= 5:  # 5=Sat, 6=Sun
            d -= timedelta(days=1)
        return d