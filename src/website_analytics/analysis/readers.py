"""Evidence readers over existing adapters, plus an explicitly synthetic fixture."""
from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from website_analytics.analysis.contracts import AnalysisError, Selection, fingerprint, metric_number, page_path, page_set, scope_document, strict_keys
from website_analytics.analysis.query import METRICS, SourceEvidence
from website_analytics.models import DateRange, SiteConfig


def read_json(path: str | Path) -> object:
    path = Path(path)
    if path.stat().st_size > 2_000_000:
        raise AnalysisError("input exceeds local analysis size limit")
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(AnalysisError("non-finite JSON value")))
    except (UnicodeError, json.JSONDecodeError):
        raise AnalysisError("invalid JSON input") from None


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise AnalysisError("duplicate JSON object key")
        result[key] = value
    return result


class FixtureReader:
    """Exact scope lookup, never a fallback to whole-site or another segment."""
    sample = True

    def __init__(self, path: str | Path):
        raw = read_json(path)
        value = strict_keys(raw, {"contract", "sample", "generated_at", "page_dimension_rows", "records"})
        if value["contract"] != "batch-evidence/v1" or value["sample"] is not True or not isinstance(value["records"], list) or len(value["records"]) > 400:
            raise AnalysisError("only bounded synthetic fixtures are supported by this CLI")
        self.identity = "sample-sha256:" + fingerprint(raw)
        self.generated_at = value["generated_at"]
        self.dimension_rows = value["page_dimension_rows"]
        if not isinstance(self.dimension_rows, list) or len(self.dimension_rows) > 1000:
            raise AnalysisError("invalid sample page dimension")
        for row in self.dimension_rows:
            strict_keys(row, {"route_url", "route_page_id", "route_source", "content_page_id", "template"})
            page_path(row["route_url"])
        self.records = {}
        allowed = {x.name for x in fields(SourceEvidence)} - {"source", "scope_digest"}
        for record in value["records"]:
            strict_keys(record, {"source", "scope", "evidence"})
            if record["source"] not in METRICS:
                raise AnalysisError("unsupported evidence source")
            scope = strict_keys(record["scope"], {"site", "start", "end", "pages", "countries", "channels"})
            if scope["site"] != "genemedi-net" or list(page_set(scope["pages"])) != scope["pages"]:
                raise AnalysisError("invalid fixture page scope")
            selection = Selection.parse({"countries": scope["countries"], "channels": scope["channels"]})
            if list(selection.countries) != scope["countries"] or list(selection.channels) != scope["channels"]:
                raise AnalysisError("fixture segment scope must be canonical")
            evidence = strict_keys(record["evidence"], {"status", "metrics", "fetched_at", "date_boundary"}, allowed)
            key = (record["source"], fingerprint(scope))
            if key in self.records:
                raise AnalysisError("duplicate fixture scope")
            self.records[key] = evidence

    def read(self, source: str, period: DateRange, pages: tuple[str, ...], selection: Selection) -> SourceEvidence:
        digest = fingerprint(scope_document(period, pages, selection))
        if source=="inquiry":self.last_inquiry_metadata=None
        found = self.records.get((source, digest))
        if found is None:
            return SourceEvidence(source, digest, "unavailable", {}, self.generated_at,
                                  "sample", notes=("no_exact_sample_scope",))
        return SourceEvidence(source=source, scope_digest=digest, **found)


class AdapterReader:
    """Inject configured existing adapters; never creates clients or loads secrets.

    The caller must separately supply verified per-source window metadata to
    establish completeness. Successful HTTP/SQL alone is not finality proof.
    CLI live wiring is intentionally absent in this first local slice.
    """
    sample = False
    identity = "existing-adapters/v1"

    def __init__(self, site: SiteConfig, *, ga4=None, gsc=None, inquiry=None,
                 window_evidence: dict | None = None, clock=None, exact_paths=False):
        from website_analytics.analysis.contracts import DOMAINS
        if site.site_key != "genemedi-net" or not site.domains or not set(site.domains) <= set(DOMAINS):
            raise AnalysisError("adapter site is outside this product boundary")
        self.site = site
        self.exact_paths = exact_paths
        self.adapters = {"ga4": ga4, "gsc": gsc, "inquiry": inquiry}
        self.window_evidence = window_evidence or {}
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def read(self, source: str, period: DateRange, pages: tuple[str, ...], selection: Selection) -> SourceEvidence:
        digest = fingerprint(scope_document(period, pages, selection))
        fetched_at = self.clock().isoformat()
        adapter = self.adapters[source]
        if adapter is None:
            return SourceEvidence(source, digest, "unavailable", {}, fetched_at, "unverified",
                                  notes=("adapter_not_configured",))
        try:
            truncated = False
            response_metadata={}
            unattributed = None
            notes = ()
            if source == "ga4":
                method = adapter.scoped_exact_aggregate if self.exact_paths else adapter.scoped_aggregate
                rows = method(self.site, period, pages,
                    countries=selection.countries, channels=selection.channels)
                if self.exact_paths:response_metadata=getattr(adapter,"last_response_metadata",{})
                if len(rows) > 1:
                    raise AnalysisError("GA4 interval aggregate must not contain multiple rows")
                metrics = {k: metric_number(v) for k, v in (rows[0] if rows else {}).items() if k in METRICS[source]}
            elif source == "gsc":
                method = adapter.query_exact_result if self.exact_paths else adapter.query_result
                result = method(self.site, period, (), page_paths=pages,**({"data_state":self.gsc_data_state} if getattr(self,"gsc_data_state","final")!="final" else {}))
                if len(result.rows) > 1:
                    raise AnalysisError("GSC interval aggregate must not contain multiple rows")
                metrics = {k: metric_number(v) for k, v in (result.rows[0] if result.rows else {}).items() if k in METRICS[source]}
                truncated = result.truncated
                notes = ("gsc_top_rows_not_exhaustive_guarantee",)
            else:
                result = adapter.query(period)
                metrics = {k: 0.0 for k in METRICS[source]}
                attributed = 0.0
                for row in result.pages:
                    raw_url = row["sourceUrl"]
                    parts = urlsplit(raw_url)
                    if parts.netloc and (parts.hostname not in self.site.domains or parts.username or parts.password):
                        raise AnalysisError("inquiry origin violates website boundary")
                    try:
                        safe_url = urlunsplit(("https" if parts.netloc else "", parts.netloc, parts.path, "", ""))
                        path = page_path(safe_url)
                        if self.exact_paths:
                            path = parts.path.rstrip("/") or "/"
                    except AnalysisError:
                        # Existing adapter guards external origins. Unparseable
                        # attribution is retained in the sitewide unknown bucket.
                        continue
                    attributed += metric_number(row["storedSubmissions"])
                    if path in pages:
                        for key in metrics:
                            metrics[key] += metric_number(row[key])
                total = metric_number(result.totals["storedSubmissions"])
                if attributed > total:
                    raise AnalysisError("inquiry page counts exceed site total")
                truncated = result.page_rows_truncated
                unattributed = total-attributed if not truncated else None
                notes = ("unattributed_is_sitewide_not_batch_count",)
                fixed_unknown=False
                if getattr(result,"site_scope",None)=="fixed_registered_source":
                    notes=("unattributed_is_databasewide_not_verified_site_count","fixed_source_pageurl_attribution_not_dedication")
                    fixed_unknown=result.unknown_attribution_count is None or result.unknown_attribution_count>0
                dates=[row["date"] for row in result.daily]
                self.last_inquiry_metadata={"scope_digest":digest,"max_record_date":max(dates) if dates else None,
                    "truncated":truncated,"unknown_attribution_count":unattributed}
            metadata = self.window_evidence.get((source, period.start.isoformat(), period.end.isoformat()), {})
            strict_keys(metadata, set(), {"date_boundary", "coverage_start", "coverage_end", "complete_through", "cutoff_basis", "missing_dates", "truncated", "notes", "status"})
            status=metadata.get("status","complete")
            if source=="inquiry" and locals().get("fixed_unknown",False):status="partial"
            if status not in {"complete","partial"}:raise AnalysisError("invalid window status")
            boundary=metadata.get("date_boundary","unverified")
            if self.exact_paths and source=="ga4":
                zone=response_metadata.get("timezone")
                if zone is not None:
                    from website_analytics.analysis.contracts import timezone_name
                    zone=timezone_name(zone)
                    if boundary not in {"unverified",zone}:raise AnalysisError("GA4 source day boundary mismatch")
                    boundary=zone
                flags=[response_metadata.get(key) for key in ("sampled","thresholded","truncated")]
                if any(type(flag) is not bool for flag in flags):
                    status="partial";notes=notes+("ga4_response_quality_unknown",)
                if any(flag is True for flag in flags):
                    truncated=True;notes=notes+("ga4_response_quality_limited",)
            return SourceEvidence(source, digest, status, metrics, fetched_at,
                boundary,
                coverage_start=metadata.get("coverage_start"), coverage_end=metadata.get("coverage_end"),
                complete_through=metadata.get("complete_through"), cutoff_basis=metadata.get("cutoff_basis", "unknown"),
                truncated=truncated or metadata.get("truncated", False), unattributed_submissions=unattributed,
                notes=notes+tuple(metadata.get("notes", ())),
                missing_dates=metadata.get("missing_dates", ()))
        except Exception as error:
            # Source exceptions may contain DSNs/URLs/payloads. Do not echo them.
            from website_analytics.analysis.diagnostics import diagnostic_notes
            return SourceEvidence(source, digest, "error", {}, fetched_at, "unverified",
                                  notes=("source_request_failed",) + diagnostic_notes(error))
