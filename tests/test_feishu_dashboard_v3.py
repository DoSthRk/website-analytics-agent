import json
from datetime import date
from pathlib import Path

import pytest

from website_analytics.dashboard_v3 import build_v3_daily_records
from website_analytics.information_mapping import load_information_mapping
from website_analytics.page_classification import (
    PageClassificationConfig,
    build_page_dimension,
)
from website_analytics.product_mapping import load_product_mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = (
    PROJECT_ROOT / "config" / "feishu_dashboard" / "v3" / "data_contract.json"
)
MANIFEST_PATH = (
    PROJECT_ROOT
    / "config"
    / "feishu_dashboard"
    / "v3"
    / "dashboard_manifest.json"
)


def test_v3_manifest_references_only_declared_additive_fields() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert contract["version"] == manifest["version"] == "3"
    assert contract["status"] == manifest["status"] == "active"
    assert contract["write_enabled"] is manifest["write_enabled"] is True
    assert contract["date_filter"]["mode"] == "custom_range"
    assert manifest["date_selection"]["field"] == "数据日期"

    tables = {table["display_name"]: table for table in contract["tables"]}
    assert len(tables) == len(contract["tables"]) == 3
    assert len({table["logical_name"] for table in tables.values()}) == 3
    for table in tables.values():
        fields = table["fields"]
        assert len({field["key"] for field in fields}) == len(fields)
        names = {field["name"] for field in fields}
        assert len(names) == len(fields)
        assert table["stable_key"] in names
        stable = next(
            field for field in fields if field["name"] == table["stable_key"]
        )
        assert stable["required"] is True

    for dashboard in manifest["dashboards"]:
        table = tables[dashboard["table"]]
        fields = {field["name"] for field in table["fields"]}
        assert set(dashboard["filters"]) <= fields
        for block in dashboard["blocks"]:
            metrics = (
                [block["metric"]]
                if "metric" in block
                else block.get("metrics", [])
            )
            for name in metrics:
                field = next(item for item in table["fields"] if item["name"] == name)
                assert name in fields
                assert field.get("aggregation") == "sum"
                assert block["aggregation"] == "sum"
            assert set(block.get("group_by", [])) <= fields
            assert set(block.get("fields", [])) <= fields
            for sort in block.get("sort", []):
                assert sort["field"] in fields


def test_v3_daily_records_reconcile_three_sources_and_page_dimensions() -> None:
    mapping = load_product_mapping(
        PROJECT_ROOT / "config" / "product_mappings" / "genemedi-net.yaml",
        "genemedi-net",
    )
    information_mapping = load_information_mapping(
        PROJECT_ROOT / "config" / "information_mappings" / "genemedi-net.yaml",
        "genemedi-net",
    )
    assert mapping is not None
    assert information_mapping is not None
    data_date = date(2026, 8, 10)

    payload = build_v3_daily_records(
        site="genemedi-net",
        data_date=data_date,
        fetch_result=_fetch_result(),
        details=_details(),
        product_mapping=mapping,
        page_dimension=_page_dimension(),
        information_mapping=information_mapping,
    )

    assert payload["write_enabled"] is False
    assert payload["mode"] == "daily_dry_run"
    overview = payload["records"]["overview_daily"][0]
    assert overview["daily_key"] == "genemedi-net|2026-08-10"
    assert overview["ga4_sessions"] == 30
    assert overview["gsc_clicks"] == 15
    assert overview["accepted_inquiries"] == 2
    assert overview["product_page_sessions"] == 8
    assert overview["information_page_sessions"] == 20
    assert overview["other_page_sessions"] == 2
    assert overview["classified_page_sessions"] == 28

    products = payload["records"]["product_daily"]
    assert len(products) == len(mapping.report_lines)
    veterinary = next(
        row for row in products if row["product_line_id"] == "VT_INFECTIOUS"
    )
    assert veterinary["ga4_sessions"] == 8
    assert veterinary["gsc_clicks"] == 5
    assert veterinary["accepted_inquiries"] == 1

    information = payload["records"]["information_daily"]
    assert len(information) == len(information_mapping.themes) * len(
        information_mapping.content_types
    )
    target_reference = next(
        row
        for row in information
        if row["theme_id"] == "TARMART_TARGET"
        and row["content_type_id"] == "TARGET_REFERENCE"
    )
    assert target_reference["ga4_sessions"] == 20
    assert target_reference["gsc_clicks"] == 10
    assert target_reference["accepted_inquiries"] == 1

    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    contract_fields = {
        table["logical_name"]: {field["key"] for field in table["fields"]}
        for table in contract["tables"]
    }
    for logical_name, rows in payload["records"].items():
        assert rows
        assert all(set(row) == contract_fields[logical_name] for row in rows)

    assert len(
        {row["product_daily_key"] for row in products}
    ) == len(products)
    assert len(
        {row["information_daily_key"] for row in information}
    ) == len(information)


def test_v3_daily_records_reject_cross_date_details() -> None:
    mapping = load_product_mapping(
        PROJECT_ROOT / "config" / "product_mappings" / "genemedi-net.yaml",
        "genemedi-net",
    )
    information_mapping = load_information_mapping(
        PROJECT_ROOT / "config" / "information_mappings" / "genemedi-net.yaml",
        "genemedi-net",
    )
    assert mapping is not None
    assert information_mapping is not None
    details = _details()
    details["GSC Daily"][0]["date"] = "2026-08-09"

    with pytest.raises(ValueError, match="another date"):
        build_v3_daily_records(
            site="genemedi-net",
            data_date=date(2026, 8, 10),
            fetch_result=_fetch_result(),
            details=details,
            product_mapping=mapping,
            page_dimension=_page_dimension(),
            information_mapping=information_mapping,
        )


def _page_dimension():
    return build_page_dimension(
        PageClassificationConfig(
            site_key="genemedi-net", version="fixture-v3", overrides={}
        ),
        [
            {
                "route_url": "/i/gmp-vt-p173",
                "route_page_id": 1,
                "route_source": "pages",
                "content_page_id": 1,
                "template": "index-with-SideBar",
            },
            {
                "route_url": "/i/itd-reference-overview",
                "route_page_id": 2,
                "route_source": "pages",
                "content_page_id": 2,
                "template": "index-target",
            },
        ],
    )


def _fetch_result() -> dict:
    return {
        "status": "ok",
        "complete": True,
        "freshness": "2026-08-24T07:03:35Z",
        "sources": {
            "ga4": {"status": "ok"},
            "gsc": {"status": "ok"},
            "inquiry": {"status": "ok"},
        },
        "totals": {
            "ga4": {"sessions": 30, "keyEvents": 2},
            "gsc": {"clicks": 15, "impressions": 500},
            "inquiry": {
                "storedSubmissions": 2,
                "quarantinedSubmissions": 0,
                "nonQuarantinedSubmissions": 2,
            },
        },
    }


def _details() -> dict:
    return {
        "GA4 Daily": [
            {
                "date": "2026-08-10",
                "sessions": 30,
                "activeUsers": 28,
                "engagedSessions": 15,
                "keyEvents": 2,
            }
        ],
        "GA4 Pages": [
            {
                "landingPagePlusQueryString": "/i/gmp-vt-p173",
                "sessions": 8,
            },
            {
                "landingPagePlusQueryString": "/i/itd-reference-overview",
                "sessions": 20,
            },
            {"landingPagePlusQueryString": "(not set)", "sessions": 2},
        ],
        "GSC Daily": [
            {
                "date": "2026-08-10",
                "clicks": 15,
                "impressions": 500,
                "position": 12,
            }
        ],
        "GSC Pages": [
            {
                "page": "https://www.genemedi.net/i/gmp-vt-p173",
                "clicks": 5,
                "impressions": 50,
            },
            {
                "page": "https://www.genemedi.net/i/itd-reference-overview",
                "clicks": 10,
                "impressions": 450,
            },
        ],
        "GSC Queries": [],
        "Inquiry Daily": [
            {
                "date": "2026-08-10",
                "storedSubmissions": 2,
                "quarantinedSubmissions": 0,
                "nonQuarantinedSubmissions": 2,
            }
        ],
        "Inquiry Pages": [
            {
                "sourceUrl": "https://www.genemedi.net/i/gmp-vt-p173",
                "storedSubmissions": 1,
                "quarantinedSubmissions": 0,
                "nonQuarantinedSubmissions": 1,
            },
            {
                "sourceUrl": "https://www.genemedi.net/i/itd-reference-overview",
                "storedSubmissions": 1,
                "quarantinedSubmissions": 0,
                "nonQuarantinedSubmissions": 1,
            },
        ],
    }
