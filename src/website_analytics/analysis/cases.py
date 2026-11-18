"""Versioned, aggregate-only provisional cases. HTTP operations never import/fetch."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from website_analytics.analysis.case_validation import PacketValidator, digest, safe_text, fields
from website_analytics.analysis.continuous import ContinuousDraft, build_weekly_report
from website_analytics.analysis.contracts import AnalysisError, canonical_json, fingerprint, identifier, strict_keys, timestamp
from website_analytics.analysis.preview import PreviewDraft, build_preview_report

CASE_CONTRACT = "case-evidence/v1"
CASE_ENGINE = "case-read/1"
MAX_CASE_BYTES = 512_000
MAX_CASE_VERSIONS = 512
CASE_OPERATIONS = ("list_cases", "case_versions", "case_observations", "case_detail", "case_trend", "page_contribution")


def current_interpretation(context: ContinuousDraft, prior_digests: list[str]) -> dict:
    return {"contract": "case-interpretation/v2", "basis": "latest_user_report_of_continuous_changes",
        "intervention": context.document(), "superseded_report_or_notice_digests": prior_digests,
        "october6_12": "already_after_reported_start_not_unoptimized_baseline",
        "october14_20": "completion_and_stability_unconfirmed",
        "both_windows_definitely_still_modifying": False,
        "october13": "earlier_approximate_completion_estimate_not_verified_single_intervention",
        "august": "candidate_baseline_not_verified_unchanged_or_same_lifecycle",
        "stage_order": "user_recollection_unverified_dates_unknown", "stage_effects_identifiable": False,
        "old_report_bytes_changed": False}


def _validated(record):
    strict_keys(record, {"contract", "engine", "case_id", "kind", "evidence_state", "sample", "batch_confirmed",
        "draft", "source_packet", "url_evidence", "freshness_recheck", "current_intervention",
        "superseded_digests", "expected_report_digest"})
    if record["contract"] != CASE_CONTRACT or record["engine"] != CASE_ENGINE or record["evidence_state"] != "provisional" or record["sample"] is not False or record["batch_confirmed"] is not False:
        raise AnalysisError("real imports cannot become synthetic or confirmed records")
    identifier(record["case_id"])
    context = ContinuousDraft.parse(record["current_intervention"])
    if record["kind"] == "continuous_trend":
        draft = ContinuousDraft.parse(record["draft"])
    elif record["kind"] == "approximate_window":
        draft = PreviewDraft.parse(record["draft"])
    else:
        raise AnalysisError("unsupported saved case kind")
    if draft.raw_urls != context.raw_urls:
        raise AnalysisError("interpretation correction must bind the same exact URLs")
    safe_text(draft.user_statement)
    safe_text(context.user_statement)
    superseded = record["superseded_digests"]
    if not isinstance(superseded, list) or len(superseded) > 20:
        raise AnalysisError("invalid interpretation provenance")
    for item in superseded:
        digest(item)
    packet = record["source_packet"]
    if not isinstance(packet, dict) or not isinstance(packet.get("variant_paths_not_merged"), list):
        raise AnalysisError("saved scoped packet required")
    validator = PacketValidator(draft.paths, packet["variant_paths_not_merged"])
    validator.packet(packet, record["kind"])
    validator.url_evidence(record["url_evidence"], record["kind"])
    validator.freshness_record(record["freshness_recheck"])
    return draft, context


def render_case(record: object, *, verify=True) -> dict:
    draft, context = _validated(record)
    if record["kind"] == "continuous_trend":
        result = build_weekly_report(draft, record["source_packet"], record["url_evidence"], availability_recheck=record["freshness_recheck"])
        result["prior_october_windows_meaning"] = "october6_12_not_unoptimized_baseline_october14_20_stability_unknown"
    else:
        if record["freshness_recheck"] is not None:
            raise AnalysisError("historical window preview cannot inherit later date finality")
        result = build_preview_report(draft, record["source_packet"], record["url_evidence"])
        result["single_event_interpretation_superseded"] = True
        result["calendar_anchor_scenarios_are_not_verified_intervention_dates"] = True
    original_digest = result.pop("report_digest")
    result.update(evidence_state="provisional", batch_confirmed=False,
        interpretation=current_interpretation(context, record["superseded_digests"]),
        original_engine_report_digest=original_digest, case_engine=CASE_ENGINE,
        source_grain_note="GA4 registered GeneMedi hostnames and exact landing paths; GSC registered property and exact page URLs; not URL-host-separated visitors")
    result["report_digest"] = fingerprint(result)
    if verify:
        digest(record["expected_report_digest"])
        if result["report_digest"] != record["expected_report_digest"]:
            raise AnalysisError("case report replay version/integrity mismatch")
    return result


def make_case_record(case_id, kind, draft, packet, urls, context, *, freshness=None, superseded=()) -> dict:
    record = {"contract": CASE_CONTRACT, "engine": CASE_ENGINE, "case_id": case_id, "kind": kind,
        "evidence_state": "provisional", "sample": False, "batch_confirmed": False,
        "draft": draft, "source_packet": packet, "url_evidence": urls, "freshness_recheck": freshness,
        "current_intervention": context, "superseded_digests": sorted(set(superseded)), "expected_report_digest": None}
    result = render_case(record, verify=False)
    record["expected_report_digest"] = result["report_digest"]
    _body(record)
    return record


def _body(record):
    body = canonical_json(record)
    if len(body.encode()) > MAX_CASE_BYTES:
        raise AnalysisError("case evidence size limit exceeded")
    return body


def page_choices(record):
    urls = record["draft"]["raw_urls"]
    paths = [urlsplit(url).path for url in urls]
    result = [{"page_key": fingerprint({"site": "genemedi-net", "exact_path": path}),
               "exact_path": path, "submitted_url": url, "cohort": "submitted"} for path, url in zip(paths, urls, strict=True)]
    result.extend({"page_key": fingerprint({"site": "genemedi-net", "exact_path": path}),
                   "exact_path": path, "submitted_url": None, "cohort": "separate_variant"}
                  for path in record["source_packet"]["variant_paths_not_merged"])
    return result


class CaseStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def readonly(self):
        if not self.path.is_file():
            raise AnalysisError("case store not found")
        return sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro", uri=True)

    def put(self, record) -> str:
        self.put_many([record])
        return fingerprint(record)

    def put_many(self, records):
        if not isinstance(records, list) or not 1 <= len(records) <= 8:
            raise AnalysisError("bounded local import required")
        prepared = []
        for record in records:
            body = _body(record)
            render_case(record)  # Entire bundle validated before any store write.
            prepared.append((record, fingerprint(record), body))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS case_evidence (case_id TEXT NOT NULL, evidence_id TEXT PRIMARY KEY, payload TEXT NOT NULL, stored_at TEXT NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            for record, evidence_id, body in prepared:
                existing = db.execute("SELECT payload FROM case_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
                if existing:
                    if existing[0] != body:
                        raise AnalysisError("existing case evidence is corrupt; no overwrite")
                    continue
                rows = db.execute("SELECT payload FROM case_evidence WHERE case_id=?", (record["case_id"],)).fetchall()
                if len(rows) >= MAX_CASE_VERSIONS:
                    raise AnalysisError("case version limit exceeded")
                if db.execute("SELECT count(*) FROM case_evidence").fetchone()[0]>=MAX_CASE_VERSIONS:raise AnalysisError("case history capacity reached; archive/review required, no evidence deleted")
                if any(json.loads(row[0])["draft"]["raw_urls"] != record["draft"]["raw_urls"] for row in rows):
                    raise AnalysisError("case grouping cannot silently change the exact URL set")
                db.execute("INSERT INTO case_evidence VALUES (?,?,?,?)", (record["case_id"], evidence_id, body, datetime.now(timezone.utc).isoformat()))
        return [evidence_id for _, evidence_id, _ in prepared]

    def get(self, case_id, evidence_id):
        identifier(case_id)
        digest(evidence_id)
        with self.readonly() as db:
            size = db.execute("SELECT length(CAST(payload AS BLOB)) FROM case_evidence WHERE case_id=? AND evidence_id=?", (case_id, evidence_id)).fetchone()
            if size is None:
                raise KeyError("case evidence not found")
            if size[0] > MAX_CASE_BYTES:
                raise AnalysisError("stored case exceeds size limit")
            row = db.execute("SELECT payload FROM case_evidence WHERE case_id=? AND evidence_id=?", (case_id, evidence_id)).fetchone()
        record = json.loads(row[0])
        if fingerprint(record) != evidence_id or record.get("case_id") != case_id:
            raise AnalysisError("case evidence hash mismatch")
        render_case(record)
        return record

    def versions(self, case_id):
        identifier(case_id)
        with self.readonly() as db:
            ids = db.execute("SELECT evidence_id,stored_at FROM case_evidence WHERE case_id=? ORDER BY evidence_id LIMIT ?", (case_id,MAX_CASE_VERSIONS+1)).fetchall()
        if not ids:
            raise KeyError("case not found")
        if len(ids) > MAX_CASE_VERSIONS:
            raise AnalysisError("case version limit exceeded")
        versions = []
        for evidence_id, stored_at in ids:
            record = self.get(case_id, evidence_id)
            report = render_case(record)
            versions.append({"evidence_id": evidence_id, "kind": record["kind"], "evidence_state": "provisional",
                "sample": False, "batch_confirmed": False, "report_contract": report["contract"],
                "report_digest": report["report_digest"], "source_packet_digest": report["source_packet_digest"],
                "interpretation_contract": "case-interpretation/v2", "source_queried_at": report["queried_at"], "stored_at": stored_at})
        return versions

    def version_count(self,case_id):
        identifier(case_id)
        with self.readonly() as db:return db.execute("SELECT count(*) FROM case_evidence WHERE case_id=?",(case_id,)).fetchone()[0]

    def version_page(self,case_id,*,limit=20,before_sequence=None):
        identifier(case_id)
        if type(limit) is not int or not 1<=limit<=32 or before_sequence is not None and (type(before_sequence) is not int or not 1<=before_sequence<=2**63-1):raise AnalysisError("invalid history pagination")
        with self.readonly() as db:
            ids=db.execute("SELECT rowid,evidence_id FROM case_evidence WHERE case_id=? AND rowid<? ORDER BY rowid DESC LIMIT ?",(case_id,before_sequence or 2**63-1,limit+1)).fetchall()
        if not ids and not self.version_count(case_id):raise KeyError("case not found")
        metadata={v["evidence_id"]:v for v in self._version_metadata(case_id,[eid for _,eid in ids[:limit]])}
        return {"versions":[{**metadata[eid],"sequence":seq} for seq,eid in ids[:limit]],"next_before_sequence":ids[limit-1][0] if len(ids)>limit else None,"version_count":self.version_count(case_id),"history_policy":"append_only_no_automatic_deletion"}

    def _version_metadata(self,case_id,ids):
        output=[]
        for eid in ids:
            record=self.get(case_id,eid);report=render_case(record)
            with self.readonly() as db:stored=db.execute("SELECT stored_at FROM case_evidence WHERE evidence_id=?",(eid,)).fetchone()[0]
            output.append({"evidence_id":eid,"kind":record["kind"],"evidence_state":"provisional","sample":False,"batch_confirmed":False,"report_contract":report["contract"],"report_digest":report["report_digest"],"source_packet_digest":report["source_packet_digest"],"interpretation_contract":"case-interpretation/v2","source_queried_at":report["queried_at"],"stored_at":stored})
        return output

    def observation_versions(self,case_id):
        if self.version_count(case_id)<=32:return self.versions(case_id)
        with self.readonly() as db:ids=[r[0] for r in db.execute("SELECT evidence_id FROM case_evidence WHERE case_id=? ORDER BY julianday(json_extract(payload,'$.source_packet.queried_at')) DESC,rowid DESC LIMIT 64",(case_id,))]
        return self._version_metadata(case_id,ids)

    def list_cases(self):
        with self.readonly() as db:ids=[row[0] for row in db.execute("SELECT DISTINCT case_id FROM case_evidence ORDER BY case_id LIMIT 33")]
        if len(ids)>32:raise AnalysisError("case catalog limit exceeded")
        output=[]
        for case_id in ids:
            item={"case_id":case_id,"evidence_state":"provisional","sample":False,"batch_confirmed":False}
            item.update({"versions":self.versions(case_id)} if self.version_count(case_id)<=32 else self.version_page(case_id))
            output.append(item)
        return output


def validate_case_request(request):
    if not isinstance(request, dict) or request.get("operation") not in CASE_OPERATIONS:
        raise AnalysisError("unsupported case operation")
    operation = request["operation"]
    required = {"contract", "operation"}
    if operation != "list_cases":
        required.add("case_id")
    if operation not in {"list_cases", "case_versions", "case_observations"}:
        required.add("evidence_id")
    if operation == "page_contribution":
        required.add("page_key")
    strict_keys(request, required,{"limit","before_sequence"} if operation=="case_versions" else set())
    if "limit" in request and (type(request["limit"]) is not int or not 1<=request["limit"]<=32):raise AnalysisError("invalid history limit")
    if "before_sequence" in request and (type(request["before_sequence"]) is not int or not 1<=request["before_sequence"]<=2**63-1):raise AnalysisError("invalid history cursor")
    if request["contract"] != "batch-service/v1":
        raise AnalysisError("unsupported service contract")
    if "case_id" in request:
        identifier(request["case_id"])
    for key in ("evidence_id", "page_key"):
        if key in request:
            digest(request[key])
    return request


def scoped_completeness(record):
    from website_analytics.analysis.completeness import project_records
    packet=record["source_packet"]
    result=project_records(packet.get("worker_window_evidence",[]),as_of=timestamp(packet["queried_at"]),
        observations=packet.get("worker_observations",[]),source_ids=packet.get("worker_source_ids",{}))
    result["metadata_diagnostics"]=packet.get("worker_metadata_diagnostics",[])
    return result


def _case_observations(store, case_id):
    groups={};scopes={}
    def contains_metrics(value):
        from website_analytics.analysis.query import METRICS
        keys=set().union(*METRICS.values())
        if isinstance(value,dict):
            return any(k in keys and type(v) in {int,float} for k,v in value.items()) or any(contains_metrics(v) for v in value.values())
        return isinstance(value,list) and any(contains_metrics(v) for v in value)
    # The immutable ID set is captured once; later appends cannot change this
    # response's selection. Every referenced payload is independently verified.
    for version in store.observation_versions(case_id):
        record=store.get(case_id,version["evidence_id"])
        packet=record["source_packet"];records=packet.get("worker_window_evidence",[])
        windows=packet.get("weeks")
        if windows is None:windows=[period for scenario in packet["scenarios"] for period in scenario["periods"].values()]
        identity={"kind":record["kind"],"intervention":record["current_intervention"],"source_ids":packet.get("worker_source_ids",{}),
            "urls":record["draft"]["raw_urls"],"variants":packet["variant_paths_not_merged"],
            "windows":[{k:row[k] for k in ("start","end")} for row in windows]}
        scope_id=fingerprint(identity);facets=scoped_completeness(record)
        scopes[scope_id]={"kind":record["kind"],"windows":identity["windows"],"source_ids":identity["source_ids"],
            "page_count":len(identity["urls"]),"variant_count":len(identity["variants"]),"intervention_digest":fingerprint(identity["intervention"])}
        item={"evidence_id":version["evidence_id"],"observed_at":packet["queried_at"],"evidence_state":"provisional",
            "scoped_completeness_contract":facets["contract"],"strict_complete":facets["strict_complete"],"status":facets["status"]}
        has_metrics=any(row["evidence"]["metrics"] and row["evidence"]["status"] not in {"error","unavailable"} for row in records) if records else contains_metrics(packet)
        groups.setdefault(scope_id,[]).append((item,has_metrics))
    result=[]
    def latest(items):
        if not items:return None,[]
        cutoff=max(timestamp(item["observed_at"]) for item in items)
        tied=sorted([item for item in items if timestamp(item["observed_at"])==cutoff],key=lambda x:x["evidence_id"])
        return (tied[0] if len(tied)==1 else None),[item["evidence_id"] for item in tied] if len(tied)>1 else []
    for scope_id,items in sorted(groups.items()):
        observed,conflicts=latest([item for item,has_metrics in items if has_metrics])
        strict,strict_conflicts=latest([item for item,_ in items if item["strict_complete"]])
        attempt,attempt_conflicts=latest([item for item,_ in items])
        result.append({"scope_id":scope_id,"scope":scopes[scope_id],"latest_provisional_observation":observed,"strict_complete":strict,
            "latest_attempt":attempt,"observation_time_conflicts":conflicts,"strict_time_conflicts":strict_conflicts,
            "attempt_time_conflicts":attempt_conflicts,"selection_basis":"captured_time_per_exact_case_scope_not_import_order"})
    return result


def query_case(store: CaseStore, request):
    raw = validate_case_request(request)
    operation = raw["operation"]
    response = {"contract": "batch-service/v1", "operation": operation, "status": "ok"}
    if operation == "list_cases":
        return {**response, "cases": store.list_cases()}
    case_id = raw["case_id"]
    response["case_id"] = case_id
    if operation == "case_versions":
        response.update({k:raw[k] for k in ("limit","before_sequence") if k in raw})
        if "limit" in raw or "before_sequence" in raw or store.version_count(case_id)>32:return {**response,**store.version_page(case_id,limit=raw.get("limit",20),before_sequence=raw.get("before_sequence"))}
        return {**response, "versions": store.versions(case_id)}
    if operation == "case_observations":
        response.update({"selection_scan_limit":64,"older_versions_not_scanned":store.version_count(case_id)>64,"strict_selection_scope":"bounded_recent_captured_versions_history_remains_pageable"} if store.version_count(case_id)>32 else {})
        return {**response,"case_id":case_id,"groups":_case_observations(store,case_id),"live_sources":False,
            "strict_completeness_is_not_batch_confirmation_or_causality":True}
    record = store.get(case_id, raw["evidence_id"])
    report = render_case(record)
    response.update(evidence_id=raw["evidence_id"], evidence_state="provisional", sample=False,
                    batch_confirmed=False, interpretation=report["interpretation"],
                    report_digest=report["report_digest"], pages=page_choices(record))
    if record["source_packet"]["runtime"].get("contract")=="runtime-provenance/v2":
        response["runtime_provenance"]=record["source_packet"]["runtime"]
    # Request success is distinct from source-window completeness. Preserve the
    # pinned report bytes/digest while projecting its captured worker metadata
    # consistently for detail, trend and page clients. Historical cases without
    # worker evidence retain their existing response shape.
    assessments = report.get("worker_window_evidence")
    quality = report.get("source_quality")
    if assessments is not None:
        response["scoped_completeness"]=scoped_completeness(record)
        from website_analytics.analysis.query import METRICS
        eligible = bool(assessments) and all(row["status"] == "complete" and row["comparison_eligible"]
            and set(METRICS[row["source"]]) <= row["metrics"].keys() for row in assessments)
        response.update(source_window_assessments=assessments,
            data_completeness={"status": "complete" if eligible else "partial",
                "comparison_eligible": eligible, "basis": "captured_worker_window_assessments",
                "batch_confirmation_or_causal_claim": False},
            source_assessment_contract="case-source-assessments/v1")
        if quality is not None:
            quality = {**quality, "ga4_property_timezones": sorted({row["date_boundary"] for row in assessments
                if row["source"] == "ga4" and row["date_boundary"] != "unverified"}),
                "timezone_basis": "captured_worker_window_metadata"}
            response["source_quality"] = quality
    if operation == "case_detail":
        packet = record["source_packet"]
        windows = packet.get("weeks")
        if windows is None:
            windows = [row for scenario in packet["scenarios"] for row in scenario["periods"].values()]
        window_evidence = []
        for row in windows:
            source_states = {}
            for source, key in (("ga4", "ga4_submitted"), ("gsc", "gsc_submitted"), ("inquiry", "inquiry")):
                if key in row:
                    envelope = row[key]
                    state = {"status": envelope["status"], "fetched_at": envelope["fetched_at"]}
                    if source == "gsc" and envelope["status"] == "ok":
                        state.update(truncated=envelope["result"]["truncated"], row_cap=envelope["result"]["row_cap"])
                    source_states[source] = state
            window_evidence.append({"start": row["start"], "end": row["end"], "sources": source_states})
        return {**response, "report": report, "source_metadata": record["source_packet"]["query_metadata"],
                "source_window_evidence": window_evidence}
    if operation == "case_trend":
        if record["kind"] != "continuous_trend":
            raise AnalysisError("this evidence version is not a weekly trend")
        return {**response, "weekly_rows": report["weekly_rows"], "source_quality": quality,
                "gsc_availability": report["gsc_availability"], "limitations": report["limitations"]}
    choices = [row for row in page_choices(record) if row["page_key"] == raw["page_key"]]
    if len(choices) != 1:
        raise KeyError("page not in evidence scope")
    choice = choices[0]
    rows = report["weekly_rows"] if record["kind"] == "continuous_trend" else list(report["preview_main"]["periods"].values())
    contribution = []
    for index, row in enumerate(rows):
        page = next(p for p in row["pages"] if p["exact_path"] == choice["exact_path"])
        ga = page.get("ga4_weekly_interval", page.get("ga4_interval"))
        gsc = page.get("gsc_page_day_sum")
        total_ga = row.get("ga4_cohort_weekly_interval", row.get("ga4"))
        total_gsc = row.get("gsc_cohort_weekly_interval", row.get("gsc_interval"))
        submitted = choice["cohort"] == "submitted"
        if record["kind"] == "continuous_trend":
            ga_verified = total_ga is not None and row["reconciliation"]["ga4_page_sessions_sum"] == total_ga.get("sessions")
            gsc_verified = row["reconciliation"]["gsc_interval_matches_page_day_counts"] is True
        else:
            period_label = ("before", "after")[index]
            checks = [item for item in report["reconciliation"] if item["period"] == period_label]
            ga_verified = any(item["source"] == "ga4" and item["sessions_match"] for item in checks)
            gsc_verified = any(item["source"] == "gsc" and item["interval_matches_page_day_clicks_impressions"] for item in checks)
        def share(metrics, total, key, verified):
            return metrics[key]/total[key] if submitted and verified and metrics is not None and total is not None and metrics.get(key) is not None and total.get(key) else None
        contribution.append({"start": row["start"], "end": row["end"], "page": page,
            "session_share_of_submitted_cohort": share(ga, total_ga, "sessions", ga_verified),
            "gsc_click_share_of_submitted_cohort": share(gsc, total_gsc, "clicks", gsc_verified),
            "gsc_impression_share_of_submitted_cohort": share(gsc, total_gsc, "impressions", gsc_verified),
            "session_denominator_reconciled": ga_verified, "gsc_denominator_reconciled": gsc_verified,
            "user_counts_non_additive_no_user_share": True})
    return {**response, "page_key": raw["page_key"], "selected_page": choice, "contribution": contribution,
            "contribution_is_descriptive_not_causal": True}
