from __future__ import annotations

from datetime import date, timedelta

from website_analytics.models import DateRange


class DateRangeError(ValueError):
    """Raised when a requested date range is invalid."""


def parse_date_range(start: str, end: str) -> DateRange:
    """Parse an inclusive ISO date range."""
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except (TypeError, ValueError) as error:
        raise DateRangeError("start and end must be ISO dates") from error

    if end_date < start_date:
        raise DateRangeError("end date must be on or after start date")
    return DateRange(start=start_date, end=end_date)


def previous_period(date_range: DateRange) -> DateRange:
    """Return the equal-length date range immediately before ``date_range``."""
    length = date_range.end - date_range.start
    previous_end = date_range.start - timedelta(days=1)
    return DateRange(start=previous_end - length, end=previous_end)
