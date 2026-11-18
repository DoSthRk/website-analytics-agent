"""Synthetic fixtures for uncertain continuous intervention and weekly imports."""
import copy
import runpy
from pathlib import Path

import pytest

from test_analysis_preview import draft_data as event_data
from website_analytics.analysis.contracts import AnalysisError
from website_analytics.analysis.continuous import ContinuousDraft, build_weekly_report, parse_intervention, reinterpretation_notice
from website_analytics.analysis.preview import PreviewDraft


def draft_data():
    return {"contract": "batch-preview/v2", "site": "genemedi-net", "draft_id": "synthetic-continuous",
        "state": "draft_unconfirmed", "raw_urls": ["https://www.genemedi.net/i/Mixed-Example"],
        "change": {"type": "continuous", "start": {"precision": "approximate_month_part", "year_month": "2026-09", "part": "early", "timezone": None},
            "end": {"precision": "unknown", "date": None, "timezone": None},
            "stages": [{"kind": kind, "order": n, "start_date": None, "end_date": None} for n, kind in enumerate(("title", "body", "faq"), 1)],
            "prior_completion_estimate": {"date": "2026-10-13", "precision": "approximate_day", "status": "not_confirmed_completion_or_single_intervention"},
            "user_statement": "Synthetic test: continuous changes in early September, stage dates unknown."}}


def packet(draft):
    def ok(value): return {"status": "ok", "result": value}
    weeks = []
    for row in draft.weeks("2026-08-24", "2026-09-13"):
        weeks.append({**row, "ga4_submitted": ok([{"sessions": 3, "totalUsers": 2}]),
            "ga4_pages": ok([{"landingPage": "/i/Mixed-Example", "sessions": 3, "totalUsers": 2}]),
            "gsc_submitted": ok({"rows": [{"clicks": 0, "impressions": 5, "position": 10, "ctr": 0}], "truncated": False}),
            "inquiry": ok({"cohorts": {"submitted": {"storedSubmissions": 0}}, "rows": [], "page_rows_truncated": False,
                "foreign_origin_group_count": 0, "database_unattributed_count": 4})})
    return {"protocol": "seo-weekly-read/1", "sample": False, "exploratory": True, "batch_confirmed": False,
        "queried_at": "2026-10-28T09:00:00Z", "runtime": {"production_release": "synthetic-test"},
        "submitted_paths": list(draft.paths), "variant_paths_not_merged": ["/i/mixed-example"], "weeks": weeks,
        "gsc_availability": ok({"first_incomplete_date": None}), "skipped_weeks": [],
        "gsc_page_days": ok({"rows": [], "truncated": False}), "query_metadata": {"ga4": [{"time_zone": "America/Los_Angeles"}]},
        "writes": 0, "cache_or_audit_persisted": False}


def test_continuous_unknown_boundaries_and_stage_dates_round_trip_without_fabrication():
    draft = ContinuousDraft.parse(draft_data())
    assert draft.document() == draft_data()
    assert draft.paths == ("/i/Mixed-Example",)
    assert draft.document()["change"]["start"]["timezone"] is None
    assert all(stage["start_date"] is None and stage["end_date"] is None for stage in draft.document()["change"]["stages"])
    assert not hasattr(draft, "reference_date")


def test_single_event_v1_remains_compatible_and_not_rewritten_as_continuous():
    assert isinstance(parse_intervention(event_data()), PreviewDraft)
    assert parse_intervention(event_data()).document() == event_data()
    assert isinstance(parse_intervention(draft_data()), ContinuousDraft)


def test_full_august_weeks_candidate_and_boundary_week_transition_not_exact_start():
    draft = ContinuousDraft.parse(draft_data())
    weeks = draft.weeks("2026-08-03", "2026-10-25")
    assert len(weeks) == 12
    assert all(w["interpretation"] == "candidate_pre_optimization_not_verified" for w in weeks[:4])
    assert weeks[4]["interpretation"] == "approximate_start_transition"
    assert all(w["interpretation"] == "continuous_change_observation" for w in weeks[5:])
    for start, end in (("2026-08-01", "2026-10-25"), ("2026-08-03", "2026-10-24"), ("2026-01-05", "2026-10-25")):
        with pytest.raises(AnalysisError):
            draft.weeks(start, end)


@pytest.mark.parametrize("mutation", ["exact_start", "known_end", "stage_date", "endpoint_claim", "timezone", "foreign", "month"])
def test_rejects_fabricated_precision_or_scope(mutation):
    raw = draft_data()
    if mutation == "exact_start": raw["change"]["start"]["precision"] = "exact_date"
    elif mutation == "known_end": raw["change"]["end"]["date"] = "2026-10-13"
    elif mutation == "stage_date": raw["change"]["stages"][0]["start_date"] = "2026-09-01"
    elif mutation == "endpoint_claim": raw["change"]["prior_completion_estimate"]["status"] = "confirmed_completion"
    elif mutation == "timezone": raw["change"]["start"]["timezone"] = "America/Los_Angeles"
    elif mutation == "foreign": raw["raw_urls"] = ["https://tarmart.com/i/x"]
    else: raw["change"]["start"]["year_month"] = "2026-13"
    with pytest.raises(AnalysisError):
        ContinuousDraft.parse(raw)


def test_old_report_interpretation_is_superseded_without_changing_bytes_or_claiming_effect():
    draft = ContinuousDraft.parse(draft_data())
    hashes = ["a"*64]
    notice = reinterpretation_notice(draft, hashes)
    assert notice["prior_report_bytes_changed"] is False
    assert notice["stage_effects_identifiable"] is False
    assert "not_unoptimized_vs_optimized" in notice["prior_october_comparison_meaning"]
    assert notice["prior_reports_preserved"] == hashes


def test_weekly_report_retains_raw_case_unknown_days_and_weekly_unique_semantics():
    draft = ContinuousDraft.parse(draft_data())
    report = build_weekly_report(draft, packet(draft), {"actual_migration_time": "unknown"})
    assert report["effect_confirmed"] is False and report["stage_effects_identifiable"] is False
    row = report["weekly_rows"][0]
    assert row["ga4_cohort_weekly_interval"]["totalUsers"] == 2
    assert row["pages"][0]["exact_path"] == "/i/Mixed-Example"
    assert row["pages"][1]["ga4_weekly_interval"] is None
    assert row["pages"][1]["gsc_page_day_sum"] is None
    assert row["database_unattributed_not_assigned_to_batch"] == 4
    assert row["gsc_week_ends_before_observed_incomplete_date"] is None
    assert "weekly_users_are_interval_unique_do_not_sum_across_weeks_or_url_variants" in report["limitations"]


def test_bounded_metadata_recheck_uses_observed_cutoff_without_recollecting_history():
    draft = ContinuousDraft.parse(draft_data())
    raw = packet(draft)
    recheck = {"protocol": "seo-weekly-read/1", "sample": False, "writes": 0, "availability_only": True,
        "runtime": {"production_release": "synthetic-test"}, "gsc_availability": {"status": "ok", "result": {"first_incomplete_date": "2026-09-12"}}}
    report = build_weekly_report(draft, raw, {}, availability_recheck=recheck)
    assert report["weekly_rows"][0]["gsc_week_ends_before_observed_incomplete_date"] is True
    assert report["weekly_rows"][-1]["gsc_week_ends_before_observed_incomplete_date"] is False
    assert raw["gsc_availability"]["result"]["first_incomplete_date"] is None


def test_wrong_case_scope_gap_or_sample_packet_is_rejected():
    draft = ContinuousDraft.parse(draft_data())
    for mutation in ("scope", "gap", "sample"):
        raw = packet(draft)
        if mutation == "scope": raw["submitted_paths"] = ["/i/mixed-example"]
        elif mutation == "gap": del raw["weeks"][1]
        else: raw["sample"] = True
        with pytest.raises(AnalysisError):
            build_weekly_report(draft, raw, {})


def test_official_camel_case_gsc_metadata_is_read_and_conflicts_rejected():
    # Loading this helper defines functions only; no server/source connection.
    helper = runpy.run_path(str(Path(__file__).resolve().parents[1]/"scripts/seo_preview_server.py"))["gsc_incomplete_date"]
    assert helper({"firstIncompleteDate": "2026-10-27"}) == "2026-10-27"
    assert helper({"first_incomplete_date": "2026-10-27"}) == "2026-10-27"
    assert helper({}) is None
    with pytest.raises(ValueError):
        helper({"firstIncompleteDate": "2026-10-27", "first_incomplete_date": "2026-10-26"})
    with pytest.raises(ValueError):
        helper({"firstIncompleteDate": "not-a-date"})
