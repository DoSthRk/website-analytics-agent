import json
from datetime import date, datetime, timezone
from pathlib import Path

from website_analytics.dashboard_v3 import build_v3_dry_run
from website_analytics.information_mapping import load_information_mapping
from website_analytics.product_mapping import load_product_mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = PROJECT_ROOT / "config" / "feishu_dashboard" / "v3" / "data_contract.json"
MANIFEST_PATH = PROJECT_ROOT / "config" / "feishu_dashboard" / "v3" / "dashboard_manifest.json"


def test_v3_manifest_references_only_declared_fields() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert contract["version"] == manifest["version"] == "3"
    assert contract["write_enabled"] is manifest["write_enabled"] is False

    tables = {table["display_name"]: table for table in contract["tables"]}
    assert len(tables) == len(contract["tables"]) == 3
    assert len({table["logical_name"] for table in tables.values()}) == 3
    for table in tables.values():
        fields = table["fields"]
        assert len({field["key"] for field in fields}) == len(fields)
        names = {field["name"] for field in fields}
        assert len(names) == len(fields)
        assert table["stable_key"] in names
        stable = next(field for field in fields if field["name"] == table["stable_key"])
        assert stable["required"] is True

    for dashboard in manifest["dashboards"]:
        table = tables[dashboard["table"]]
        fields = {field["name"] for field in table["fields"]}
        assert set(dashboard["filters"]) <= fields
        for block in dashboard["blocks"]:
            for name in (
                [block["metric"]] if "metric" in block else block.get("metrics", [])
            ):
                assert name in fields
            assert set(block.get("group_by", [])) <= fields
            assert set(block.get("fields", [])) <= fields
            assert set(block.get("filter", {})) <= fields
            for sort in block.get("sort", []):
                assert sort["field"] in fields


def test_v3_dry_run_keeps_source_metrics_and_partial_scope_explicit() -> None:
    product_mapping = load_product_mapping(
        PROJECT_ROOT / "config" / "product_mappings" / "genemedi-net.yaml",
        "genemedi-net",
    )
    information_mapping = load_information_mapping(
        PROJECT_ROOT / "config" / "information_mappings" / "genemedi-net.yaml",
        "genemedi-net",
    )
    assert product_mapping is not None
    assert information_mapping is not None
    current = _details(product_sessions=8, information_sessions=20, other_sessions=2)
    previous = _details(product_sessions=5, information_sessions=12, other_sessions=3)

    payload = build_v3_dry_run(
        site="genemedi-net",
        current_start=date(2026, 8, 10),
        current_end=date(2026, 8, 16),
        previous_start=date(2026, 8, 3),
        previous_end=date(2026, 8, 9),
        current_details=current,
        previous_details=previous,
        product_mapping=product_mapping,
        information_mapping=information_mapping,
        product_paths={"/i/gmp-vt-p173"},
        refreshed_at=datetime(2026, 8, 24, 7, 3, 35, tzinfo=timezone.utc),
    )

    assert payload["write_enabled"] is False
    assert payload["classification_scope"]["status"] == "prototype_partial"
    overview = payload["records"]["overview_periods"][0]
    assert overview["ga4_sessions"] == 30
    assert overview["gsc_clicks"] == 15
    assert overview["accepted_inquiries"] == 2
    assert overview["product_page_sessions"] == 8
    assert overview["information_page_sessions"] == 20
    assert overview["other_page_sessions"] == 2
    assert overview["sessions_delta"] == 10
    assert overview["data_status"] == "页面分类原型"

    products = payload["records"]["product_periods"]
    current_product = next(row for row in products if row["is_current"])
    assert current_product["product_line_id"] == "VT_INFECTIOUS"
    assert current_product["ga4_sessions"] == 8
    assert current_product["sessions_delta"] == 3

    information = payload["records"]["information_periods"]
    current_information = next(row for row in information if row["is_current"])
    assert current_information["theme_id"] == "TARMART_TARGET"
    assert current_information["content_type_id"] == "TARGET_REFERENCE"
    assert current_information["ga4_sessions"] == 20

    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    contract_fields = {
        table["logical_name"]: {field["key"] for field in table["fields"]}
        for table in contract["tables"]
    }
    for logical_name, rows in payload["records"].items():
        assert rows
        assert all(set(row) == contract_fields[logical_name] for row in rows)

    assert len({row["period_key"] for row in payload["records"]["overview_periods"]}) == 2
    assert len(
        {row["product_period_key"] for row in payload["records"]["product_periods"]}
    ) == len(payload["records"]["product_periods"])
    assert len(
        {
            row["information_period_key"]
            for row in payload["records"]["information_periods"]
        }
    ) == len(payload["records"]["information_periods"])


def _details(
    *, product_sessions: int, information_sessions: int, other_sessions: int
) -> dict:
    sessions = product_sessions + information_sessions + other_sessions
    return {
        "GA4 Daily": [
            {
                "date": "2026-08-10",
                "sessions": sessions,
                "activeUsers": sessions - 2,
                "engagedSessions": sessions // 2,
                "keyEvents": 1,
            }
        ],
        "GA4 Pages": [
            {
                "landingPagePlusQueryString": "/i/gmp-vt-p173",
                "sessions": product_sessions,
            },
            {
                "landingPagePlusQueryString": "/i/itd-reference-overview",
                "sessions": information_sessions,
            },
            {"landingPagePlusQueryString": "/", "sessions": other_sessions},
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
