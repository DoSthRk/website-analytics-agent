"""GA4 reporting adapter with injected client dependencies only."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Callable, Protocol

from google.analytics.data_v1beta.types import (
    DateRange as GA4DateRange,
    Dimension,
    Filter,
    FilterExpression,
    FilterExpressionList,
    Metric,
    Row,
    RunReportRequest,
    RunReportResponse,
)

from website_analytics.models import DateRange, SiteConfig
from website_analytics.retry import DEFAULT_RETRY_POLICY, RetryPolicy, retry_transient
from website_analytics.url_safety import sanitize_url_query


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
_PAGE_SIZE = 250_000

AnalyticsRow = dict[str, str | float]


class _AnalyticsDataClient(Protocol):
    def run_report(self, request: RunReportRequest) -> RunReportResponse:
        """Run a single GA4 report request."""


class GA4Adapter:
    """Read GA4 reports through an injected Analytics Data API client."""

    def __init__(
        self,
        client: _AnalyticsDataClient,
        *,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._retry_policy = retry_policy
        self._sleep = sleep

    def daily(self, site: SiteConfig, date_range: DateRange) -> list[AnalyticsRow]:
        """Return daily GA4 metrics for a registered site and inclusive date range."""
        return self._run(site, date_range, _DAILY_DIMENSIONS)

    def pages(self, site: SiteConfig, date_range: DateRange) -> list[AnalyticsRow]:
        """Return GA4 metrics grouped by landing page and query string."""
        return self._run(site, date_range, _PAGE_DIMENSIONS)

    def aggregate(self, site: SiteConfig, date_range: DateRange) -> list[AnalyticsRow]:
        """Return one interval-level GA4 row without a grouping dimension.

        Interval unique-user metrics are only valid at this aggregate level;
        callers must not derive them by adding daily unique-user rows.
        """
        return self._run(site, date_range, ())

    def _run(
        self,
        site: SiteConfig,
        date_range: DateRange,
        dimensions: tuple[str, ...],
    ) -> list[AnalyticsRow]:
        normalized_rows: list[AnalyticsRow] = []
        while True:
            request = RunReportRequest(
                property=f"properties/{site.ga4_property_id}",
                dimensions=[Dimension(name=name) for name in dimensions],
                metrics=[Metric(name=name) for name in _METRICS],
                dimension_filter=_hostname_filter(site.domains),
                date_ranges=[
                    GA4DateRange(
                        start_date=date_range.start.isoformat(),
                        end_date=date_range.end.isoformat(),
                    )
                ],
                limit=_PAGE_SIZE,
                offset=len(normalized_rows),
            )
            response = retry_transient(
                lambda: self._client.run_report(request),
                policy=self._retry_policy,
                sleep=self._sleep,
            )
            page_rows = list(response.rows)
            normalized_rows.extend(
                _normalize_row(row, dimensions) for row in page_rows
            )

            row_count = _row_count(response)
            if row_count is not None and len(normalized_rows) >= row_count:
                return normalized_rows
            if not page_rows:
                if row_count is None:
                    return normalized_rows
                raise ValueError("GA4 response ended before its reported row_count")


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


def _row_count(response: RunReportResponse) -> int | None:
    value = getattr(response, "row_count", None)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("GA4 response row_count must be a non-negative integer")
    return value


def _normalize_dimension_value(dimension: str, value: str) -> str:
    if dimension == "date" and len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return sanitize_url_query(value)


def _hostname_filter(domains: Sequence[str]) -> FilterExpression:
    """Restrict every GA4 report to the registered website hostnames."""
    expressions = [
        FilterExpression(
            filter=Filter(
                field_name="hostName",
                string_filter=Filter.StringFilter(
                    match_type=Filter.StringFilter.MatchType.EXACT,
                    value=domain.casefold().rstrip("."),
                    case_sensitive=False,
                ),
            )
        )
        for domain in domains
    ]
    return FilterExpression(
        or_group=FilterExpressionList(expressions=expressions)
    )
