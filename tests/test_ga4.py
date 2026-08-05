from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest
from google.analytics.data_v1beta.types import RunReportResponse

import website_analytics.adapters.ga4 as ga4
from website_analytics.adapters.ga4 import GA4Adapter
from website_analytics.models import DateRange, SiteConfig


FIXTURES = Path(__file__).parent / "fixtures"
METRICS = (
    "sessions",
    "totalUsers",
    "activeUsers",
    "engagedSessions",
    "engagementRate",
    "screenPageViews",
    "keyEvents",
)


@dataclass(frozen=True)
class FakeValue:
    value: str


@dataclass(frozen=True)
class FakeRow:
    dimension_values: tuple[FakeValue, ...]
    metric_values: tuple[FakeValue, ...]


@dataclass(frozen=True)
class FakeResponse:
    rows: tuple[FakeRow, ...]
    row_count: int | None = None


class FakeGA4Client:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests: list[object] = []
        self._returned_response = False

    def run_report(self, request: object) -> FakeResponse:
        self.requests.append(request)
        if self._returned_response:
            return FakeResponse(rows=())
        self._returned_response = True
        return self.response


class PaginatedFakeGA4Client:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[object] = []

    def run_report(self, request: object) -> FakeResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def test_daily_sends_expected_ga4_request_and_normalizes_iso_date() -> None:
    client = FakeGA4Client(_response_from_fixture("ga4_report.json"))

    result = GA4Adapter(client).daily(_site(), _date_range())

    request = client.requests[0]
    assert request.property == "properties/123456789"
    assert [dimension.name for dimension in request.dimensions] == ["date"]
    assert [metric.name for metric in request.metrics] == list(METRICS)
    assert request.limit == 250000
    assert request.offset == 0
    assert result == [
        {
            "date": "2026-08-03",
            "sessions": 10.0,
            "totalUsers": 8.0,
            "activeUsers": 7.0,
            "engagedSessions": 6.0,
            "engagementRate": 0.6,
            "screenPageViews": 30.0,
            "keyEvents": 2.0,
        }
    ]


def test_pages_sends_landing_page_dimension_and_normalizes_values() -> None:
    client = FakeGA4Client(
        FakeResponse(
            rows=(
                FakeRow(
                    dimension_values=(FakeValue("/products?source=ad"),),
                    metric_values=tuple(FakeValue("1") for _ in METRICS),
                ),
            )
        )
    )

    result = GA4Adapter(client).pages(_site(), _date_range())

    request = client.requests[0]
    assert request.property == "properties/123456789"
    assert [dimension.name for dimension in request.dimensions] == [
        "landingPagePlusQueryString"
    ]
    assert result == [
        {
            "landingPagePlusQueryString": "/products?source=ad",
            "sessions": 1.0,
            "totalUsers": 1.0,
            "activeUsers": 1.0,
            "engagedSessions": 1.0,
            "engagementRate": 1.0,
            "screenPageViews": 1.0,
            "keyEvents": 1.0,
        }
    ]


def test_daily_returns_empty_list_for_response_without_rows() -> None:
    client = FakeGA4Client(FakeResponse(rows=()))

    assert GA4Adapter(client).daily(_site(), _date_range()) == []


def test_pages_paginates_until_authoritative_row_count_and_retains_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ga4, "_PAGE_SIZE", 2)
    client = PaginatedFakeGA4Client(
        [
            FakeResponse(
                rows=(
                    _page_row("/first", "1"),
                    _page_row("/second", "2"),
                ),
                row_count=3,
            ),
            FakeResponse(rows=(_page_row("/third", "3"),), row_count=3),
        ]
    )

    result = GA4Adapter(client).pages(_site(), _date_range())

    assert [request.limit for request in client.requests] == [2, 2]
    assert [request.offset for request in client.requests] == [0, 2]
    assert [row["landingPagePlusQueryString"] for row in result] == [
        "/first",
        "/second",
        "/third",
    ]
    assert [row["sessions"] for row in result] == [1.0, 2.0, 3.0]


def test_pages_raises_when_empty_page_precedes_authoritative_row_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ga4, "_PAGE_SIZE", 2)
    client = PaginatedFakeGA4Client(
        [
            FakeResponse(
                rows=(
                    _page_row("/first", "1"),
                    _page_row("/second", "2"),
                ),
                row_count=3,
            ),
            FakeResponse(rows=(), row_count=3),
        ]
    )

    with pytest.raises(
        ValueError, match="GA4 response ended before its reported row_count"
    ):
        GA4Adapter(client).pages(_site(), _date_range())

    assert [request.offset for request in client.requests] == [0, 2]


def test_fixture_response_preserves_google_row_count() -> None:
    response = _response_from_fixture("ga4_report.json")

    assert isinstance(response, RunReportResponse)
    assert response.row_count == 1


def _response_from_fixture(name: str) -> RunReportResponse:
    return RunReportResponse.from_json((FIXTURES / name).read_text(encoding="utf-8"))


def _page_row(page: str, sessions: str) -> FakeRow:
    return FakeRow(
        dimension_values=(FakeValue(page),),
        metric_values=(
            FakeValue(sessions),
            FakeValue("1"),
            FakeValue("1"),
            FakeValue("1"),
            FakeValue("1"),
            FakeValue("1"),
            FakeValue("1"),
        ),
    )


def _site() -> SiteConfig:
    return SiteConfig(
        site_key="demo",
        display_name="Demo",
        domains=("example.com",),
        timezone="Asia/Shanghai",
        ga4_property_id="123456789",
        gsc_property_url="sc-domain:example.com",
    )


def _date_range() -> DateRange:
    return DateRange(start=date(2026, 8, 3), end=date(2026, 8, 3))
