from datetime import date
import importlib.util
import json
from pathlib import Path

from website_analytics.dashboard_sync import cache_path_for_period
from website_analytics.periods import AnalyticsPeriod


script = Path(__file__).resolve().parents[1] / "scripts" / "review_landing_urls.py"
spec = importlib.util.spec_from_file_location("review_landing_urls", script)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


def test_cache_join_keeps_original_request_targets(tmp_path):
    day = date(2026, 10, 7)
    cache = cache_path_for_period(tmp_path, "genemedi-net", "ga4", AnalyticsPeriod("day", day, day))
    cache.parent.mkdir(parents=True)
    paths = ["/i/Ab", "/i/Ab?utm_source=campaign", "/i/aB", "//i/aav-packaging", "/i/X%2Fy/"]
    cache.write_text(json.dumps({"GA4 Pages": [
        {"landingPagePlusQueryString": path, "sessions": 1} for path in paths
    ]}))
    counts, missing = review.collect_variants({"/i/ab", "/aav-packaging", "/i/x/y"}, tmp_path, day, date(2026, 10, 8))
    assert counts == {
        ("/i/ab", "/i/Ab"): 2, ("/i/ab", "/i/aB"): 1,
        ("/aav-packaging", "//i/aav-packaging"): 1, ("/i/x/y", "/i/X%2Fy/"): 1,
    }
    assert missing == ["2026-10-08"]


def test_404_requires_final_http_evidence(monkeypatch):
    statuses = {
        review.ORIGIN + "/i/ABC": (301, "/i/Abc"),
        review.ORIGIN + "/i/Abc": (200, None),
        review.ORIGIN + "/i/abc": (404, None),
        review.ORIGIN + "/old": (302, "/i/abc"),
        review.ORIGIN + "/other": (301, "https://example.org/page"),
        review.ORIGIN + "//i/aav-packaging": (200, None),
    }
    monkeypatch.setattr(review, "request_status", statuses.__getitem__)
    assert review.probe("/i/ABC") == {"hops": [
        {"path": "/i/ABC", "status": 301}, {"path": "/i/Abc", "status": 200}
    ], "result": "redirect_ok"}
    assert review.probe("/old")["result"] == "404"
    assert review.probe("/other")["result"] == "external_redirect"
    assert review.probe("//i/aav-packaging")["result"] == "ok"

    def timeout(url):
        raise TimeoutError()

    monkeypatch.setattr(review, "request_status", timeout)
    assert review.probe("/i/abc") == {"hops": [], "result": "unverified", "error": "TimeoutError"}
