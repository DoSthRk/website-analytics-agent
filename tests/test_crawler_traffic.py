from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from website_analytics import crawler_traffic
from website_analytics.crawler_traffic import (
    CrawlerRule,
    crawler_sessions,
    fingerprint_days,
    load_crawler_rule,
    load_fingerprint_days,
    monthly_summary,
    save_fingerprint_days,
)

RULE = CrawlerRule("t", window_days=7, min_sessions=30, max_engaged_rate=0.05)
BOT = ("Direct", "Singapore", "Singapore", "1280x1200", "Chrome", "Windows")
HUMAN = ("Organic Search", "Germany", "Berlin", "1920x1080", "Chrome", "Windows")
DAY0 = date(2026, 11, 1)


def _doc(day, site, product, version="4"):
    """site: {fingerprint: (sessions, engaged)}; product: {(line, fingerprint): sessions}."""
    return {
        "data_date": day.isoformat(),
        "mapping_version": version,
        "fingerprints": [[*key, s, e] for key, (s, e) in site.items()],
        "product": [[line, *key, s, 0] for (line, key), s in product.items()],
    }


def test_fingerprint_is_flagged_on_its_trailing_window_only():
    # 10 bot sessions a day with no engagement: 30 reached on the third day.
    docs = {
        (DAY0 + timedelta(days=i)).isoformat(): _doc(
            DAY0 + timedelta(days=i), {BOT: (10, 0), HUMAN: (40, 25)}, {("A", BOT): 6, ("A", HUMAN): 20})
        for i in range(4)
    }
    result = crawler_sessions(docs, [DAY0 + timedelta(days=i) for i in range(4)], RULE, mapping_version="4")
    assert [result[DAY0 + timedelta(days=i)].get("A", 0) for i in range(4)] == [0, 0, 6, 6]


def test_old_days_leave_the_window():
    days = [DAY0 + timedelta(days=i) for i in range(10)]
    docs = {day.isoformat(): _doc(day, {BOT: (40 if i == 0 else 0, 0)}, {("A", BOT): 40 if i == 0 else 1})
            for i, day in enumerate(days)}
    result = crawler_sessions(docs, days, RULE, mapping_version="4")
    # Flagged while day 0 is inside the 7-day window (days 0-6), not after.
    assert [result[day].get("A", 0) for day in days] == [40, 1, 1, 1, 1, 1, 1, 0, 0, 0]


def test_engaged_fingerprints_are_never_flagged():
    docs = {DAY0.isoformat(): _doc(DAY0, {HUMAN: (500, 300)}, {("A", HUMAN): 200})}
    assert crawler_sessions(docs, [DAY0], RULE, mapping_version="4") == {DAY0: {}}


def test_missing_day_and_other_mapping_are_refused():
    docs = {DAY0.isoformat(): _doc(DAY0, {}, {})}
    with pytest.raises(ValueError, match="missing 1 days"):
        crawler_sessions(docs, [DAY0, DAY0 + timedelta(days=1)], RULE, mapping_version="4")
    with pytest.raises(ValueError, match="product mapping"):
        crawler_sessions(docs, [DAY0], RULE, mapping_version="5")


def test_report_rows_become_daily_documents_without_paths(monkeypatch, tmp_path):
    rule = SimpleNamespace(include_in_product_report=True, report_line_id="A")
    monkeypatch.setattr(crawler_traffic, "match_product_rule",
                        lambda mapping, path, page_class, template: rule if path.startswith("/product") else None)
    dimension = SimpleNamespace(version="pc4", classify=lambda path: SimpleNamespace(
        page_class="product_page" if path.startswith("/product") else "information_page", template=""))
    mapping = SimpleNamespace(version="4")

    def row(day, path, key, sessions, engaged):
        return {"date": day, "landingPagePlusQueryString": path, "sessions": sessions, "engagedSessions": engaged,
                **dict(zip(crawler_traffic.FINGERPRINT_DIMENSIONS, key))}

    rows = [row("2026-11-01", "/product/x?utm=1", BOT, 5, 0), row("2026-11-01", "/Product/X/", BOT, 3, 0),
            row("2026-11-01", "/news/y", BOT, 7, 0), row("2026-11-01", "/product/z", HUMAN, 4, 3)]
    docs = fingerprint_days(rows, days=[DAY0, DAY0 + timedelta(days=1)], mapping=mapping, page_dimension=dimension,
                            ga4_metadata={"sampled": False})
    first = docs["2026-11-01"]
    assert sorted(map(tuple, first["fingerprints"])) == sorted([(*BOT, 15, 0), (*HUMAN, 4, 3)])
    assert sorted(map(tuple, first["product"])) == sorted([("A", *BOT, 8, 0), ("A", *HUMAN, 4, 3)])
    assert docs["2026-11-02"]["fingerprints"] == [] and first["page_classification_version"] == "pc4"
    assert "/product" not in str(docs)

    save_fingerprint_days(tmp_path, docs, datetime(2026, 11, 26, 12, 0))
    loaded = load_fingerprint_days(tmp_path)
    assert set(loaded) == {"2026-11-01", "2026-11-02"} and loaded["2026-11-01"]["fetched_at"] == "2026-11-26T12:00:00"

    with pytest.raises(ValueError, match="outside the requested days"):
        fingerprint_days([row("2026-10-31", "/product/x", BOT, 1, 0)], days=[DAY0], mapping=mapping,
                         page_dimension=dimension)


def test_monthly_summary_reports_share():
    docs = {DAY0.isoformat(): _doc(DAY0, {BOT: (90, 0)}, {("A", BOT): 90, ("A", HUMAN): 10})}
    crawler = crawler_sessions(docs, [DAY0], RULE, mapping_version="4")
    assert monthly_summary(docs, crawler) == [
        {"month": "2026-11", "days": 1, "product_sessions": 100, "suspected_crawler": 90, "share": 0.9}]


def test_repository_rule_loads_and_rejects_other_sites(tmp_path):
    rule = load_crawler_rule("config/crawler_rules/genemedi-net.yaml", "genemedi-net")
    assert (rule.window_days, rule.min_sessions, rule.max_engaged_rate) == (28, 30, 0.05)
    with pytest.raises(ValueError, match="another site"):
        load_crawler_rule("config/crawler_rules/genemedi-net.yaml", "other")
    bad = tmp_path / "rule.yaml"
    bad.write_text("site: genemedi-net\nversion: x\nwindow_days: 3\nmin_sessions: 30\nmax_engaged_rate: 0.05\n")
    with pytest.raises(ValueError, match="7-62 day window"):
        load_crawler_rule(bad, "genemedi-net")
