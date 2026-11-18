"""Unconfirmed exploratory cases with approximate dates and exact URL identity.

Separate from confirmed batch-analysis/v1: no fabricated time, timezone or user
confirmation is needed to inspect aggregate evidence. Legacy classification keys
remain an explicit lookup aid, never the identity of an observed URL.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from website_analytics.analysis.contracts import AnalysisError, calendar_date, fingerprint, identifier, metric_number, page_path, strict_keys, text
from website_analytics.page_classification import canonical_page_path

PREVIEW_CONTRACT = "batch-preview/v1"


@dataclass(frozen=True)
class PreviewDraft:
    draft_id: str
    raw_urls: tuple[str, ...]
    reference_date: str
    user_statement: str

    @classmethod
    def parse(cls, value: object) -> PreviewDraft:
        raw = strict_keys(value, {"contract", "site", "draft_id", "state", "raw_urls", "change"})
        if raw["contract"] != PREVIEW_CONTRACT or raw["site"] != "genemedi-net" or raw["state"] != "draft_unconfirmed":
            raise AnalysisError("unsupported exploratory draft")
        urls = raw["raw_urls"]
        if not isinstance(urls, list) or not 1 <= len(urls) <= 100 or any(not isinstance(url, str) for url in urls) or len(set(urls)) != len(urls):
            raise AnalysisError("exploratory URLs must be a bounded unique list")
        for url in urls:
            page_path(url)  # Safety validation only; intentionally discard its folded result.
            if urlsplit(url).scheme != "https":
                raise AnalysisError("exploratory draft retains full approved HTTPS URLs")
        change = strict_keys(raw["change"], {"reference_date", "precision", "timezone", "description", "user_statement"})
        if change["precision"] != "approximate_day" or change["timezone"] is not None or change["description"] is not None:
            raise AnalysisError("this draft must preserve unknown time, timezone and change details")
        day = calendar_date(change["reference_date"]).isoformat()
        return cls(identifier(raw["draft_id"]), tuple(urls), day, text(change["user_statement"], limit=1000))

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(urlsplit(url).path for url in self.raw_urls)

    def document(self) -> dict:
        return {"contract": PREVIEW_CONTRACT, "site": "genemedi-net", "draft_id": self.draft_id,
            "state": "draft_unconfirmed", "raw_urls": list(self.raw_urls),
            "change": {"reference_date": self.reference_date, "precision": "approximate_day",
                "timezone": None, "description": None, "user_statement": self.user_statement}}

    @property
    def digest(self) -> str:
        return fingerprint(self.document())

    def lookup_rows(self) -> list[dict]:
        return [{"raw_url": url, "exact_path": urlsplit(url).path,
                 "legacy_classification_lookup_path": canonical_page_path(url),
                 "classification_key_is_not_url_equivalence_proof": True} for url in self.raw_urls]

    def scenarios(self, *, days: int = 7) -> list[dict]:
        if type(days) is not int or not 1 <= days <= 90:
            raise AnalysisError("invalid exploratory window")
        reference = calendar_date(self.reference_date)
        rows = []
        for offset in (-1, 0, 1):
            assumed = reference + timedelta(days=offset)
            rows.append({"assumed_change_date": assumed.isoformat(), "offset_days": offset,
                "assumption_not_confirmed_date": True,
                "before": {"start": (assumed-timedelta(days=days)).isoformat(), "end": (assumed-timedelta(days=1)).isoformat()},
                "after": {"start": (assumed+timedelta(days=1)).isoformat(), "end": (assumed+timedelta(days=days)).isoformat()}})
        return rows


def _interval_metrics(envelope: dict, source: str) -> dict | None:
    from website_analytics.analysis.query import METRICS
    if envelope.get("status") != "ok":
        return None
    result = envelope["result"]
    rows = result.get("rows", []) if source == "gsc" else result
    if source == "gsc" and result.get("truncated"):
        return None
    if not rows:
        return None  # No source row is not evidence of zero traffic.
    if len(rows) != 1:
        raise AnalysisError("preview interval must be independently aggregated")
    metrics = {k: metric_number(rows[0][k]) for k in METRICS[source] if k in rows[0]}
    if source == "gsc" and "clicks" in metrics and "impressions" in metrics:
        metrics["ctr"] = metrics["clicks"]/metrics["impressions"] if metrics["impressions"] else 0
    return metrics


def _page_day_metrics(envelope: dict, period: dict, paths: tuple[str, ...]) -> dict | None:
    if envelope.get("status") != "ok" or envelope["result"].get("truncated"):
        return None
    rows = [r for r in envelope["result"]["rows"] if period["start"] <= r["date"] <= period["end"] and urlsplit(r["page"]).path.rstrip("/") in paths]
    if not rows:
        return None
    clicks = sum(metric_number(r["clicks"]) for r in rows)
    impressions = sum(metric_number(r["impressions"]) for r in rows)
    return {"clicks": clicks, "impressions": impressions, "ctr": clicks/impressions if impressions else 0,
            "position": sum(metric_number(r["position"])*metric_number(r["impressions"]) for r in rows)/impressions if impressions else None}


def _page_day_coverage(envelope: dict, period: dict, path: str) -> dict:
    first, last = calendar_date(period["start"]), calendar_date(period["end"])
    expected = {(first+timedelta(days=n)).isoformat() for n in range((last-first).days+1)}
    rows = envelope.get("result", {}).get("rows", []) if envelope.get("status") == "ok" else []
    observed = {r["date"] for r in rows if r["date"] in expected and urlsplit(r["page"]).path.rstrip("/") == path}
    return {"dates_with_rows": sorted(observed), "dates_without_returned_rows": sorted(expected-observed),
            "zero_filled": False, "no_row_is_not_zero_or_missing_finality_proof": True}


def build_preview_report(draft: PreviewDraft, packet: object, url_audit: object) -> dict:
    """Import real aggregate evidence without inventing a confirmed batch.

    Exposes observed values and diagnostics, not a confirmed causal effect or a
    claim that GA4/legacy database daily finality has been independently proved.
    """
    raw = strict_keys(packet, {"protocol", "sample", "exploratory", "batch_confirmed", "queried_at", "runtime", "submitted_paths", "variant_paths_not_merged", "scenarios", "writes", "cache_or_audit_persisted", "broad_range", "gsc_page_days", "ga4_page_days", "cms", "query_metadata"})
    if raw["protocol"] != "seo-preview-read/1" or raw["sample"] is not False or raw["exploratory"] is not True or raw["batch_confirmed"] is not False or raw["writes"] != 0 or raw["cache_or_audit_persisted"] is not False or raw["submitted_paths"] != list(draft.paths):
        raise AnalysisError("preview evidence does not match this unconfirmed real case")
    expected = draft.scenarios(days=7)
    if not isinstance(raw["scenarios"], list) or [r["offset_days"] for r in raw["scenarios"]] != [-1, 0, 1]:
        raise AnalysisError("preview must preserve all declared date scenarios")
    scenarios, reconciliations = [], []
    for declared, observed in zip(expected, raw["scenarios"], strict=True):
        row = {"assumed_change_date": declared["assumed_change_date"], "offset_days": observed["offset_days"], "assumption_not_confirmed_date": True, "periods": {}}
        for label in ("before", "after"):
            data = observed["periods"][label]
            period = declared[label]
            if data["start"] != period["start"] or data["end"] != period["end"]:
                raise AnalysisError("preview source dates differ from requested scenario")
            inquiry = data["inquiry"]
            item = {**period, "ga4": _interval_metrics(data["ga4_submitted"], "ga4"),
                    "gsc_from_page_day_rows": _page_day_metrics(raw["gsc_page_days"], period, draft.paths),
                    "inquiry_linked_to_submitted_urls": inquiry["result"]["cohorts"]["submitted"] if inquiry.get("status") == "ok" and not inquiry["result"].get("page_rows_truncated") and not inquiry["result"].get("foreign_origin_group_count") else None}
            if observed["offset_days"] == 0:
                item["gsc_interval"] = _interval_metrics(data["gsc_submitted"], "gsc")
                item["lowercase_variant_not_merged"] = {
                    "paths": raw["variant_paths_not_merged"],
                    "ga4": _interval_metrics(data["ga4_lowercase_variant"], "ga4"),
                    "gsc_interval": _interval_metrics(data["gsc_lowercase_variant"], "gsc"),
                    "inquiry": inquiry["result"]["cohorts"]["lowercase_variant"] if inquiry.get("status") == "ok" and not inquiry["result"].get("page_rows_truncated") and not inquiry["result"].get("foreign_origin_group_count") else None}
                item["database_unattributed_not_assigned_to_batch"] = inquiry["result"].get("database_unattributed_count") if inquiry.get("status") == "ok" else None
                item["database_scope"] = "configured_database_not_independently_proven_dedicated"
                pages = []
                for path in (*draft.paths, *raw["variant_paths_not_merged"]):
                    ga_rows = data["ga4_pages"].get("result", []) if data["ga4_pages"].get("status") == "ok" else []
                    matching = [r for r in ga_rows if r.get("landingPage", "").rstrip("/") == path]
                    ga_metrics = {k: metric_number(v) for k, v in matching[0].items() if k != "landingPage"} if len(matching) == 1 else None
                    pages.append({"exact_path": path, "submitted": path in draft.paths, "ga4_interval": ga_metrics,
                        "gsc_page_day_sum": _page_day_metrics(raw["gsc_page_days"], period, (path,)),
                        "gsc_date_coverage": _page_day_coverage(raw["gsc_page_days"], period, path),
                        "inquiry": [r for r in inquiry["result"]["rows"] if r["exact_path"] == path] if inquiry.get("status") == "ok" else None})
                item["pages"] = pages
                if item["gsc_interval"] is not None and item["gsc_from_page_day_rows"] is not None:
                    reconciliations.append({"period": label, "source": "gsc", "interval_matches_page_day_clicks_impressions": all(item["gsc_interval"][k] == item["gsc_from_page_day_rows"][k] for k in ("clicks", "impressions"))})
                if item["ga4"] is not None and data["ga4_pages"].get("status") == "ok":
                    observed_sessions = sum(metric_number(r["sessions"]) for r in data["ga4_pages"]["result"] if r.get("landingPage", "").rstrip("/") in draft.paths)
                    reconciliations.append({"period": label, "source": "ga4", "interval_sessions": item["ga4"]["sessions"],
                        "page_sessions_sum": observed_sessions, "sessions_match": item["ga4"]["sessions"] == observed_sessions,
                        "interval_users_queried_independently_never_page_or_day_sum": True})
            row["periods"][label] = item
        scenarios.append(row)
    main = scenarios[1]
    quality = raw["query_metadata"]
    metadata = quality.get("ga4", [])
    cms = raw["cms"]
    route_groups = {}
    if cms.get("status") == "ok":
        for route in cms["result"]["route_rows"]:
            route_groups.setdefault(canonical_page_path(route["route_url"]), []).append(route["route_url"])
    report = {"contract": "batch-preview-report/v1", "sample": False, "status": "exploratory_unconfirmed",
        "batch": draft.document(), "draft_digest_for_review_not_confirmation": draft.digest,
        "source_packet_digest": fingerprint(packet), "public_url_metadata": url_audit,
        "url_identity": draft.lookup_rows(), "preview_main": main, "date_sensitivity": scenarios,
        "reconciliation": reconciliations, "cms_current": raw["cms"],
        "legacy_lookup_collisions_not_url_equivalence": [
            {"legacy_lookup_path": key, "raw_route_spellings": sorted(values),
             "invalid_broken_is_not_an_http_404_assertion": True}
            for key, values in route_groups.items() if len(set(values)) > 1],
        "ordinary_traffic_target_visitors_undefined": True, "effect_confirmed": False,
        "source_quality": {"ga4_property_timezones": sorted({m["time_zone"] for m in metadata if m.get("time_zone")}),
            "ga4_thresholding_observed": any(m.get("subject_to_thresholding") for m in metadata),
            "ga4_other_row_loss_observed": any(m.get("data_loss_from_other_row") for m in metadata),
            "ga4_sampling_observed": any(s["samples_read_count"] < s["sampling_space_size"] for m in metadata for s in m.get("sampling", [])),
            "ga4_daily_finality": "not_independently_verified", "gsc_data_state": "final",
            "gsc_page_days_truncated": raw["gsc_page_days"].get("result", {}).get("truncated"),
            "inquiry_date_boundary": "legacy_server_calendar_unverified"},
        "queried_at": raw["queried_at"], "production_release": raw["runtime"]["production_release"],
        "limitations": ["approximate_change_date_no_time_or_timezone_confirmed", "plus_minus_one_day_are_analyst_scenarios_not_user_asserted_bounds",
            "exact_case_url_variants_not_automatically_merged", "missing_source_rows_not_zero_filled",
            "gsc_privacy_and_low_volume_can_omit_rows", "language_inferred_from_live_english_html_not_visitor_language",
            "inquiry_counts_not_qualified_leads_unknown_not_assigned", "no_causal_effect_claim"]}
    report["limitations"].append("ga4_landing_cohort_views_are_not_total_page_url_views")
    report["report_digest"] = fingerprint(report)
    return report
