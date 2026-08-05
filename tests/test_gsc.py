from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from website_analytics.adapters.gsc import GSCAdapter
from website_analytics.models import DateRange, SiteConfig


FIXTURES = Path(__file__).parent / "fixtures"


class FakeRequest:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response

    def execute(self) -> dict[str, object]:
        return self.response


class FakeSearchAnalytics:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    def query(self, *, siteUrl: str, body: dict[str, object]) -> FakeRequest:
        self.calls.append((siteUrl, body))
        return FakeRequest(self.responses.pop(0))


class FakeGSCService:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.resource = FakeSearchAnalytics(responses)

    def searchanalytics(self) -> FakeSearchAnalytics:
        return self.resource


def test_daily_query_sends_safe_gsc_request_and_normalizes_metrics() -> None:
    service = FakeGSCService([_fixture("gsc_daily.json")])

    result = GSCAdapter(service).query(_site(), _date_range(), ("date",))

    assert service.resource.calls == [
        (
            "sc-domain:example.com",
            {
                "startDate": "2026-08-03",
                "endDate": "2026-08-03",
                "dimensions": ["date"],
                "type": "web",
                "dataState": "final",
                "rowLimit": 25000,
                "startRow": 0,
            },
        )
    ]
    assert result == [
        {
            "date": "2026-08-03",
            "clicks": 10.0,
            "impressions": 100.0,
            "ctr": 0.1,
            "position": 3.5,
        }
    ]


@pytest.mark.parametrize(
    ("fixture_name", "dimension", "expected_value", "expected_clicks"),
    [
        ("gsc_pages.json", "page", "https://example.com/products", 5.0),
        ("gsc_queries.json", "query", "recombinant protein", 2.0),
    ],
)
def test_query_maps_response_keys_to_requested_dimensions(
    fixture_name: str, dimension: str, expected_value: str, expected_clicks: float
) -> None:
    service = FakeGSCService([_fixture(fixture_name)])

    result = GSCAdapter(service).query(_site(), _date_range(), (dimension,))

    assert result[0][dimension] == expected_value
    assert result[0]["clicks"] == expected_clicks


def test_query_rejects_unapproved_dimension_before_calling_service() -> None:
    service = FakeGSCService([])

    with pytest.raises(ValueError, match="unsupported GSC dimension 'searchAppearance'"):
        GSCAdapter(service).query(_site(), _date_range(), ("searchAppearance",))

    assert service.resource.calls == []


def test_query_fetches_second_batch_only_after_full_first_batch() -> None:
    full_batch = _fixture("gsc_daily.json")["rows"] * 25000
    second_batch = _fixture("gsc_daily.json")["rows"]
    service = FakeGSCService([{"rows": full_batch}, {"rows": second_batch}])

    result = GSCAdapter(service).query(_site(), _date_range(), ("date",))

    assert [body["startRow"] for _, body in service.resource.calls] == [0, 25000]
    assert len(result) == 25001


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


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
