from datetime import date

import pytest

from website_analytics.models import DateRange
from website_analytics.periods import (
    AnalyticsPeriod,
    SourceCoverage,
    calendar_period,
    custom_period,
    dashboard_windows,
    effective_range,
    period_key,
    previous_analytics_period,
    recent_periods,
    rolling_period,
    snapshot_status,
)


def test_calendar_periods_cover_day_week_month_quarter_and_year() -> None:
    anchor = date(2026, 8, 19)

    assert calendar_period("day", anchor).range == DateRange(anchor, anchor)
    assert calendar_period("week", anchor).range == DateRange(
        date(2026, 8, 17), date(2026, 8, 23)
    )
    assert calendar_period("month", anchor).range == DateRange(
        date(2026, 8, 1), date(2026, 8, 31)
    )
    assert calendar_period("quarter", anchor).range == DateRange(
        date(2026, 7, 1), date(2026, 9, 30)
    )
    assert calendar_period("year", anchor).range == DateRange(
        date(2026, 1, 1), date(2026, 12, 31)
    )


def test_rolling_and_custom_ranges_have_distinct_storage_keys() -> None:
    rolling = rolling_period(7, date(2026, 8, 16))
    custom = custom_period(date(2026, 8, 10), date(2026, 8, 16))

    assert period_key("genemedi-net", rolling) == (
        "genemedi-net|rolling-7d|2026-08-10|2026-08-16"
    )
    assert period_key("genemedi-net", custom) == (
        "genemedi-net|custom|2026-08-10|2026-08-16"
    )


def test_previous_period_preserves_kind_and_duration() -> None:
    current = rolling_period(28, date(2026, 8, 16))

    assert previous_analytics_period(current) == AnalyticsPeriod(
        kind="rolling",
        start=date(2026, 6, 22),
        end=date(2026, 7, 19),
        window_days=28,
    )


def test_recent_periods_are_chronological_and_monday_based() -> None:
    periods = recent_periods("week", 4, date(2026, 8, 19))

    assert [period.start for period in periods] == [
        date(2026, 7, 27),
        date(2026, 8, 3),
        date(2026, 8, 10),
        date(2026, 8, 17),
    ]
    assert dashboard_windows(periods[0], date(2026, 8, 19)) == ("近4周", "近12周")
    assert dashboard_windows(periods[-1], date(2026, 8, 19)) == (
        "当前周期",
        "近4周",
        "近12周",
    )


def test_recent_calendar_periods_preserve_real_boundaries() -> None:
    months = recent_periods("month", 3, date(2026, 3, 15))
    assert [(period.start, period.end) for period in months] == [
        (date(2026, 1, 1), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 28)),
        (date(2026, 3, 1), date(2026, 3, 31)),
    ]

    quarters = recent_periods("quarter", 3, date(2026, 2, 10))
    assert [(period.start, period.end) for period in quarters] == [
        (date(2025, 7, 1), date(2025, 9, 30)),
        (date(2025, 10, 1), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 3, 31)),
    ]

    years = recent_periods("year", 2, date(2026, 8, 19))
    assert [(period.start, period.end) for period in years] == [
        (date(2025, 1, 1), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 12, 31)),
    ]


def test_source_coverage_distinguishes_complete_preliminary_and_partial() -> None:
    period = calendar_period("week", date(2026, 8, 19))
    ga4 = SourceCoverage("ga4", "ok", date(2026, 8, 19))
    gsc = SourceCoverage("gsc", "ok", date(2026, 8, 16))
    inquiry = SourceCoverage("inquiry", "ok", date(2026, 8, 19))

    assert snapshot_status(period, (ga4, gsc, inquiry)) == "preliminary"
    assert effective_range(period, gsc) is None
    assert snapshot_status(
        period,
        (ga4, SourceCoverage("gsc", "error", None), inquiry),
    ) == "partial"
    complete = tuple(
        SourceCoverage(source, "ok", period.end)
        for source in ("ga4", "gsc", "inquiry")
    )
    assert snapshot_status(period, complete) == "complete"


def test_effective_range_caps_a_source_at_its_available_date() -> None:
    period = custom_period(date(2026, 8, 1), date(2026, 8, 19))

    assert effective_range(
        period, SourceCoverage("gsc", "ok", date(2026, 8, 16))
    ) == DateRange(date(2026, 8, 1), date(2026, 8, 16))
    assert effective_range(
        period, SourceCoverage("gsc", "ok", date(2026, 7, 31))
    ) is None


def test_period_validation_rejects_invalid_windows() -> None:
    with pytest.raises(ValueError, match="match"):
        AnalyticsPeriod(
            kind="rolling",
            start=date(2026, 8, 10),
            end=date(2026, 8, 16),
            window_days=28,
        )
    with pytest.raises(ValueError, match="positive"):
        recent_periods("week", 0, date(2026, 8, 19))
