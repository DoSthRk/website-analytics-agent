"""GA4 reporting adapter with injected client dependencies only."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from google.analytics.data_v1beta.types import (
    DateRange as GA4DateRange,
    Dimension,
    Metric,
    Row,
    RunReportRequest,
    RunReportResponse,
)

from website_analytics.models import DateRange, SiteConfig


_METRICS = (
    "sessions",
    "totalUsers",
    "activeUsers",
    "engagedSessions",
    "engagementRate",
    "screenPageViews",
    "keyEvents",
)

_DAILY_DIMENSIONS = ("date",)
_PAGE_DIMENSIONS = ("landingPagePlusQueryString",)

AnalyticsRow = dict[str, str | float]


class _AnalyticsDataClient(Protocol):
    def run_report(self, request: RunReportRequest) -> RunReportResponse:
        """Run a single GA4 report request."""


class GA4Adapter:
    """Read GA4 reports through an injected Analytics Data API client."""

    def __init__(self, client: _AnalyticsDataClient) -> None:
        self._client = client

    def daily(self, site: SiteConfig, date_range: DateRange) -> list[AnalyticsRow]:
        """Return daily GA4 metrics for a registered site and inclusive date range."""
        return self._run(site, date_range, _DAILY_DIMENSIONS)

    def pages(self, site: SiteConfig, date_range: DateRange) -> list[AnalyticsRow]:
        """Return GA4 metrics grouped by landing page and query string."""
        return self._run(site, date_range, _PAGE_DIMENSIONS)

    def _run(
        self,
        site: SiteConfig,
        date_range: DateRange,
        dimensions: tuple[str, ...],
    ) -> list[AnalyticsRow]:
        request = RunReportRequest(
            property=f"properties/{site.ga4_property_id}",
            dimensions=[Dimension(name=name) for name in dimensions],
            metrics=[Metric(name=name) for name in _METRICS],
            date_ranges=[
                GA4DateRange(
                    start_date=date_range.start.isoformat(),
                    end_date=date_range.end.isoformat(),
                )
            ],
        )
        response = self._client.run_report(request)
        return [
            _normalize_row(row, dimensions)
            for row in response.rows
        ]


def _normalize_row(row: Row, dimensions: Sequence[str]) -> AnalyticsRow:
    dimension_values = row.dimension_values
    metric_values = row.metric_values
    normalized: AnalyticsRow = {
        dimension: _normalize_dimension_value(dimension, value.value)
        for dimension, value in zip(dimensions, dimension_values, strict=True)
    }
    normalized.update(
        {
            metric: float(value.value)
            for metric, value in zip(_METRICS, metric_values, strict=True)
        }
    )
    return normalized


def _normalize_dimension_value(dimension: str, value: str) -> str:
    if dimension == "date" and len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value
