import re
from datetime import date, timedelta

import pytest

from website_analytics.period_facts import (
    COMPLETE,
    IN_PROGRESS,
    MISSING_DAYS,
    MONTH,
    WEEK,
    Exclusion,
    build_period_facts,
    load_exclusions,
    periods_between,
    product_scopes,
)
from website_analytics.product_mapping import ProductMapping, ReportLine

MAPPING = ProductMapping(
    site_key="genemedi-net",
    version="4",
    report_lines=(
        ReportLine("A", "Alpha", "X", "P"),
        ReportLine("B", "Beta", "X", "P"),
        ReportLine("C", "Gamma", "Y", "Q"),
    ),
    rules=(),
)
FIRST = date(2025, 7, 14)  # Monday, 2025-W29


def _funnel(day, line, **overrides):
    row = {
        "data_date": day.isoformat(),
        "product_line_id": line,
        "mapping_version": "4",
        "gsc_impressions": 100,
        "gsc_clicks": 10,
        "gsc_position_impression_sum": 500,
        "ga4_product_landing_sessions_all_channels": 20,
        "ga4_product_landing_engaged_sessions_all_channels": 10,
        "db_accepted_inquiries": 1,
    }
    row.update(overrides)
    return row


def _overview(day, **overrides):
    row = {
        "data_date": day.isoformat(),
        "page_classification_version": "4",
        "ga4_sessions": 100,
        "product_page_sessions": 60,
        "information_page_sessions": 30,
        "other_page_sessions": 10,
        "gsc_clicks": 30,
        "gsc_impressions": 300,
        "accepted_inquiries": 3,
    }
    row.update(overrides)
    return row


def _days(weeks, start=FIRST):
    return [start + timedelta(days=offset) for offset in range(weeks * 7)]


def _build(days, *, funnel_override=None, overview_override=None, exclusions=(), last_final=None, grains=(WEEK,),
           crawler=None):
    funnel_override = funnel_override or (lambda day, line: {})
    overview_override = overview_override or (lambda day: {})
    funnel = [_funnel(day, line, **funnel_override(day, line)) for day in days for line in ("A", "B", "C")]
    overview = [_overview(day, **overview_override(day)) for day in days]
    return build_period_facts(
        site="genemedi-net",
        mapping=MAPPING,
        funnel_daily=funnel,
        overview_daily=overview,
        last_final=last_final or days[-1],
        crawler={day: (crawler(day) if crawler else {}) for day in days},
        exclusions=exclusions,
        grains=grains,
    )


def _row(build, period, scope, mode, table="product_period"):
    return next(
        row
        for row in build.records[table]
        if row["period"] == period and row["scope_id"] == scope and row["comparison_mode"] == mode
    )


def test_periods_start_at_first_full_week_and_month_and_include_iso_week_53():
    weeks = periods_between(WEEK, date(2025, 7, 11), date(2027, 1, 3))
    months = periods_between(MONTH, date(2025, 7, 11), date(2025, 9, 30))

    assert weeks[0].key == "2025-W29"
    assert weeks[-1].key == "2026-W53"
    assert weeks[-1].start == date(2026, 12, 28) and weeks[-1].end == date(2027, 1, 3)
    assert [month.key for month in months] == ["2025-08", "2025-09"]


def test_scopes_keep_only_l2_groups_with_several_products():
    scopes = {scope.identifier: scope for scope in product_scopes(MAPPING)}

    assert set(scopes) == {"ALL_PRODUCTS", "L1:X", "L1:Y", "L2:X/P", "A", "B", "C"}
    assert scopes["L2:X/P"].members == {"A", "B"}


def test_group_named_like_a_leaf_gets_a_distinct_dashboard_name():
    mapping = ProductMapping(
        site_key="genemedi-net", version="4",
        report_lines=(ReportLine("A", "Alpha", "X", "P"), ReportLine("COVID19", "COVID-19", "COVID-19")),
        rules=(),
    )
    names = {scope.identifier: scope.name for scope in product_scopes(mapping)}

    assert names["L1:COVID-19"] == "COVID-19（一级分类）"
    assert names["COVID19"] == "COVID-19"
    assert names["L1:X"] == "X"


def test_ratios_are_sum_over_sum_not_average_of_daily_ratios():
    days = _days(1)
    # Day 1: 90 clicks / 100 impressions (90%); other days 10 / 100.
    build = _build(days, funnel_override=lambda day, line: {"gsc_clicks": 90} if day == days[0] else {})
    row = _row(build, "2025-W29", "A", "前8周均值")

    assert row["impressions"] == 700
    assert row["clicks"] == 150
    assert row["click_rate"] == pytest.approx(150 / 700, abs=1e-6)
    assert row["avg_position"] == pytest.approx(5.0)
    assert _row(build, "2025-W29", "ALL_PRODUCTS", "前8周均值")["landing_sessions"] == 420


def test_rolling_baseline_skips_excluded_week_and_reaches_back_for_eight():
    days = _days(10)
    spike = {day for day in days[35:42]}  # 2025-W34
    build = _build(
        days,
        funnel_override=lambda day, line: {"ga4_product_landing_sessions_all_channels": 2000} if day in spike else {},
        exclusions=[Exclusion(date(2025, 8, 18), date(2025, 8, 20), None, "爬虫流量")],
    )
    row = _row(build, "2025-W38", "A", "前8周均值")

    assert row["baseline_periods"] == 8
    assert row["baseline_landing_sessions"] == 140  # spike week not averaged in
    assert "跳过 2025-W34（排除）" in row["baseline_note"]
    assert row["baseline_note"].startswith("2025-W29…2025-W37")
    excluded = _row(build, "2025-W34", "A", "前8周均值")
    assert excluded["baseline_excluded"] is True
    assert excluded["is_anomaly"] is False


def test_rolling_baseline_is_empty_without_enough_eligible_periods():
    build = _build(_days(9))

    assert _row(build, "2025-W37", "A", "前8周均值")["baseline_clicks"] == 70
    row = _row(build, "2025-W37", "A", "前12周均值")
    assert row["baseline_clicks"] is None
    assert row["change_clicks"] is None
    assert row["baseline_note"] == "合格周期不足12期"


def test_missing_and_in_progress_periods_are_marked_and_not_used_as_baseline():
    days = [day for day in _days(10) if day != date(2025, 7, 30)]  # inside 2025-W31
    build = _build(days, last_final=days[-1] - timedelta(days=2))

    assert _row(build, "2025-W31", "A", "前8周均值")["period_status"] == MISSING_DAYS
    assert _row(build, "2025-W38", "A", "前8周均值")["period_status"] == IN_PROGRESS
    assert _row(build, "2025-W38", "A", "前8周均值")["change_clicks"] is None
    assert _row(build, "2025-W37", "A", "前8周均值")["baseline_clicks"] is None  # only 7 eligible


def test_year_over_year_uses_same_iso_week_and_skips_ineligible_weeks():
    days = _days(54)
    build = _build(
        days,
        funnel_override=lambda day, line: {"gsc_clicks": 20} if day.isocalendar()[0] == 2026 else {},
        exclusions=[Exclusion(date(2025, 7, 22), date(2025, 7, 22), frozenset({"A"}), "人工排除")],
    )
    same = _row(build, "2026-W29", "B", "去年同期")
    assert same["baseline_clicks"] == 70
    assert same["change_clicks"] == pytest.approx(1.0)
    assert "点击" in same["anomaly_items"]
    blocked = _row(build, "2026-W30", "A", "去年同期")
    assert blocked["baseline_clicks"] is None
    assert blocked["baseline_note"] == "2025-W30已排除，不作对照"
    assert _row(build, "2026-W30", "B", "去年同期")["baseline_clicks"] == 70


def test_anomaly_requires_thirty_percent_and_minimum_volume():
    days = _days(9)
    last = set(days[-7:])
    build = _build(
        days,
        funnel_override=lambda day, line: {"gsc_clicks": 14, "db_accepted_inquiries": 0} if day in last else {},
    )
    row = _row(build, "2025-W37", "A", "前8周均值")

    assert row["change_clicks"] == pytest.approx(0.4)
    assert row["change_accepted_inquiries"] == pytest.approx(-1.0)
    assert row["is_anomaly"] is True
    assert "Google 自然搜索点击" in row["anomaly_items"]
    assert "官网表单入库数（非隔离）" in row["anomaly_items"]  # baseline 7 >= 5
    # Same relative change on a tiny base is not an anomaly.
    small = _build(
        days,
        funnel_override=lambda day, line: {"gsc_clicks": 1 if day not in last else 2} if line == "C" else {},
    )
    tiny = _row(small, "2025-W37", "C", "前8周均值")
    assert tiny["change_clicks"] == pytest.approx(1.0)
    assert "Google 自然搜索点击" not in tiny["anomaly_items"]


def test_change_table_has_latest_complete_week_in_long_form():
    build = _build(_days(10), last_final=_days(10)[-1] - timedelta(days=1))
    rows = build.records["product_change"]

    assert len(rows) == 7 * 3 * 10
    assert {row["period"] for row in rows} == {"2025-W37"}
    assert len({row["change_key"] for row in rows}) == len(rows)
    assert all("2025-W" not in row["change_key"] for row in rows)
    first = sorted((row for row in rows if row["scope_id"] == "A" and row["comparison_mode"] == "前8周均值"), key=lambda r: r["item_order"])
    assert [row["item"] for row in first][:3] == ["1. Google 搜索曝光", "2. 搜索点击率（%）", "3. Google 自然搜索点击"]
    assert first[1]["current"] == pytest.approx(10.0)  # 10 clicks / 100 impressions, in percent points


def test_site_table_and_candidates_flag_bot_like_weeks():
    days = _days(10)
    bot = set(days[56:63])  # 2025-W37
    build = _build(
        days,
        funnel_override=lambda day, line: {"ga4_product_landing_sessions_all_channels": 200} if day in bot and line == "A" else {},
        overview_override=lambda day: {"ga4_sessions": 1000, "other_page_sessions": 910} if day in bot else {},
    )
    site = _row(build, "2025-W37", "SITE", "前8周均值", table="site_period")
    assert site["sessions"] == 7000
    assert site["baseline_sessions"] == 700
    flagged = {(item["period"], item["scope_id"]) for item in build.candidates}
    assert ("2025-W37", "A") in flagged
    assert ("2025-W37", "SITE") in flagged
    assert ("2025-W37", "C") not in flagged
    metrics = {item["metric"] for item in build.candidates if item["scope_id"] == "SITE"}
    assert metrics == {"sessions", "other_page_sessions"}


def test_month_rows_use_month_modes():
    build = _build(_days(30), grains=(MONTH,))
    modes = {row["comparison_mode"] for row in build.records["product_period"]}

    assert modes == {"前3月均值", "前6月均值", "去年同月"}
    assert _row(build, "2025-11", "A", "前3月均值")["baseline_periods"] == 3


def test_mixed_mapping_versions_are_rejected():
    days = _days(1)
    funnel = [_funnel(day, line) for day in days for line in ("A", "B", "C")]
    funnel[0]["mapping_version"] = "3"
    with pytest.raises(ValueError, match="mix mapping_version"):
        build_period_facts(
            site="genemedi-net",
            mapping=MAPPING,
            funnel_daily=funnel,
            overview_daily=[_overview(day) for day in days],
            last_final=days[-1],
            crawler={day: {} for day in days},
        )


def test_load_exclusions_validates_site_and_reason(tmp_path):
    path = tmp_path / "exclusions.yaml"
    path.write_text(
        "version: '1'\nsite: genemedi-net\nexclusions:\n"
        "  - {start: 2026-08-31, end: 2026-09-05, scope: all, reason: 爬虫流量}\n"
        "  - {start: 2026-05-18, end: 2026-05-24, scope: [A, L1:X], reason: 人工排除}\n",
        encoding="utf-8",
    )
    first, second = load_exclusions(path, "genemedi-net")
    assert first.scopes is None and first.start == date(2026, 8, 31)
    assert second.scopes == {"A", "L1:X"}

    with pytest.raises(ValueError, match="another site"):
        load_exclusions(path, "other-site")
    path.write_text("site: genemedi-net\nexclusions:\n  - {start: 2026-08-31, end: 2026-09-05}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="reason"):
        load_exclusions(path, "genemedi-net")


def test_repository_exclusions_load():
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "config" / "baseline_exclusions" / "genemedi-net.yaml"
    exclusions = load_exclusions(path, "genemedi-net")
    assert exclusions and all(item.reason for item in exclusions)


def test_session_counts_are_shown_but_never_alert():
    days = _days(9)
    last = set(days[-7:])
    build = _build(
        days,
        funnel_override=lambda day, line: {"ga4_product_landing_sessions_all_channels": 200} if day in last else {},
        overview_override=lambda day: {"ga4_sessions": 1000, "other_page_sessions": 910} if day in last else {},
    )
    product = _row(build, "2025-W37", "A", "前8周均值")
    assert product["change_landing_sessions"] == pytest.approx(9.0)
    assert product["change_engaged_rate"] == pytest.approx(-0.9)
    assert product["is_anomaly"] is False
    site = _row(build, "2025-W37", "SITE", "前8周均值", table="site_period")
    assert site["change_sessions"] == pytest.approx(9.0)
    assert site["is_anomaly"] is False

    change = {re.sub(r"^(\d+\. |参考 · )", "", row["item"]): row for row in build.records["product_change"]
              if row["scope_id"] == "A" and row["comparison_mode"] == "前8周均值"}
    assert change["产品落地会话（全部渠道）"]["alert_enabled"] is False
    assert "含爬虫" in change["产品落地会话（全部渠道）"]["note"]
    assert change["产品落地会话（剔除疑似爬虫）"]["alert_enabled"] is False
    assert change["产品落地有效会话"]["alert_enabled"] is True


def test_engaged_sessions_still_alert():
    days = _days(9)
    last = set(days[-7:])
    build = _build(days, funnel_override=lambda day, line: {"ga4_product_landing_engaged_sessions_all_channels": 5} if day in last else {})
    row = _row(build, "2025-W37", "A", "前8周均值")

    assert row["change_engaged_sessions"] == pytest.approx(-0.5)
    assert "产品落地有效会话" in row["anomaly_items"]
    assert "有效会话率" not in row["anomaly_items"]


def test_ratio_alert_needs_numerator_volume_too():
    days = _days(9)
    last = set(days[-7:])
    # C has 1 click a day (base 7/week): its click rate doubling must not alert,
    # even though its impressions base (700) is large.
    build = _build(days, funnel_override=lambda day, line: {"gsc_clicks": 2 if day in last else 1} if line == "C" else {})
    row = _row(build, "2025-W37", "C", "前8周均值")

    assert row["change_click_rate"] == pytest.approx(1.0)
    assert row["is_anomaly"] is False


def test_crawler_sessions_are_removed_and_the_session_ratios_use_the_clean_count():
    days = _days(9)
    last = set(days[-7:])
    # In the last week the fingerprint report says 75% of A's and all of B's
    # product landing sessions are crawlers; the share applies to item 5.
    build = _build(days, crawler=lambda day: {"A": 0.75, "B": 1.0} if day in last else {})
    a = _row(build, "2025-W37", "A", "前8周均值")
    assert a["landing_sessions"] == 140 and a["clean_landing_sessions"] == 35
    assert a["baseline_clean_landing_sessions"] == 140
    assert a["arrival_ratio"] == pytest.approx(35 / 70)
    assert a["engaged_rate"] == pytest.approx(70 / 35)
    assert a["is_anomaly"] is False  # the cleaned count and its ratios do not alert yet
    assert _row(build, "2025-W37", "B", "前8周均值")["clean_landing_sessions"] == 0

    change = sorted((row for row in build.records["product_change"]
                     if row["scope_id"] == "A" and row["comparison_mode"] == "前8周均值"), key=lambda r: r["item_order"])
    assert [row["item"] for row in change][4] == "5. 产品落地会话（剔除疑似爬虫）"
    assert change[-1]["item"] == "参考 · 产品落地会话（全部渠道）" and change[-1]["item_order"] == 10


def test_every_day_needs_a_crawler_estimate():
    days = _days(2)
    funnel = [_funnel(day, line) for day in days for line in ("A", "B", "C")]
    with pytest.raises(ValueError, match="no crawler estimate for 13 days"):
        build_period_facts(site="genemedi-net", mapping=MAPPING, funnel_daily=funnel,
                           overview_daily=[_overview(day) for day in days], last_final=days[-1],
                           crawler={days[0]: {}})
    with pytest.raises(ValueError, match="unknown product lines"):
        build_period_facts(site="genemedi-net", mapping=MAPPING, funnel_daily=funnel,
                           overview_daily=[_overview(day) for day in days], last_final=days[-1],
                           crawler={day: {"Z": 0.5} for day in days})
    with pytest.raises(ValueError, match="out of range"):
        build_period_facts(site="genemedi-net", mapping=MAPPING, funnel_daily=funnel,
                           overview_daily=[_overview(day) for day in days], last_final=days[-1],
                           crawler={day: {"A": 1.5} for day in days})
