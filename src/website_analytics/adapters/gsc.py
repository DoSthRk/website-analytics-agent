"""Google Search Console adapter with bounded detailed-report pagination."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Callable, Literal, Protocol, TypedDict
from datetime import date

from website_analytics.models import DateRange, SiteConfig
from website_analytics.retry import DEFAULT_RETRY_POLICY, RetryPolicy, retry_transient
from website_analytics.url_safety import sanitize_url_query
from website_analytics.adapters.scope import page_regex


GSCDimension = Literal["date", "page", "query", "country", "device"]
GSCRow = dict[str, str | float]

_ALLOWED_DIMENSIONS = frozenset({"date", "page", "query", "country", "device"})
_METRICS = ("clicks", "impressions", "ctr", "position")
_BATCH_SIZE = 25_000
_MAX_ROWS = 50_000


@dataclass(frozen=True)
class GSCQueryResult:
    """A bounded Search Console result with an explicit completeness signal."""

    rows: tuple[GSCRow, ...]
    dimensions: tuple[GSCDimension, ...]
    truncated: bool
    row_cap: int


@dataclass(frozen=True)
class GSCMetadataResult:
    """Only approved freshness fields; no metrics, queries, URLs or body text."""
    request_scope: dict
    first_incomplete_date: str | None
    dates_observed: tuple[str, ...]
    truncated: bool
    response_aggregation_type: str | None


class _SearchAnalyticsBody(TypedDict):
    startDate: str
    endDate: str
    dimensions: list[GSCDimension]
    type: Literal["web"]
    dataState: Literal["final", "all"]
    rowLimit: int
    startRow: int


class _ScopedSearchAnalyticsBody(_SearchAnalyticsBody, total=False):
    dimensionFilterGroups: list[dict]


class _SearchAnalyticsRequest(Protocol):
    def execute(self) -> Mapping[str, object]:
        """Execute the prepared Search Console request."""


class _SearchAnalyticsResource(Protocol):
    def query(
        self, *, siteUrl: str, body: _SearchAnalyticsBody
    ) -> _SearchAnalyticsRequest:
        """Build a Search Console query request."""


class _GSCService(Protocol):
    def searchanalytics(self) -> _SearchAnalyticsResource:
        """Return the Search Console analytics resource."""


class GSCAdapter:
    """Read final web Search Console data through an injected service.

    Detailed output is deliberately bounded to 50,000 rows. It is not an
    exhaustive representation of query-level Search Console data.
    """

    exact_scope_contract = "genemedi-exact/v1"
    exact_metadata_contract = "genemedi-exact-metadata/v1"

    def __init__(
        self,
        service: _GSCService,
        *,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._service = service
        self._retry_policy = retry_policy
        self._sleep = sleep

    def query(
        self,
        site: SiteConfig,
        date_range: DateRange,
        dimensions: Sequence[GSCDimension],
    ) -> list[GSCRow]:
        """Return final web Search Console rows for approved dimensions only."""
        return list(self.query_result(site, date_range, dimensions).rows)

    def query_result(
        self,
        site: SiteConfig,
        date_range: DateRange,
        dimensions: Sequence[GSCDimension],
        *, page_paths: Sequence[str] | None = None, exact_case: bool = False, data_state: str = "final",
    ) -> GSCQueryResult:
        """Return final web rows with 50,000-row-cap metadata.

        A full final batch at the cap is marked truncated because the API has
        not established that more matching rows do not exist.
        """
        if data_state not in {"final","all"}:raise ValueError("unsupported GSC data state")
        requested_dimensions = tuple(dimensions)
        _validate_dimensions(requested_dimensions)
        cohort_filter = page_regex(page_paths, site.domains, exact_case=exact_case) if page_paths is not None else None

        rows: list[GSCRow] = []
        truncated = False
        for start_row in range(0, _MAX_ROWS, _BATCH_SIZE):
            body: _ScopedSearchAnalyticsBody = {
                    "startDate": date_range.start.isoformat(),
                    "endDate": date_range.end.isoformat(),
                    "dimensions": list(requested_dimensions),
                    "type": "web",
                    "dataState": data_state,
                    "rowLimit": _BATCH_SIZE,
                    "startRow": start_row,
                }
            if cohort_filter is not None:
                body["dimensionFilterGroups"] = [{"groupType": "and", "filters": [{
                    "dimension": "page", "operator": "includingRegex", "expression": cohort_filter,
                }]}]
            request = self._service.searchanalytics().query(siteUrl=site.gsc_property_url, body=body)
            response = retry_transient(
                request.execute,
                policy=self._retry_policy,
                sleep=self._sleep,
            )
            batch = _response_rows(response)
            rows.extend(_normalize_row(row, requested_dimensions) for row in batch)
            if len(batch) < _BATCH_SIZE:
                break
            if start_row + _BATCH_SIZE >= _MAX_ROWS:
                truncated = True
        return GSCQueryResult(
            rows=tuple(rows),
            dimensions=requested_dimensions,
            truncated=truncated,
            row_cap=_MAX_ROWS,
        )

    def query_exact_result(self, site, date_range, dimensions, *, page_paths, data_state="final"):
        return self.query_result(site, date_range, dimensions, page_paths=page_paths, exact_case=True,data_state=data_state)

    def availability_exact_result(self, site, date_range, *, page_paths):
        """One explicitly bounded date-only all-state request, exact page case.

        Missing metadata is unknown. Returned dates are not a complete-data or
        zero-fill guarantee. This method does not widen the supplied range.
        """
        if not 0 <= (date_range.end-date_range.start).days <= 13:
            raise ValueError("GSC metadata window must be within 14 days")
        pattern=page_regex(page_paths,site.domains,exact_case=True)
        body={"startDate":date_range.start.isoformat(),"endDate":date_range.end.isoformat(),
            "dimensions":["date"],"type":"web","dataState":"all","rowLimit":100,"startRow":0,
            "dimensionFilterGroups":[{"groupType":"and","filters":[{"dimension":"page","operator":"includingRegex","expression":pattern}]}]}
        request=self._service.searchanalytics().query(siteUrl=site.gsc_property_url,body=body)
        response=retry_transient(request.execute,policy=self._retry_policy,sleep=self._sleep)
        metadata=response.get("metadata") or {}
        if not isinstance(metadata,Mapping):raise ValueError("invalid GSC freshness metadata")
        first,second=metadata.get("firstIncompleteDate"),metadata.get("first_incomplete_date")
        if first is not None and second is not None and first!=second:raise ValueError("conflicting GSC incomplete date metadata")
        incomplete=first if first is not None else second
        if incomplete is not None:
            incomplete=date.fromisoformat(incomplete).isoformat()
            if not date_range.start.isoformat()<=incomplete<=date_range.end.isoformat():raise ValueError("GSC incomplete date outside requested metadata range")
        rows=_response_rows(response);days=[]
        for row in rows[:100]:
            keys=row.get("keys")
            if not isinstance(keys,list) or len(keys)!=1 or not isinstance(keys[0],str):raise ValueError("invalid GSC freshness date grain")
            day=date.fromisoformat(keys[0]).isoformat()
            if not date_range.start.isoformat()<=day<=date_range.end.isoformat() or day in days:raise ValueError("invalid GSC freshness date scope")
            days.append(day)
        aggregation=response.get("responseAggregationType")
        return GSCMetadataResult({"site":site.site_key,"property":site.gsc_property_url,"domains":sorted(site.domains),
            "start":date_range.start.isoformat(),"end":date_range.end.isoformat(),"pages":list(page_paths),"countries":[],"channels":[]},
            incomplete,tuple(days),len(rows)>=100,aggregation if aggregation in {"auto","byPage","byProperty"} else None)


def _validate_dimensions(dimensions: tuple[GSCDimension, ...]) -> None:
    for dimension in dimensions:
        if dimension not in _ALLOWED_DIMENSIONS:
            raise ValueError(f"unsupported GSC dimension '{dimension}'")


def _response_rows(response: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw_rows = response.get("rows", [])
    if not isinstance(raw_rows, list):
        raise ValueError("GSC response field 'rows' must be a list")
    if not all(isinstance(row, Mapping) for row in raw_rows):
        raise ValueError("GSC response rows must be mappings")
    return raw_rows


def _normalize_row(
    row: Mapping[str, object], dimensions: tuple[GSCDimension, ...]
) -> GSCRow:
    raw_keys = row.get("keys", [])
    if not isinstance(raw_keys, list) or not all(
        isinstance(value, str) for value in raw_keys
    ):
        raise ValueError("GSC response row field 'keys' must be a list of strings")
    normalized: GSCRow = {
        dimension: sanitize_url_query(value)
        for dimension, value in zip(dimensions, raw_keys, strict=True)
    }
    normalized.update({metric: _as_float(row[metric]) for metric in _METRICS})
    return normalized


def _as_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("GSC metric values must be numeric")
    return float(value)
