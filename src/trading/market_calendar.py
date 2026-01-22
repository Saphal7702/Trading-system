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
