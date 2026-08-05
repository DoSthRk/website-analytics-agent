"""Google Search Console adapter with bounded detailed-report pagination."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

from website_analytics.models import DateRange, SiteConfig
from website_analytics.url_safety import sanitize_url_query


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


class _SearchAnalyticsBody(TypedDict):
    startDate: str
    endDate: str
    dimensions: list[GSCDimension]
    type: Literal["web"]
    dataState: Literal["final"]
    rowLimit: int
    startRow: int


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

    def __init__(self, service: _GSCService) -> None:
        self._service = service

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
    ) -> GSCQueryResult:
        """Return final web rows with 50,000-row-cap metadata.

        A full final batch at the cap is marked truncated because the API has
        not established that more matching rows do not exist.
        """
        requested_dimensions = tuple(dimensions)
        _validate_dimensions(requested_dimensions)

        rows: list[GSCRow] = []
        truncated = False
        for start_row in range(0, _MAX_ROWS, _BATCH_SIZE):
            response = self._service.searchanalytics().query(
                siteUrl=site.gsc_property_url,
                body={
                    "startDate": date_range.start.isoformat(),
                    "endDate": date_range.end.isoformat(),
                    "dimensions": list(requested_dimensions),
                    "type": "web",
                    "dataState": "final",
                    "rowLimit": _BATCH_SIZE,
                    "startRow": start_row,
                },
            ).execute()
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
