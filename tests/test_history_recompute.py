from datetime import date
from pathlib import Path

from website_analytics.cache import write_cached_json
from website_analytics.history_recompute import RuleSet, recompute_days
from website_analytics.information_mapping import load_information_mapping
from website_analytics.product_mapping import load_product_mapping

from test_feishu_dashboard_v3 import _details
from test_feishu_dashboard_v3_backfill import _page_dimension, _write_audit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 8, 10)


def _write_day(cache_dir: Path, audit_dir: Path, data_date: date) -> None:
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
            {"source": source, "start": data_date.isoformat(), "end": data_date.isoformat()},
            {name: details[name] for name in names},
        )
    _write_audit(audit_dir, data_date)


def _rules() -> RuleSet:
    mapping = load_product_mapping(PROJECT_ROOT / "config/product_mappings/genemedi-net.yaml", "genemedi-net")
    information = load_information_mapping(
        PROJECT_ROOT / "config/information_mappings/genemedi-net.yaml", "genemedi-net"
    )
    assert mapping is not None and information is not None
    return RuleSet(mapping, _page_dimension(), information)


def test_recompute_rebuilds_complete_days_and_reconciles(tmp_path: Path) -> None:
    cache_dir, audit_dir = tmp_path / "cache", tmp_path / "audits"
    _write_day(cache_dir, audit_dir, DAY)
    rules = _rules()

    result = recompute_days(
        site="genemedi-net",
        days=[DAY],
        cache_dir=cache_dir,
        audit_dir=audit_dir,
        current=rules,
        baseline=rules,
    )

    assert result["write_enabled"] is False
    assert result["completed_days"] == [DAY.isoformat()]
    assert result["skipped_days"] == []
    assert result["mismatches"] == []
    assert result["record_counts"]["overview_daily"] == 1
    assert result["record_counts"]["product_daily"] == len(rules.product_mapping.report_lines)
    assert result["record_counts"]["product_funnel_daily"] == len(rules.product_mapping.report_lines)
    # Same rules on both sides: nothing moves between classes or lines.
    assert set(result["page_class_session_shift"].values()) == {0}
    assert all(row["baseline"] == row["current"] for row in result["product_line_sessions"])


def test_recompute_skips_incomplete_days_instead_of_writing_zero(tmp_path: Path) -> None:
    cache_dir, audit_dir = tmp_path / "cache", tmp_path / "audits"
    _write_day(cache_dir, audit_dir, DAY)
    _write_audit(audit_dir, date(2026, 8, 11), gsc_status="error")
    rules = _rules()

    result = recompute_days(
        site="genemedi-net",
        days=[DAY, date(2026, 8, 11), date(2026, 8, 12)],
        cache_dir=cache_dir,
        audit_dir=audit_dir,
        current=rules,
        baseline=rules,
    )

    assert result["completed_days"] == [DAY.isoformat()]
    assert [item["data_date"] for item in result["skipped_days"]] == ["2026-08-11", "2026-08-12"]
    assert result["record_counts"]["overview_daily"] == 1
