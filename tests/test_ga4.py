from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

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


class FakeGA4Client:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests: list[object] = []

    def run_report(self, request: object) -> FakeResponse:
        self.requests.append(request)
        return self.response


def test_daily_sends_expected_ga4_request_and_normalizes_iso_date() -> None:
    client = FakeGA4Client(_response_from_fixture("ga4_report.json"))

    result = GA4Adapter(client).daily(_site(), _date_range())

    request = client.requests[0]
    assert request.property == "properties/123456789"
    assert [dimension.name for dimension in request.dimensions] == ["date"]
    assert [metric.name for metric in request.metrics] == list(METRICS)
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


def _response_from_fixture(name: str) -> FakeResponse:
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return FakeResponse(
        rows=tuple(
            FakeRow(
                dimension_values=tuple(
                    FakeValue(value["value"]) for value in row["dimensionValues"]
                ),
                metric_values=tuple(
                    FakeValue(value["value"]) for value in row["metricValues"]
                ),
            )
            for row in payload["rows"]
        )
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
