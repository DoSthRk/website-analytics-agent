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
    ResponseMetaData,
)

from website_analytics.models import DateRange, SiteConfig
from website_analytics.retry import DEFAULT_RETRY_POLICY, RetryPolicy, retry_transient
from website_analytics.url_safety import sanitize_url_query
from website_analytics.adapters.scope import page_regex, validate_segments


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

    exact_scope_contract = "genemedi-exact/v1"

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

    def report(self, site: SiteConfig, date_range: DateRange, dimensions: Sequence[str]) -> list[AnalyticsRow]:
        """Rows grouped by any GA4 dimensions, e.g. the crawler fingerprint report."""
        return self._run(site, date_range, tuple(dimensions))

    def aggregate(self, site: SiteConfig, date_range: DateRange) -> list[AnalyticsRow]:
        """Return one interval-level GA4 row without a grouping dimension.

        Interval unique-user metrics are only valid at this aggregate level;
        callers must not derive them by adding daily unique-user rows.
        """
        return self._run(site, date_range, ())

    def scoped_aggregate(
        self, site: SiteConfig, date_range: DateRange, paths: Sequence[str],
        *, countries: Sequence[str] = (), channels: Sequence[str] = (),
    ) -> list[AnalyticsRow]:
        """One interval aggregate for a landing-page cohort; never sum page UV.

        Optional ISO alpha-2 countries and session default channel groups are
        GA4-only conditions. Registered hostname filtering is always retained.
        """
        return self._scoped(site, date_range, paths, countries=countries, channels=channels, exact_case=False)

    def scoped_exact_aggregate(self, site, date_range, paths, *, countries=(), channels=()):
        return self._scoped(site, date_range, paths, countries=countries, channels=channels, exact_case=True)

    def _scoped(self, site, date_range, paths, *, countries, channels, exact_case):
        self.last_exact_scope=None
        validate_segments(countries, channels)
        filters = [_hostname_filter(site.domains), FilterExpression(filter=Filter(
            field_name="landingPagePlusQueryString", string_filter=Filter.StringFilter(
                match_type=Filter.StringFilter.MatchType.FULL_REGEXP,
                value=page_regex(paths, exact_case=exact_case), case_sensitive=exact_case,
            ))) ]
        for field, values in (("countryId", countries), ("sessionDefaultChannelGroup", channels)):
            if values:
                filters.append(FilterExpression(filter=Filter(field_name=field,
                    in_list_filter=Filter.InListFilter(values=list(values), case_sensitive=False))))
        result=self._run(site, date_range, (), FilterExpression(and_group=FilterExpressionList(expressions=filters)))
        if exact_case:
            self.last_exact_scope={"site":site.site_key,"property":site.ga4_property_id,"domains":sorted(site.domains),
                "start":date_range.start.isoformat(),"end":date_range.end.isoformat(),"pages":list(paths),
                "countries":list(countries),"channels":list(channels)}
        return result

    def _run(
        self,
        site: SiteConfig,
        date_range: DateRange,
        dimensions: tuple[str, ...],
        dimension_filter: FilterExpression | None = None,
    ) -> list[AnalyticsRow]:
        normalized_rows: list[AnalyticsRow] = []
        self.last_exact_scope=None
        observed_metadata=[]
        self.last_response_metadata={}
        while True:
            request = RunReportRequest(
                property=f"properties/{site.ga4_property_id}",
                dimensions=[Dimension(name=name) for name in dimensions],
                metrics=[Metric(name=name) for name in _METRICS],
                dimension_filter=dimension_filter if dimension_filter is not None else _hostname_filter(site.domains),
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
            observed_metadata.append(_response_metadata(response))
            zones=[m["timezone"] for m in observed_metadata]
            self.last_response_metadata={"timezone":zones[0] if all(z is not None and z==zones[0] for z in zones) else None,
                **{key:_observed_flag([m[key] for m in observed_metadata])
                   for key in ("truncated","thresholded","sampled","other_row_loss","provider_truncated","restricted")},
                "truncation_reasons_supported":False if any(m["truncation_reasons_supported"] is False for m in observed_metadata)
                    else True if all(m["truncation_reasons_supported"] is True for m in observed_metadata) else None}
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


def _observed_flag(values):
    if any(value is True for value in values):return True
    return False if values and all(value is False for value in values) else None


def _response_metadata(response):
    """Keep capability separate from flags; never retain raw reason strings."""
    meta=getattr(response,"metadata",None);pb=getattr(response,"_pb",None)
    if pb is not None and not pb.HasField("metadata"):meta=None
    declared=getattr(getattr(type(meta),"meta",None),"fields",None) if meta is not None else None
    if declared is None and pb is not None:declared=ResponseMetaData.meta.fields
    supported=("data_truncation_reasons" in declared) if declared is not None else hasattr(meta,"data_truncation_reasons") if meta is not None else None
    samples=getattr(meta,"sampling_metadatas",None)
    other=getattr(meta,"data_loss_from_other_row",None)
    reasons=getattr(meta,"data_truncation_reasons",None) if supported is True else None
    restrictions=getattr(meta,"schema_restriction_response",None)
    active=getattr(restrictions,"active_metric_restrictions",None)
    return {"timezone":getattr(meta,"time_zone",None) or None,
        "truncated":other,"other_row_loss":other,
        "thresholded":getattr(meta,"subject_to_thresholding",None),
        "sampled":bool(list(samples)) if samples is not None else None,
        "truncation_reasons_supported":supported,
        "provider_truncated":bool(list(reasons)) if supported is True and reasons is not None else None,
        "restricted":bool(list(active)) if active is not None else None}


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
