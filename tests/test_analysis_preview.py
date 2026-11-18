"""Synthetic unit inputs only; no production connection or real customer rows."""
import copy

import pytest

from website_analytics.analysis.contracts import AnalysisError
from website_analytics.analysis.preview import PreviewDraft, build_preview_report


def draft_data():
    return {"contract": "batch-preview/v1", "site": "genemedi-net", "draft_id": "synthetic-preview",
        "state": "draft_unconfirmed", "raw_urls": ["https://www.genemedi.net/i/Mixed-Example"],
        "change": {"reference_date": "2026-10-13", "precision": "approximate_day", "timezone": None,
                   "description": None, "user_statement": "Approximately October 13; synthetic test only."}}


def fake_packet(draft):
    """Pretend aggregate transport envelope, solely to test the import boundary."""
    def ok(value): return {"status": "ok", "result": value}
    ga = ok([{"sessions": 3, "totalUsers": 2, "engagedSessions": 1}])
    gs = ok({"rows": [{"clicks": 1, "impressions": 9, "ctr": 1/9, "position": 12}], "truncated": False})
    inquiry = ok({"cohorts": {"submitted": {"storedSubmissions": 0}, "lowercase_variant": {"storedSubmissions": 0}}, "rows": [],
        "page_rows_truncated": False, "foreign_origin_group_count": 0, "database_unattributed_count": 4})
    scenarios = []
    for scenario in draft.scenarios():
        periods = {}
        for label in ("before", "after"):
            periods[label] = {**scenario[label], "ga4_submitted": ga, "inquiry": inquiry,
                "ga4_lowercase_variant": ok([]), "gsc_submitted": gs, "gsc_lowercase_variant": ok({"rows": [], "truncated": False}),
                "ga4_pages": ok([{"landingPage": "/i/Mixed-Example", "sessions": 3, "totalUsers": 2}])}
        scenarios.append({"offset_days": scenario["offset_days"], "periods": periods})
    return {"protocol": "seo-preview-read/1", "sample": False, "exploratory": True, "batch_confirmed": False,
        "queried_at": "2026-10-28T07:00:00Z", "runtime": {"production_release": "synthetic-test-not-production"},
        "submitted_paths": list(draft.paths), "variant_paths_not_merged": ["/i/mixed-example"],
        "scenarios": scenarios, "writes": 0, "cache_or_audit_persisted": False,
        "broad_range": {"start": "2026-10-05", "end": "2026-10-21"},
        "gsc_page_days": ok({"rows": [], "truncated": False}), "ga4_page_days": ok([]),
        "cms": ok({"route_rows": [{"route_url": "i/Mixed-Example"}, {"route_url": "i/mixed-example"}]}),
        "query_metadata": {"ga4": [{"time_zone": "America/Los_Angeles", "subject_to_thresholding": False, "data_loss_from_other_row": False, "sampling": []}]}}


def test_approximate_draft_preserves_case_and_unknown_time_without_confirmation():
    draft = PreviewDraft.parse(draft_data())
    assert draft.document() == draft_data()
    assert draft.paths == ("/i/Mixed-Example",)
    assert draft.lookup_rows()[0]["legacy_classification_lookup_path"] == "/i/mixed-example"
    assert draft.document()["change"]["timezone"] is None
    assert "published_at" not in draft.document()
    scenarios = draft.scenarios()
    assert scenarios[1]["before"] == {"start": "2026-10-06", "end": "2026-10-12"}
    assert scenarios[1]["after"] == {"start": "2026-10-14", "end": "2026-10-20"}
    assert all(row["assumption_not_confirmed_date"] for row in scenarios)


@pytest.mark.parametrize("mutation", ["confirmed", "instant", "timezone", "foreign", "query", "nonstring"])
def test_preview_rejects_fabricated_precision_confirmation_or_unsafe_url(mutation):
    raw = draft_data()
    if mutation == "confirmed": raw["state"] = "confirmed"
    elif mutation == "instant": raw["change"]["precision"] = "exact_timestamp"
    elif mutation == "timezone": raw["change"]["timezone"] = "America/Los_Angeles"
    elif mutation == "foreign": raw["raw_urls"] = ["https://tarmart.com/i/x"]
    elif mutation == "query": raw["raw_urls"] = ["https://www.genemedi.net/i/x?secret=x"]
    else: raw["raw_urls"] = [{}]
    with pytest.raises(AnalysisError):
        PreviewDraft.parse(raw)


def test_report_retains_unconfirmed_state_and_never_merges_case_or_zero_fills_missing_rows():
    draft = PreviewDraft.parse(draft_data())
    report = build_preview_report(draft, fake_packet(draft), {"synthetic_test": True})
    assert report["status"] == "exploratory_unconfirmed" and report["effect_confirmed"] is False
    assert report["ordinary_traffic_target_visitors_undefined"] is True
    before = report["preview_main"]["periods"]["before"]
    assert before["ga4"]["totalUsers"] == 2
    assert before["lowercase_variant_not_merged"]["ga4"] is None
    assert before["lowercase_variant_not_merged"]["gsc_interval"] is None
    assert before["pages"][0]["exact_path"] == "/i/Mixed-Example"
    assert before["pages"][0]["gsc_page_day_sum"] is None
    assert len(before["pages"][0]["gsc_date_coverage"]["dates_without_returned_rows"]) == 7
    assert before["database_unattributed_not_assigned_to_batch"] == 4
    assert report["legacy_lookup_collisions_not_url_equivalence"][0]["invalid_broken_is_not_an_http_404_assertion"]


@pytest.mark.parametrize("mutation", ["sample", "dates", "scope", "foreign_inquiry"])
def test_import_verifies_identity_dates_and_suppresses_cross_site_inquiry(mutation):
    draft = PreviewDraft.parse(draft_data())
    packet = fake_packet(draft)
    if mutation == "sample": packet["sample"] = True
    elif mutation == "dates": packet["scenarios"][1]["periods"]["before"]["start"] = "2026-10-01"
    elif mutation == "scope": packet["submitted_paths"] = ["/i/mixed-example"]
    else:
        packet["scenarios"][1]["periods"]["before"]["inquiry"]["result"]["foreign_origin_group_count"] = 1
        report = build_preview_report(draft, packet, {})
        assert report["preview_main"]["periods"]["before"]["inquiry_linked_to_submitted_urls"] is None
        return
    with pytest.raises(AnalysisError):
        build_preview_report(draft, packet, {})


def test_import_checks_gsc_truncation_instead_of_calling_missing_rows_zero():
    draft = PreviewDraft.parse(draft_data())
    packet = fake_packet(draft)
    packet["gsc_page_days"]["result"]["truncated"] = True
    packet["scenarios"][1]["periods"]["after"]["gsc_submitted"]["result"]["truncated"] = True
    report = build_preview_report(draft, packet, {})
    assert report["preview_main"]["periods"]["after"]["gsc_interval"] is None
    assert report["source_quality"]["gsc_page_days_truncated"] is True
