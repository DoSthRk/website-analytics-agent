"""Deterministic read-only batch use case. No persistence or client creation."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol
import math
import re

from website_analytics.analysis import CONTRACT_VERSION, METRIC_VERSION
from website_analytics.analysis.batches import ConfirmedBatch
from website_analytics.analysis.contracts import AnalysisError, Query, Selection, calendar_date, fingerprint, metric_number, scope_document, strict_keys, timestamp
from website_analytics.models import DateRange
from website_analytics.page_classification import PageDimension
from website_analytics.product_mapping import ProductMapping, match_product_rule
from website_analytics.reporting import compare_totals

METRICS = {
    "gsc": ("clicks", "impressions", "ctr", "position"),
    "ga4": ("sessions", "totalUsers", "activeUsers", "engagedSessions", "engagementRate", "screenPageViews", "keyEvents"),
    "inquiry": ("storedSubmissions", "quarantinedSubmissions", "nonQuarantinedSubmissions"),
}


@dataclass(frozen=True)
class SourceEvidence:
    source: str
    scope_digest: str
    status: str
    metrics: dict[str, float]
    fetched_at: str
    date_boundary: str
    coverage_start: str | None = None
    coverage_end: str | None = None
    complete_through: str | None = None
    cutoff_basis: str = "unknown"
    truncated: bool = False
    unattributed_submissions: float | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    missing_dates: tuple[str, ...] = field(default_factory=tuple)

    def assess(self, period: DateRange, expected_scope: str, *, sample: bool,
               as_of: datetime) -> dict:
        if self.source not in METRICS or self.scope_digest != expected_scope or self.status not in {"complete", "partial", "error", "unavailable"}:
            raise AnalysisError("source evidence does not match requested scope")
        if self.cutoff_basis not in {"unknown", "configured_lag", "observed", "sample"} or (self.cutoff_basis == "sample" and not sample):
            raise AnalysisError("invalid source cutoff evidence")
        if not isinstance(self.date_boundary, str) or not self.date_boundary or len(self.date_boundary) > 100 or timestamp(self.fetched_at) > as_of:
            raise AnalysisError("invalid source time metadata")
        if type(self.truncated) is not bool or not isinstance(self.metrics, dict) or set(self.metrics) - set(METRICS[self.source]):
            raise AnalysisError("unsupported source metric metadata")
        if not isinstance(self.notes, (tuple, list)) or len(self.notes) > 10 or any(not isinstance(n, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", n) for n in self.notes):
            raise AnalysisError("source notes must be bounded reason codes")
        if not isinstance(self.missing_dates, (tuple, list)) or len(self.missing_dates) > 90 or any(not period.start <= calendar_date(d) <= period.end for d in self.missing_dates):
            raise AnalysisError("invalid missing source dates")
        metrics = {key: metric_number(value) for key, value in self.metrics.items()}
        if any(metrics.get(key, 0) > 1 for key in ("ctr", "engagementRate")):
            raise AnalysisError("source rates must be fractions")
        for numerator, denominator, rate in (("clicks", "impressions", "ctr"), ("engagedSessions", "sessions", "engagementRate")):
            if numerator in metrics and denominator in metrics:
                if metrics[numerator] > metrics[denominator]:
                    raise AnalysisError("source rate counts are inconsistent")
                metrics[rate] = metrics[numerator] / metrics[denominator] if metrics[denominator] else 0.0
        if self.source == "inquiry" and set(METRICS["inquiry"]) <= metrics.keys() and not math.isclose(metrics["storedSubmissions"], metrics["quarantinedSubmissions"] + metrics["nonQuarantinedSubmissions"]):
            raise AnalysisError("inquiry counts do not form the stored partition")
        if self.complete_through is not None and calendar_date(self.complete_through) > timestamp(self.fetched_at).date():
            raise AnalysisError("source cutoff cannot be later than fetching date")
        if self.unattributed_submissions is not None:
            if self.source != "inquiry":
                raise AnalysisError("unattributed inquiry counts belong to inquiry only")
            metric_number(self.unattributed_submissions)
        reasons = []
        if self.date_boundary == "unverified":
            reasons.append("source_day_boundary_unverified")
        if self.status != "complete":
            reasons.append("source_" + self.status)
        if self.truncated:
            reasons.append("source_truncated")
        if self.missing_dates:
            reasons.append("source_dates_missing")
        if self.coverage_start is None or self.coverage_end is None:
            reasons.append("coverage_not_verified")
        elif not calendar_date(self.coverage_start) <= period.start <= period.end <= calendar_date(self.coverage_end):
            reasons.append("requested_dates_not_covered")
        if self.complete_through is None or calendar_date(self.complete_through) < period.end:
            reasons.append("period_not_finalized")
        if self.cutoff_basis not in {"observed", "sample"}:
            reasons.append("finality_is_estimate_or_unknown")
        if period.end >= as_of.date():
            reasons.append("period_includes_current_or_future_date")
        if self.status in {"error", "unavailable"} and metrics:
            raise AnalysisError("failed sources cannot supply reportable metrics")
        return {
            "status": "complete" if not reasons else "partial", "source_status": self.status,
            "metrics": metrics, "comparison_eligible": not reasons,
            "fetched_at": self.fetched_at, "date_boundary": self.date_boundary,
            "coverage": {"start": self.coverage_start, "end": self.coverage_end, "missing_dates": sorted(set(self.missing_dates))},
            "complete_through": self.complete_through, "cutoff_basis": self.cutoff_basis,
            "truncated": self.truncated, "scope_digest": self.scope_digest,
            "unattributed_submissions_sitewide": self.unattributed_submissions,
            "limitations": sorted(set(reasons) | set(self.notes)),
        }


class EvidenceReader(Protocol):
    sample: bool
    identity: str

    def read(self, source: str, period: DateRange, pages: tuple[str, ...], selection: Selection) -> SourceEvidence: ...


def _comparison(before: dict, after: dict) -> dict:
    # Reuse the existing comparator, retaining missingness and withholding deltas
    # when coverage/finality is uncertain. Observed partial values remain visible.
    metrics = {}
    complete = True
    for source, names in METRICS.items():
        pre, post = before[source], after[source]
        result = compare_totals({source: post["metrics"]}, {source: pre["metrics"]})
        values = result["metrics"].get(source, {})
        rows = {}
        for name in names:
            row = dict(values.get(name, {"current": None, "previous": None, "available": False, "delta": None}))
            eligible = row["available"] and pre["comparison_eligible"] and post["comparison_eligible"]
            row["available"] = eligible
            row["delta"] = row["delta"] if eligible else None
            baseline = row["previous"]
            row["relative_change"] = (row["delta"] / baseline if eligible and baseline else None)
            row["unit"] = "fraction" if name in {"ctr", "engagementRate"} else ("average_rank" if name == "position" else "count")
            complete = complete and eligible
            rows[name] = row
        metrics[source] = rows
    return {"complete": complete, "metrics": metrics}


def analyze(batch: ConfirmedBatch, query: Query, reader: EvidenceReader,
            dimension: PageDimension, mapping: ProductMapping | None = None,
            *, as_of: datetime | None = None) -> dict:
    now = as_of or datetime.now(timezone.utc)
    batch = ConfirmedBatch.parse(batch.document())
    query = Query.parse(query.document(), batch.draft)
    if now.tzinfo is None or batch.draft.batch_id != query.batch_id or batch.draft.revision != query.revision:
        raise AnalysisError("analysis must reference its confirmed revision")
    if batch.draft.sample != reader.sample:
        raise AnalysisError("sample and real evidence cannot be mixed")
    if dimension.site_key != "genemedi-net" or (mapping is not None and mapping.site_key != dimension.site_key):
        raise AnalysisError("classification site mismatch")
    pages = query.selection.pages or batch.draft.pages
    cohorts = {"changed": pages}
    if query.include_control:
        cohorts["control"] = batch.draft.control_pages
    results = {}
    for label, cohort in cohorts.items():
        periods = {}
        for period_label, period in (("before", query.before), ("after", query.after)):
            scope = fingerprint(scope_document(period, cohort, query.selection))
            periods[period_label] = {}
            for source in METRICS:
                evidence = reader.read(source, period, cohort, query.selection)
                if evidence.source != source:
                    raise AnalysisError("source identity mismatch")
                periods[period_label][source] = evidence.assess(period, scope, sample=reader.sample,
                    as_of=as_of or datetime.now(timezone.utc))
        results[label] = {**periods, "comparison": _comparison(periods["before"], periods["after"])}
    control = None
    if "control" in results:
        control = {}
        for source in METRICS:
            control[source] = {}
            for metric in METRICS[source]:
                a = results["changed"]["comparison"]["metrics"][source][metric]["delta"]
                b = results["control"]["comparison"]["metrics"][source][metric]["delta"]
                control[source][metric] = {"changed_delta": a, "control_delta": b}
        # Deliberately no pooled ratio or causal lift for differently sized cohorts.
    classifications = []
    for cohort, paths in cohorts.items():
        for path in paths:
            entry = dimension.classify(path)
            rule = match_product_rule(mapping, path, entry.page_class, entry.template) if mapping else None
            first = path.strip("/").split("/")[0]
            language = "ja" if first == "ja" else ("unknown" if len(first) == 2 and first != "en" else "en")
            classifications.append({"cohort": cohort, "path": path,
                "language_from_directory": language, "language_evidence": "directory_convention_not_browser_language",
                "language_launch_status": "not_verified_by_this_query",
                "page_class": entry.page_class, "classification_status": entry.classification_status,
                "product_line": rule.product_line_id if rule and rule.mapping_status == "approved" and entry.page_class == "product_page" else None})
    result = {"contract": CONTRACT_VERSION, "metric_version": METRIC_VERSION,
        "status": "ok" if all(x["comparison"]["complete"] for x in results.values()) else "partial",
        "sample": reader.sample, "evidence_identity": reader.identity,
        "generated_at": (as_of or datetime.now(timezone.utc)).isoformat(),
        "batch": batch.document(), "classification_version": dimension.version,
        "product_mapping_version": mapping.version if mapping else None,
        "periods": {"before": {"start": query.before.start.isoformat(), "end": query.before.end.isoformat()},
                    "after": {"start": query.after.start.isoformat(), "end": query.after.end.isoformat()}},
        "selection": query.selection.document(),
        "visitor_label": "filtered_ga4_visitors" if query.selection.countries or query.selection.channels else "ordinary_traffic_target_visitors_undefined",
        "filter_application": {"ga4": "landing-page cohort AND optional country/session-channel", "gsc": "page cohort; GA4 country/channel conditions do not apply", "inquiry": "submission PageURL cohort; visitor segments unavailable"},
        "metric_priority": ["gsc", "ga4", "inquiry"], "pages": classifications,
        "cohorts": results, "control_context": control,
        "limitations": ["observational_before_after_not_causal", "launch_day_excluded_in_batch_timezone",
            "source_day_boundaries_may_differ_no_timestamp_rebucketing",
            "gsc_can_omit_rows_even_without_local_truncation", "key_events_are_not_form_submission_counts",
            "inquiry_counts_are_not_qualified_leads", "unattributed_inquiries_cannot_be_assigned_to_batch",
            "language_is_directory_inference_no_hreflang_or_launch_verification"]}
    if (query.before.end-query.before.start).days % 7 != 6:
        result["limitations"].append("window_does_not_cover_whole_weeks")
    result["result_digest"] = fingerprint({k: v for k, v in result.items() if k != "generated_at"})
    return result
