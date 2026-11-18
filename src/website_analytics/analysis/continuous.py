"""Continuous interventions with uncertain boundaries, compatible with v1 cases."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from website_analytics.analysis.contracts import AnalysisError, calendar_date, fingerprint, identifier, metric_number, page_path, strict_keys, text
from website_analytics.analysis.preview import PreviewDraft


@dataclass(frozen=True)
class ContinuousDraft:
    draft_id: str
    raw_urls: tuple[str, ...]
    start_year_month: str
    start_month_part: str
    prior_completion_estimate: str | None
    user_statement: str

    @classmethod
    def parse(cls, value: object) -> ContinuousDraft:
        raw = strict_keys(value, {"contract", "site", "draft_id", "state", "raw_urls", "change"})
        if raw["contract"] != "batch-preview/v2" or raw["site"] != "genemedi-net" or raw["state"] != "draft_unconfirmed":
            raise AnalysisError("unsupported continuous draft")
        urls = raw["raw_urls"]
        if not isinstance(urls, list) or not 1 <= len(urls) <= 20 or any(not isinstance(u, str) for u in urls) or len(set(urls)) != len(urls):
            raise AnalysisError("invalid continuous URL set")
        for url in urls:
            page_path(url)  # Validate only; retain exact URL identity/case.
            if urlsplit(url).scheme != "https":
                raise AnalysisError("full website URL required")
        change = strict_keys(raw["change"], {"type", "start", "end", "stages", "prior_completion_estimate", "user_statement"})
        start = strict_keys(change["start"], {"precision", "year_month", "part", "timezone"})
        end = strict_keys(change["end"], {"precision", "date", "timezone"})
        if change["type"] != "continuous" or start["precision"] != "approximate_month_part" or not isinstance(start["year_month"], str) or not re.fullmatch(r"\d{4}-\d{2}", start["year_month"]):
            raise AnalysisError("approximate start month required")
        calendar_date(start["year_month"]+"-01")
        if start["part"] not in {"early", "middle", "late", "unknown"} or start["timezone"] is not None or end != {"precision": "unknown", "date": None, "timezone": None}:
            raise AnalysisError("unknown dates and timezone cannot be invented")
        expected_stages = [{"kind": kind, "order": n, "start_date": None, "end_date": None} for n, kind in enumerate(("title", "body", "faq"), 1)]
        if change["stages"] != expected_stages:
            raise AnalysisError("only the user-reported stage order with unknown dates is supported")
        estimate = change["prior_completion_estimate"]
        if estimate is not None:
            strict_keys(estimate, {"date", "precision", "status"})
            if estimate["precision"] != "approximate_day" or estimate["status"] != "not_confirmed_completion_or_single_intervention":
                raise AnalysisError("prior estimate cannot become a confirmed endpoint")
            estimate = calendar_date(estimate["date"]).isoformat()
        return cls(identifier(raw["draft_id"]), tuple(urls), start["year_month"], start["part"], estimate, text(change["user_statement"], limit=1000))

    @property
    def paths(self):
        return tuple(urlsplit(url).path for url in self.raw_urls)

    def document(self):
        return {"contract": "batch-preview/v2", "site": "genemedi-net", "draft_id": self.draft_id,
            "state": "draft_unconfirmed", "raw_urls": list(self.raw_urls),
            "change": {"type": "continuous", "start": {"precision": "approximate_month_part", "year_month": self.start_year_month,
                "part": self.start_month_part, "timezone": None}, "end": {"precision": "unknown", "date": None, "timezone": None},
                "stages": [{"kind": kind, "order": n, "start_date": None, "end_date": None} for n, kind in enumerate(("title", "body", "faq"), 1)],
                "prior_completion_estimate": {"date": self.prior_completion_estimate, "precision": "approximate_day", "status": "not_confirmed_completion_or_single_intervention"} if self.prior_completion_estimate else None,
                "user_statement": self.user_statement}}

    @property
    def digest(self):
        return fingerprint(self.document())

    def weeks(self, start: str, end: str) -> list[dict]:
        first, last = calendar_date(start), calendar_date(end)
        if first.weekday() != 0 or last.weekday() != 6 or first > last or (last-first).days > 111:
            raise AnalysisError("use at most 16 full Monday-Sunday weeks")
        month_start = calendar_date(self.start_year_month+"-01")
        result = []
        current = first
        while current <= last:
            finish = current+timedelta(days=6)
            label = "candidate_pre_optimization_not_verified" if finish < month_start else (
                "approximate_start_transition" if current < month_start <= finish else "continuous_change_observation")
            result.append({"start": current.isoformat(), "end": finish.isoformat(), "interpretation": label})
            current += timedelta(days=7)
        return result


def period_labels(draft, periods, *, open_intervals=False,as_of=None):
    if not open_intervals:return draft.weeks(periods[0].start.isoformat(),periods[-1].end.isoformat())
    month_start=calendar_date(draft.start_year_month+"-01")
    result=[]
    for period in periods:
        label="candidate_pre_optimization_not_verified" if period.end<month_start else ("approximate_start_transition" if period.start<month_start<=period.end else "continuous_change_observation")
        result.append({"start":period.start.isoformat(),"end":period.end.isoformat(),"interpretation":label,"window_kind":"completed_week" if period.end.weekday()==6 and (as_of is None or period.end<as_of.astimezone(ZoneInfo("America/Los_Angeles")).date()) else "open_week"})
    return result


def parse_intervention(value: object):
    if isinstance(value, dict) and value.get("contract") == "batch-preview/v2":
        return ContinuousDraft.parse(value)
    return PreviewDraft.parse(value)


def reinterpretation_notice(draft: ContinuousDraft, prior_report_digests: list[str]) -> dict:
    if any(not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in prior_report_digests):
        raise AnalysisError("invalid prior report digest")
    return {"contract": "analysis-interpretation-update/v1", "current_intervention": draft.document(),
        "prior_reports_preserved": prior_report_digests, "prior_report_bytes_changed": False,
        "prior_october_comparison_meaning": "not_unoptimized_vs_optimized_october6_12_after_reported_start_october14_20_stability_unconfirmed",
        "october_13_is_not_a_verified_single_intervention_or_completion": True,
        "stage_effects_identifiable": False}


def build_weekly_report(draft: ContinuousDraft, packet: object, url_state: object, *, availability_recheck: dict | None = None) -> dict:
    from website_analytics.analysis.preview import _interval_metrics, _page_day_metrics, _page_day_coverage
    raw = strict_keys(packet, {"protocol", "sample", "exploratory", "batch_confirmed", "queried_at", "runtime", "submitted_paths", "variant_paths_not_merged", "weeks", "gsc_availability", "skipped_weeks", "gsc_page_days", "query_metadata", "writes", "cache_or_audit_persisted"}, {"worker_window_evidence","worker_captured_at","worker_source_ids","worker_observations","worker_metadata_diagnostics","selection_timezone"})
    if raw["protocol"] not in {"seo-weekly-read/1","seo-period-read/2"} or raw["sample"] is not False or raw["exploratory"] is not True or raw["batch_confirmed"] is not False or raw["writes"] != 0 or raw["cache_or_audit_persisted"] is not False or raw["submitted_paths"] != list(draft.paths):
        raise AnalysisError("weekly evidence does not match this continuous unconfirmed case")
    weeks = raw["weeks"]
    if not isinstance(weeks, list) or not 1 <= len(weeks) <= 16:
        raise AnalysisError("invalid bounded weekly response")
    v2=raw["protocol"]=="seo-period-read/2"
    if v2:
        from website_analytics.analysis.worker import parse_request
        periods_input={"contract":"bounded-worker/v2","site":"genemedi-net","raw_urls":list(draft.raw_urls),"variant_paths":raw["variant_paths_not_merged"],"weeks":[{k:r[k] for k in ("start","end","window_kind")} for r in weeks],"selection_timezone":raw.get("selection_timezone")}
        from website_analytics.analysis.contracts import timestamp
        _,_,periods=parse_request(periods_input,as_of=timestamp(raw["queried_at"]))
        expected=period_labels(draft,periods,open_intervals=True,as_of=timestamp(raw["queried_at"]))
    else:
        if "selection_timezone" in raw:raise AnalysisError("legacy report cannot change interval semantics")
        expected=draft.weeks(weeks[0]["start"],weeks[-1]["end"])
    if len(expected) != len(weeks):
        raise AnalysisError("weekly response has unreported gaps")
    availability = raw["gsc_availability"]
    if availability_recheck is not None:
        if availability_recheck.get("protocol") != "seo-weekly-read/1" or availability_recheck.get("sample") is not False or availability_recheck.get("writes") != 0 or availability_recheck.get("availability_only") is not True or availability_recheck["runtime"]["production_release"] != raw["runtime"]["production_release"]:
            raise AnalysisError("invalid scoped freshness recheck")
        availability = availability_recheck["gsc_availability"]
    cutoff = availability.get("result", {}).get("first_incomplete_date") if availability.get("status") == "ok" else None
    if cutoff is not None:
        calendar_date(cutoff)
    rows = []
    for requested, observed in zip(expected, weeks, strict=True):
        if any(observed[key] != requested[key] for key in (("start","end","interpretation","window_kind") if v2 else ("start","end","interpretation"))):
            raise AnalysisError("weekly source dates or interpretation mismatch")
        period = {"start": requested["start"], "end": requested["end"]}
        ga = _interval_metrics(observed["ga4_submitted"], "ga4")
        gsc = _interval_metrics(observed["gsc_submitted"], "gsc")
        iq = observed["inquiry"]
        iq_result = iq.get("result", {})
        iq_ready = iq.get("status") == "ok" and not iq_result.get("page_rows_truncated") and not iq_result.get("foreign_origin_group_count")
        pages = []
        ga_rows = observed["ga4_pages"].get("result", []) if observed["ga4_pages"].get("status") == "ok" else []
        for path in (*draft.paths, *raw["variant_paths_not_merged"]):
            matching = [row for row in ga_rows if row.get("landingPage", "").rstrip("/") == path]
            ga_metrics = {key: metric_number(value) for key, value in matching[0].items() if key != "landingPage"} if len(matching) == 1 else None
            inquiry = None
            if iq_ready:
                inquiry = {key: sum(metric_number(row[key]) for row in iq_result["rows"] if row["exact_path"] == path) for key in ("storedSubmissions", "quarantinedSubmissions", "nonQuarantinedSubmissions")}
            pages.append({"exact_path": path, "submitted": path in draft.paths,
                "ga4_weekly_interval": ga_metrics, "gsc_page_day_sum": _page_day_metrics(raw["gsc_page_days"], period, (path,)),
                "gsc_date_coverage": _page_day_coverage(raw["gsc_page_days"], period, path), "inquiry_linked_records": inquiry})
        gsc_pages = _page_day_metrics(raw["gsc_page_days"], period, draft.paths)
        rows.append({**requested, "ga4_cohort_weekly_interval": ga, "gsc_cohort_weekly_interval": gsc,
            "gsc_week_ends_before_observed_incomplete_date": requested["end"] < cutoff if cutoff is not None else None,
            "inquiry_linked_to_submitted_urls": iq_result["cohorts"]["submitted"] if iq_ready else None,
            "source_status": {source: observed[key]["status"] for source, key in (("ga4", "ga4_submitted"), ("gsc", "gsc_submitted"), ("inquiry", "inquiry"))},
            "pages": pages,
            "reconciliation": {"ga4_page_sessions_sum": sum(metric_number(row["sessions"]) for row in ga_rows if row.get("landingPage", "").rstrip("/") in draft.paths) if observed["ga4_pages"].get("status") == "ok" else None,
                "ga4_interval_users_independent_not_summed": True,
                "gsc_interval_matches_page_day_counts": all(gsc[key] == gsc_pages[key] for key in ("clicks", "impressions")) if gsc is not None and gsc_pages is not None else None},
            "database_unattributed_not_assigned_to_batch": iq_result.get("database_unattributed_count") if iq_ready else None})
    report = {"contract": "continuous-trend-report/v1", "sample": False, "status": "exploratory_continuous_change",
        "batch_confirmed": False, "intervention": draft.document(), "draft_digest_for_review_not_confirmation": draft.digest,
        "source_packet_digest": fingerprint(packet), "queried_at": raw["queried_at"], "production_release": raw["runtime"]["production_release"],
        "weekly_rows": rows, "gsc_availability": availability, "skipped_weeks": raw["skipped_weeks"],
        "availability_recheck_digest": fingerprint(availability_recheck) if availability_recheck is not None else None,
        "url_state_observation": url_state, "variant_paths_not_merged": raw["variant_paths_not_merged"],
        "source_quality": {"ga4_property_timezones": sorted({m["time_zone"] for m in raw["query_metadata"].get("ga4", []) if m.get("time_zone")}),
            "ga4_finality": "not_independently_verified", "gsc_weekly_data_state": "final",
            "gsc_page_day_truncated": raw["gsc_page_days"].get("result", {}).get("truncated"),
            "database_calendar_and_dedicated_scope": "not_independently_verified"},
        "prior_october_windows_meaning": "within_continuous_modification_period_not_unoptimized_baseline",
        "stage_effects_identifiable": False, "effect_confirmed": False,
        "limitations": ["august_weeks_are_candidate_baseline_not_verified_unchanged_or_same_page_lifecycle",
            "early_september_start_has_no_exact_day_or_timezone", "stage_order_only_title_then_body_then_faq_no_stage_dates",
            "completion_date_unknown_october13_estimate_not_a_single_event", "cannot_identify_stage_contributions_or_causal_effect",
            "weekly_users_are_interval_unique_do_not_sum_across_weeks_or_url_variants",
            "missing_rows_not_zero_filled_gsc_privacy_and_low_volume", "source_calendars_not_rebucketed_or_conflated",
            "third_party_redirect_proposal_not_authorization_or_completion_proof", "no_qualified_lead_claim"]}
    if v2:
        report["contract"]="continuous-trend-report/v2"
        report["selection_timezone"]=raw["selection_timezone"]
        report["open_intervals_are_provisional_not_completed_weeks"]=True
        report["source_quality"]["gsc_weekly_data_state"]="all"
        report["limitations"].append("selection_calendar_is_not_proof_of_source_alignment_or_finality")
    if "worker_window_evidence" in raw:
        report["worker_captured_at"]=raw["worker_captured_at"]
        report["worker_window_evidence"]=[{"source":r["evidence"]["source"],"scope":r["scope"],**r["assessment"]} for r in raw["worker_window_evidence"]]
        report["limitations"].append("worker_source_metadata_preserved_not_independent_finality_guarantee")
    report["report_digest"] = fingerprint(report)
    return report
