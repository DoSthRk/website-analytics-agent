from datetime import date

import pytest

from website_analytics.dates import DateRangeError, parse_date_range, previous_period
from website_analytics.models import DateRange


def test_previous_period_has_the_same_length_and_ends_before_current_range() -> None:
    current_range = parse_date_range("2026-08-03", "2026-08-09")

    assert previous_period(current_range) == DateRange(
        start=date(2026, 7, 27),
        end=date(2026, 8, 2),
    )


def test_parse_date_range_rejects_an_end_before_start() -> None:
    with pytest.raises(DateRangeError, match="on or after"):
        parse_date_range("2026-08-09", "2026-08-03")


def test_previous_period_rejects_a_range_without_an_earlier_period() -> None:
    date_range = parse_date_range("0001-01-01", "0001-01-01")

    with pytest.raises(DateRangeError, match="no available previous period"):
        previous_period(date_range)
