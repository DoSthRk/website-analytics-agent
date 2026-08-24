import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from website_analytics.cache import write_cached_json
from website_analytics.dashboard_v3_backfill import (
    audit_path_for_day,
    build_v3_backfill,
)
from website_analytics.information_mapping import load_information_mapping
from website_analytics.page_classification import (
    PageClassificationConfig,
    build_page_dimension,
)
from website_analytics.product_mapping import load_product_mapping

from test_feishu_dashboard_v3 import _details


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_v3_backfill_builds_complete_unique_daily_facts(tmp_path: Path) -> None:
    data_date = date(2026, 8, 10)
    cache_dir = tmp_path / "cache"
    audit_dir = tmp_path / "audits"
    details = _details()
    for source, names in {
        "ga4": ("GA4 Daily", "GA4 Pages"),
        "gsc": ("GSC Daily", "GSC Pages", "GSC Queries"),
        "inquiry": ("Inquiry Daily", "Inquiry Pages"),
    }.items():
        write_cached_json(
            cache_dir,
            "genemedi-net",
            source,
            {
                "source": source,
                "start": data_date.isoformat(),
                "end": data_date.isoformat(),
            },
            {name: details[name] for name in names},
        )
    _write_audit(audit_dir, data_date)
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

    payload = build_v3_backfill(
        site="genemedi-net",
        start=data_date,
        end=data_date,
        cache_dir=cache_dir,
        audit_dir=audit_dir,
        product_mapping=mapping,
        page_dimension=_page_dimension(),
        information_mapping=information_mapping,
    )

    assert payload["write_enabled"] is False
    assert payload["date_range"]["days"] == 1
    assert payload["reconciliation"]["status"] == "passed"
    assert payload["reconciliation"]["record_counts"] == {
        "overview_daily": 1,
        "product_daily": len(mapping.report_lines),
        "information_daily": (
            len(information_mapping.themes)
            * len(information_mapping.content_types)
        ),
    }
    assert payload["reconciliation"]["additive_totals"]["ga4_sessions"] == 30
    assert payload["reconciliation"]["additive_totals"]["accepted_inquiries"] == 2


def test_v3_backfill_rejects_nonfinal_audit(tmp_path: Path) -> None:
    data_date = date(2026, 8, 10)
    cache_dir = tmp_path / "cache"
    audit_dir = tmp_path / "audits"
    details = _details()
    for source, names in {
        "ga4": ("GA4 Daily", "GA4 Pages"),
        "gsc": ("GSC Daily", "GSC Pages", "GSC Queries"),
        "inquiry": ("Inquiry Daily", "Inquiry Pages"),
    }.items():
        write_cached_json(
            cache_dir,
            "genemedi-net",
            source,
            {
                "source": source,
                "start": data_date.isoformat(),
                "end": data_date.isoformat(),
            },
            {name: details[name] for name in names},
        )
    _write_audit(audit_dir, data_date, gsc_status="error")
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

    with pytest.raises(ValueError, match="gsc"):
        build_v3_backfill(
            site="genemedi-net",
            start=data_date,
            end=data_date,
            cache_dir=cache_dir,
            audit_dir=audit_dir,
            product_mapping=mapping,
            page_dimension=_page_dimension(),
            information_mapping=information_mapping,
        )


def test_v3_backfill_rejects_more_than_400_days(tmp_path: Path) -> None:
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
    start = date(2025, 1, 1)

    with pytest.raises(ValueError, match="400-day"):
        build_v3_backfill(
            site="genemedi-net",
            start=start,
            end=start + timedelta(days=400),
            cache_dir=tmp_path / "cache",
            audit_dir=tmp_path / "audits",
            product_mapping=mapping,
            page_dimension=_page_dimension(),
            information_mapping=information_mapping,
        )


def _write_audit(
    root: Path, data_date: date, *, gsc_status: str = "ok"
) -> None:
    path = audit_path_for_day(root, "genemedi-net", data_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = data_date.isoformat()
    path.write_text(
        json.dumps(
            {
                "request": {
                    "command": "fetch",
                    "site": "genemedi-net",
                    "date_range": {"start": value, "end": value},
                },
                "source_statuses": {
                    "generated_at": "2026-08-24T07:03:35Z",
                    "sources": {
                        "ga4": {"status": "ok"},
                        "gsc": {"status": gsc_status, "truncated": False},
                        "inquiry": {"status": "ok"},
                    },
                },
            }
        ),
        encoding="utf-8",
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
