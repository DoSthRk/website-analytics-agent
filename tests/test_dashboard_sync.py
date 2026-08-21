import json
from datetime import date

from website_analytics.dashboard_sync import (
    build_dashboard_records,
    cache_path_for_period,
    load_period_details,
)
from website_analytics.periods import AnalyticsPeriod
from website_analytics.page_classification import (
    PageClassificationConfig,
    build_page_dimension,
)
from website_analytics.product_mapping import ProductMapping, ProductRule, ReportLine


def _mapping() -> ProductMapping:
    return ProductMapping(
        site_key="genemedi-net",
        version="test",
        report_lines=(ReportLine(identifier="GMP", name="GMP"),),
        rules=(
            ProductRule(
                identifier="gmp",
                priority=1,
                match_type="path_prefix",
                values=("/i/gmp",),
                product_line_id="GMP",
                report_line_id="GMP",
                include_in_product_report=True,
                mapping_status="approved",
                reason="test",
            ),
        ),
    )


def _dimension():
    return build_page_dimension(
        PageClassificationConfig(site_key="genemedi-net", version="test", overrides={}),
        [
            {
                "route_url": "i/gmp-product",
                "route_page_id": 1,
                "content_page_id": 1,
                "template": "indexwithSideBar",
            }
        ],
    )


def _result(*, sessions: int, clicks: int, inquiries: int) -> dict:
    return {
        "status": "ok",
        "complete": True,
        "freshness": "2026-08-19T10:30:20Z",
        "sources": {
            "ga4": {"status": "ok"},
            "gsc": {"status": "ok"},
            "inquiry": {"status": "ok"},
        },
        "totals": {
            "ga4": {
                "sessions": sessions,
                "activeUsers": sessions - 1,
                "keyEvents": 2,
            },
            "gsc": {
                "clicks": clicks,
                "impressions": 100,
                "ctr": clicks / 100,
                "position": 10.5,
            },
            "inquiry": {
                "storedSubmissions": inquiries + 1,
                "quarantinedSubmissions": 1,
                "nonQuarantinedSubmissions": inquiries,
            },
        },
    }


def _details(*, sessions: int, clicks: int, inquiries: int) -> dict:
    return {
        "GA4 Daily": [],
        "GA4 Pages": [
            {"landingPagePlusQueryString": "/i/gmp-product", "sessions": sessions}
        ],
        "GSC Daily": [],
        "GSC Pages": [
            {"page": "https://www.genemedi.net/i/gmp-product", "clicks": clicks, "impressions": 100}
        ],
        "GSC Queries": [],
        "Inquiry Daily": [],
        "Inquiry Pages": [
            {
                "sourceUrl": "https://www.genemedi.net/i/gmp-product",
                "storedSubmissions": inquiries + 1,
                "quarantinedSubmissions": 1,
                "nonQuarantinedSubmissions": inquiries,
            }
        ],
    }


def test_build_dashboard_records_keeps_sources_and_time_model_explicit() -> None:
    period = AnalyticsPeriod("week", date(2026, 8, 10), date(2026, 8, 16))
    plan = {
        "periodKey": "genemedi-net|week|2026-08-10|2026-08-16",
        "status": "complete",
        "isFinal": True,
        "dashboardWindows": ["近4周", "近12周"],
        "sourceAvailableThrough": {
            "ga4": "2026-08-17",
            "gsc": "2026-08-16",
            "inquiry": "2026-08-18",
        },
    }

    records = build_dashboard_records(
        site="genemedi-net",
        period=period,
        plan=plan,
        current_result=_result(sessions=50, clicks=10, inquiries=3),
        current_details=_details(sessions=20, clicks=8, inquiries=2),
        previous_result=_result(sessions=40, clicks=7, inquiries=1),
        previous_details=_details(sessions=12, clicks=5, inquiries=1),
        mapping=_mapping(),
        page_dimension=_dimension(),
        sync_batch="batch-1",
    )

    overview = records["overview"]
    assert overview["周期键"] == "genemedi-net|week|2026-08-10|2026-08-16"
    assert overview["官网访问次数"] == 50
    assert overview["Google自然搜索点击"] == 10
    assert overview["官网入库询盘"] == 3
    assert overview["访问较上周"] == 10
    assert overview["统计粒度"] == "周"
    assert overview["是否最终值"] is True
    assert overview["GSC数据截至"] == "2026-08-16 00:00:00"
    assert overview["产品页访问次数"] == 20
    assert overview["信息页访问次数"] == 0
    assert overview["未分类页面访问次数"] == 0
    assert overview["页面访问分类覆盖率"] == 1.0

    product = records["products"][0]
    assert product["产品周期键"].endswith("|GMP")
    assert product["官网访问次数"] == 20
    assert product["Google自然搜索点击"] == 8
    assert product["官网入库询盘"] == 2
    assert product["访问较上周"] == 8
    assert product["数据完整性"] == "完整"
    assert "数据状态" not in product


def test_load_period_details_uses_the_cli_cache_key(tmp_path) -> None:
    period = AnalyticsPeriod("day", date(2026, 8, 19), date(2026, 8, 19))
    for source, content in {
        "ga4": {"GA4 Daily": [], "GA4 Pages": []},
        "gsc": {"GSC Daily": [], "GSC Pages": [], "GSC Queries": []},
        "inquiry": {"Inquiry Daily": [], "Inquiry Pages": []},
    }.items():
        path = cache_path_for_period(tmp_path, "genemedi-net", source, period)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content), encoding="utf-8")

    details = load_period_details(tmp_path, "genemedi-net", period)
    assert set(details) == {
        "GA4 Daily",
        "GA4 Pages",
        "GSC Daily",
        "GSC Pages",
        "GSC Queries",
        "Inquiry Daily",
        "Inquiry Pages",
    }


def test_current_preliminary_period_keeps_unavailable_gsc_rates_blank() -> None:
    period = AnalyticsPeriod("week", date(2026, 8, 17), date(2026, 8, 23))
    current = _result(sessions=5, clicks=0, inquiries=0)
    current["totals"]["gsc"].pop("ctr")
    current["totals"]["gsc"].pop("position")
    records = build_dashboard_records(
        site="genemedi-net",
        period=period,
        plan={
            "periodKey": "genemedi-net|week|2026-08-17|2026-08-23",
            "status": "preliminary",
            "isFinal": False,
            "dashboardWindows": ["当前周期", "近4周", "近12周"],
            "sourceAvailableThrough": {
                "ga4": "2026-08-17",
                "gsc": "2026-08-16",
                "inquiry": "2026-08-18",
            },
        },
        current_result=current,
        current_details=_details(sessions=5, clicks=0, inquiries=0),
        previous_result=_result(sessions=4, clicks=1, inquiries=0),
        previous_details=_details(sessions=4, clicks=1, inquiries=0),
        mapping=_mapping(),
        page_dimension=_dimension(),
        sync_batch="batch-current",
    )

    assert records["overview"]["搜索点击率"] is None
    assert records["overview"]["平均搜索排名"] is None
    assert records["overview"]["数据状态"] == "preliminary"
    assert records["products"][0]["数据完整性"] == "初步数据"
