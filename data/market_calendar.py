"""NYSE trading calendar.

Everything in the system that says "trading day" or "next session" goes
through here, so holidays and early closes are handled in one place.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from functools import lru_cache

import pandas as pd


class CalendarError(ValueError):
    """The date is outside the range the calendar knows about."""


@lru_cache(maxsize=1)
def _calendar():
    import exchange_calendars

    return exchange_calendars.get_calendar("XNYS")


def _checked(day: date) -> pd.Timestamp:
    stamp = pd.Timestamp(day)
    calendar = _calendar()
    if not calendar.first_session <= stamp <= calendar.last_session:
        raise CalendarError(
            f"{day} is outside the trading calendar "
            f"({calendar.first_session.date()} to {calendar.last_session.date()})."
        )
    return stamp


def is_trading_day(day: date) -> bool:
    return bool(_calendar().is_session(_checked(day)))


def is_early_close(day: date) -> bool:
    """True on sessions that end at 13:00 New York time instead of 16:00."""
    return is_trading_day(day) and pd.Timestamp(day) in _calendar().early_closes


def session_close(day: date) -> datetime:
    """When the session on `day` ends, as a UTC datetime."""
    if not is_trading_day(day):
        raise CalendarError(f"{day} is not a trading day.")
    return _calendar().session_close(pd.Timestamp(day)).to_pydatetime()


def previous_trading_day(day: date) -> date:
    """The last trading day strictly before `day`."""
    stamp = _checked(day) - pd.Timedelta(days=1)
    return _calendar().date_to_session(_checked(stamp.date()), direction="previous").date()


def next_trading_day(day: date) -> date:
    """The first trading day strictly after `day`."""
    stamp = _checked(day) + pd.Timedelta(days=1)
    return _calendar().date_to_session(_checked(stamp.date()), direction="next").date()


def last_completed_session(now: datetime | None = None) -> date:
    """The most recent session that has already closed.

    While the market is open this is the previous session: today's bar is
    still changing and must not be treated as final.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    today_in_new_york = pd.Timestamp(now).tz_convert("America/New_York").date()
    candidate = _calendar().date_to_session(_checked(today_in_new_york), direction="previous").date()
    if session_close(candidate) > now:
        candidate = previous_trading_day(candidate)
    return candidate
