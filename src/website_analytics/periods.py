"""Extensible time periods and source-freshness rules for analytics snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal, Sequence

from website_analytics.models import DateRange


CalendarPeriodKind = Literal["day", "week", "month", "quarter", "year"]
PeriodKind = Literal["day", "week", "month", "quarter", "year", "rolling", "custom"]
SourceStatus = Literal["ok", "partial", "error"]
SnapshotStatus = Literal["complete", "preliminary", "partial"]

_KIND_LABELS: dict[PeriodKind, str] = {
    "day": "日",
    "week": "周",
    "month": "月",
    "quarter": "季度",
    "year": "年度",
    "rolling": "滚动窗口",
    "custom": "自定义",
}


@dataclass(frozen=True)
class AnalyticsPeriod:
    """One exact API aggregation range, independent of other stored periods."""

    kind: PeriodKind
    start: date
    end: date
    window_days: int | None = None

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("period end must be on or after start")
        actual_days = (self.end - self.start).days + 1
        if self.kind == "rolling":
            if self.window_days is None or self.window_days < 1:
                raise ValueError("rolling periods require a positive window_days")
            if self.window_days != actual_days:
                raise ValueError("rolling window_days must match the inclusive date range")
        elif self.window_days is not None:
            raise ValueError("window_days is only valid for rolling periods")

    @property
    def range(self) -> DateRange:
        return DateRange(start=self.start, end=self.end)

    @property
    def kind_label(self) -> str:
        return _KIND_LABELS[self.kind]

    @property
    def storage_kind(self) -> str:
        return f"rolling-{self.window_days}d" if self.kind == "rolling" else self.kind

    @property
    def label(self) -> str:
        return f"{self.start.isoformat()} 至 {self.end.isoformat()}（{self.kind_label}）"


@dataclass(frozen=True)
class SourceCoverage:
    """How far one fact source covers an intended analytics period."""

    source: str
    status: SourceStatus
    available_through: date | None


def calendar_period(kind: CalendarPeriodKind, anchor: date) -> AnalyticsPeriod:
    """Return the calendar period containing ``anchor`` using Monday-based weeks."""
    if kind == "day":
        return AnalyticsPeriod(kind=kind, start=anchor, end=anchor)
    if kind == "week":
        start = anchor - timedelta(days=anchor.weekday())
        return AnalyticsPeriod(kind=kind, start=start, end=start + timedelta(days=6))
    if kind == "month":
        start = anchor.replace(day=1)
        next_month = _shift_month(start, 1)
        return AnalyticsPeriod(kind=kind, start=start, end=next_month - timedelta(days=1))
    if kind == "quarter":
        start_month = ((anchor.month - 1) // 3) * 3 + 1
        start = anchor.replace(month=start_month, day=1)
        next_quarter = _shift_month(start, 3)
        return AnalyticsPeriod(kind=kind, start=start, end=next_quarter - timedelta(days=1))
    if kind == "year":
        return AnalyticsPeriod(
            kind=kind,
            start=anchor.replace(month=1, day=1),
            end=anchor.replace(month=12, day=31),
        )
    raise ValueError(f"unsupported calendar period kind: {kind}")


def rolling_period(window_days: int, end: date) -> AnalyticsPeriod:
    """Return an inclusive rolling window ending on ``end``."""
    if window_days < 1:
        raise ValueError("window_days must be positive")
    return AnalyticsPeriod(
        kind="rolling",
        start=end - timedelta(days=window_days - 1),
        end=end,
        window_days=window_days,
    )


def custom_period(start: date, end: date) -> AnalyticsPeriod:
    return AnalyticsPeriod(kind="custom", start=start, end=end)


def previous_analytics_period(period: AnalyticsPeriod) -> AnalyticsPeriod:
    """Return the preceding period while preserving calendar boundaries."""
    if period.kind == "month":
        return calendar_period("month", period.start - timedelta(days=1))
    if period.kind == "quarter":
        return calendar_period("quarter", period.start - timedelta(days=1))
    if period.kind == "year":
        return calendar_period("year", period.start - timedelta(days=1))
    duration = period.end - period.start
    previous_end = period.start - timedelta(days=1)
    return AnalyticsPeriod(
        kind=period.kind,
        start=previous_end - duration,
        end=previous_end,
        window_days=period.window_days,
    )


def recent_periods(
    kind: CalendarPeriodKind,
    count: int,
    anchor: date,
) -> tuple[AnalyticsPeriod, ...]:
    """Return ``count`` consecutive calendar periods in chronological order."""
    if count < 1:
        raise ValueError("count must be positive")
    periods = [calendar_period(kind, anchor)]
    while len(periods) < count:
        periods.append(previous_analytics_period(periods[-1]))
    return tuple(reversed(periods))


def period_key(site_key: str, period: AnalyticsPeriod) -> str:
    """Return a stable, collision-resistant Base upsert key."""
    if not site_key or "|" in site_key:
        raise ValueError("site_key must be non-empty and cannot contain '|'")
    return "|".join(
        (site_key, period.storage_kind, period.start.isoformat(), period.end.isoformat())
    )


def effective_range(period: AnalyticsPeriod, coverage: SourceCoverage) -> DateRange | None:
    """Return the part of ``period`` currently available from one source."""
    if coverage.status == "error" or coverage.available_through is None:
        return None
    if coverage.available_through < period.start:
        return None
    return DateRange(start=period.start, end=min(period.end, coverage.available_through))


def snapshot_status(
    period: AnalyticsPeriod,
    coverages: Sequence[SourceCoverage],
) -> SnapshotStatus:
    """Classify a snapshot without treating delayed or failed sources as zero."""
    if not coverages or any(
        coverage.status in {"partial", "error"} or coverage.available_through is None
        for coverage in coverages
    ):
        return "partial"
    if all(coverage.available_through >= period.end for coverage in coverages):
        return "complete"
    return "preliminary"


def dashboard_windows(period: AnalyticsPeriod, anchor: date) -> tuple[str, ...]:
    """Return deterministic rolling-view memberships for dashboard filtering."""
    windows: list[str] = []
    if period.start <= anchor <= period.end:
        windows.append("当前周期")
    if period.kind == "week":
        current = calendar_period("week", anchor)
        recent_4 = recent_periods("week", 4, anchor)
        recent_12 = recent_periods("week", 12, anchor)
        if period in recent_4:
            windows.append("近4周")
        if period in recent_12:
            windows.append("近12周")
        if period == current and "当前周期" not in windows:
            windows.append("当前周期")
    if period.kind == "month" and period in recent_periods("month", 12, anchor):
        windows.append("近12个月")
    return tuple(windows)


def _shift_month(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    return value.replace(year=year, month=zero_based_month + 1, day=1)
