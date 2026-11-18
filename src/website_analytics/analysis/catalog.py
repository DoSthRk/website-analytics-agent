"""Bounded read-only discovery over offline refresh imports; never imports writer code."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from website_analytics.analysis.contracts import AnalysisError, fingerprint, strict_keys, timestamp
from website_analytics.analysis.snapshots import MAX_SNAPSHOT_BYTES, replay, snapshot_identifier

CATALOG_OPERATIONS = ("refresh_jobs", "refresh_versions", "refresh_latest", "refresh_observed", "refresh_snapshot")
_FIELDS = {"contract", "job_id", "run_id", "mode", "outcome", "sample", "snapshot_id",
           "head_snapshot_id", "report_status", "captured_at", "imported_at", "sources",
           "data_through_label", "interpretation"}
_SOURCE_FIELDS = ("status", "fetched_at", "date_boundary", "coverage_start", "coverage_end",
                  "complete_through", "cutoff_basis", "truncated", "missing_dates")
LIMITATIONS = ["saved_evidence_only_no_fetch_or_scheduler", "source_metadata_supplied_by_import_not_reverified_here",
               "per_source_day_boundaries_no_timezone_guarantee", "import_time_is_not_data_through",
               "freshness_sla_not_configured", "observational_not_causal",
               "pre_capture_fetch_failures_require_worker_run_log"]


def validate_catalog_request(request):
    if not isinstance(request, dict) or request.get("operation") not in CATALOG_OPERATIONS:
        raise AnalysisError("unsupported catalog operation")
    op = request["operation"]
    required = {"contract", "operation"}
    if op != "refresh_jobs": required.add("job_id")
    if op == "refresh_snapshot": required.add("snapshot_id")
    optional = {"limit", "after_job"} if op == "refresh_jobs" else ({"limit", "before_sequence"} if op == "refresh_versions" else set())
    strict_keys(request, required, optional)
    if request["contract"] != "batch-service/v1": raise AnalysisError("unsupported catalog contract")
    for key in ("job_id", "snapshot_id", "after_job"):
        if key in request: snapshot_identifier(request[key])
    if "limit" in request and (type(request["limit"]) is not int or not 1 <= request["limit"] <= 20):
        raise AnalysisError("invalid catalog page size")
    if "before_sequence" in request and (type(request["before_sequence"]) is not int or not 1 <= request["before_sequence"] <= 2**63-1):
        raise AnalysisError("invalid catalog cursor")
    return request


class RefreshCatalog:
    def __init__(self, path): self.path = Path(path)

    def readonly(self):
        if not self.path.is_file(): raise AnalysisError("catalog store not found")
        return sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro", uri=True)

    def _entry(self, db, row):
        sequence, job, run, digest, encoded = row
        entry = strict_keys(json.loads(encoded), _FIELDS)
        snapshot_identifier(entry["snapshot_id"])
        saved = db.execute("SELECT length(CAST(payload AS BLOB)),payload FROM snapshots WHERE snapshot_id=?", (entry["snapshot_id"],)).fetchone()
        if saved is None or saved[0] > MAX_SNAPSHOT_BYTES:
            raise AnalysisError("catalog snapshot unavailable or oversized")
        payload = json.loads(saved[1])
        if fingerprint(payload) != entry["snapshot_id"]:
            raise AnalysisError("catalog snapshot integrity mismatch")
        report = replay(payload)
        expected_job = fingerprint({"batch": payload["batch"], "query": payload["query"], "sample": payload["sample"]})
        if job != expected_job or entry["job_id"] != job or entry["run_id"] != run or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", run):
            raise AnalysisError("catalog scope mismatch")
        if entry["contract"] != "batch-refresh/v1" or entry["mode"] not in {"daily", "on_demand"} or entry["outcome"] not in {"promoted", "unchanged", "rejected_partial", "rejected_stale"}:
            raise AnalysisError("invalid catalog receipt")
        envelope = {"contract": entry["contract"], "job_id": job, "run_id": run, "mode": entry["mode"], "snapshot": payload}
        if fingerprint(envelope) != digest: raise AnalysisError("catalog envelope integrity mismatch")
        sources = [{"source": r["source"], "scope": r["scope"], **{k: r["evidence"][k] for k in _SOURCE_FIELDS}} for r in payload["sources"]]
        if (entry["sources"] != sources or type(entry["sample"]) is not bool or entry["sample"] != payload["sample"] or
                entry["captured_at"] != payload["evaluated_at"] or entry["data_through_label"] != "per_source_complete_through_not_import_time" or entry["interpretation"] != "observational_not_causal"):
            raise AnalysisError("catalog metadata integrity mismatch")
        timestamp(entry["imported_at"])
        if entry["head_snapshot_id"] is not None: snapshot_identifier(entry["head_snapshot_id"])
        if entry["report_status"] != report["status"] or (entry["outcome"] in {"promoted", "unchanged", "rejected_stale"} and report["status"] != "ok"):
            raise AnalysisError("catalog completeness mismatch")
        # Do not multiply full URL cohorts across every catalog item/source.
        # Exact page sets remain in pinned reports; metadata has a scope digest.
        summaries = []
        for source in sources:
            scope = source["scope"]
            summaries.append({**source, "scope": {k: scope[k] for k in ("site", "start", "end", "countries", "channels")} |
                {"scope_digest": fingerprint(scope), "page_count": len(scope["pages"])}})
        return {**entry, "sources": summaries, "sequence": sequence}, report

    def _state(self, db, job):
        last = db.execute("SELECT rowid,job_id,run_id,digest,receipt FROM refresh_runs WHERE job_id=? ORDER BY rowid DESC LIMIT 1", (job,)).fetchone()
        if last is None: raise KeyError("refresh job not found")
        latest, _ = self._entry(db, last)
        head = db.execute("SELECT snapshot_id,receipt FROM refresh_heads WHERE job_id=?", (job,)).fetchone()
        complete = report = None
        if head:
            row = db.execute("SELECT rowid,job_id,run_id,digest,receipt FROM refresh_runs WHERE job_id=? AND receipt=?", (job, head[1])).fetchone()
            if not row: raise AnalysisError("catalog head has no immutable receipt")
            complete, report = self._entry(db, row)
            if complete["snapshot_id"] != head[0] or complete["outcome"] != "promoted" or report["status"] != "ok":
                raise AnalysisError("invalid complete catalog head")
        return latest, complete, report

    def _facets(self,db,entry):
        from website_analytics.analysis.completeness import project_records
        body=db.execute("SELECT payload FROM snapshots WHERE snapshot_id=?",(entry["snapshot_id"],)).fetchone()[0]
        payload=json.loads(body)
        return project_records(payload["sources"],as_of=timestamp(payload["evaluated_at"]))

    def _observed(self,db,job,latest,head):
        # Bounded historical search. Import order is not observation freshness.
        rows=db.execute("SELECT rowid,job_id,run_id,digest,receipt FROM refresh_runs WHERE job_id=? ORDER BY julianday(json_extract(receipt,'$.captured_at')) DESC,rowid DESC LIMIT 65",(job,)).fetchall()
        candidates=[];seen=set();strict=[]
        for row in rows[:64]:
            entry,report=self._entry(db,row)
            if entry["snapshot_id"] in seen:continue
            seen.add(entry["snapshot_id"])
            payload=json.loads(db.execute("SELECT payload FROM snapshots WHERE snapshot_id=?",(entry["snapshot_id"],)).fetchone()[0])
            facets=self._facets(db,entry)
            if any(r["evidence"]["metrics"] and r["evidence"]["status"] not in {"error","unavailable"} for r in payload["sources"]):
                candidates.append((entry,report,facets))
            if facets["strict_complete"]:strict.append((entry,report,facets))
        candidates.sort(key=lambda item:timestamp(item[0]["captured_at"]),reverse=True)
        strict.sort(key=lambda item:timestamp(item[0]["captured_at"]),reverse=True)
        tied=[item[0]["snapshot_id"] for item in candidates if timestamp(item[0]["captured_at"])==timestamp(candidates[0][0]["captured_at"])] if candidates else []
        chosen=candidates[0] if len(tied)==1 else None
        return {"selection":"latest_saved_observation_separate_from_strict_complete",
            "status":"partial" if not chosen or not chosen[2]["strict_complete"] else "ok",
            "latest_provisional_observation":{"receipt":chosen[0],"report":chosen[1],"scoped_completeness":chosen[2]} if chosen else None,
            "strict_complete":{"receipt":strict[0][0],"report":strict[0][1],"scoped_completeness":strict[0][2]} if strict else None,
            "legacy_v1_complete_head":head,"latest_attempt":latest,"observation_time_conflicts":tied if len(tied)>1 else [],
            "selection_scan_limit":64,"older_versions_not_scanned":len(rows)>64,
            "strict_selection_scope":"bounded_loaded_versions",
            "strict_selection_status":"attested_complete_found" if strict else "not_found_within_scan",
            "serving_prior_observation_after_failed_attempt":bool(chosen and chosen[0]["snapshot_id"]!=latest["snapshot_id"])}

    def query(self, request):
        raw = validate_catalog_request(request)
        op = raw["operation"]
        response = {"contract": "batch-service/v1", "operation": op, "status": "ok", "live_sources": False,
                    "latest_attempt_basis": "imported_envelopes_only_not_worker_health",
                    "limitations": LIMITATIONS, **{k: v for k, v in raw.items() if k not in {"contract", "operation"}}}
        # One catalog read transaction ensures head and latest-attempt selection
        # cannot straddle a concurrent refresh commit. Snapshots are immutable.
        with self.readonly() as db:
            db.execute("BEGIN")
            limit = raw.get("limit", 10)
            if op == "refresh_jobs":
                jobs = db.execute("SELECT DISTINCT job_id FROM refresh_runs WHERE job_id>? ORDER BY job_id LIMIT ?", (raw.get("after_job", ""), limit+1)).fetchall()
                items = []
                for (job,) in jobs[:limit]:
                    last, head, _ = self._state(db, job)
                    items.append({"job_id": job, "sample": last["sample"], "latest_attempt": last, "last_complete": head})
                return {**response, "jobs": items, "next_after_job": jobs[limit-1][0] if len(jobs)>limit else None}
            job = raw["job_id"]
            latest, head, report = self._state(db, job)
            if op=="refresh_observed":
                return {**response,**self._observed(db,job,latest,head)}
            if op == "refresh_versions":
                rows = db.execute("SELECT rowid,job_id,run_id,digest,receipt FROM refresh_runs WHERE job_id=? AND rowid<? ORDER BY rowid DESC LIMIT ?", (job, raw.get("before_sequence", 2**63-1), limit+1)).fetchall()
                return {**response, "versions": [self._entry(db, row)[0] for row in rows[:limit]],
                        "next_before_sequence": rows[limit-1][0] if len(rows)>limit else None}
            if op == "refresh_snapshot":
                rows = db.execute("SELECT rowid,job_id,run_id,digest,receipt FROM refresh_runs WHERE job_id=? AND json_extract(receipt,'$.snapshot_id')=? ORDER BY rowid LIMIT 1", (job, raw["snapshot_id"]))
                # No arbitrary cross-job replay or unbounded response history.
                for row in rows:
                    entry = json.loads(row[4])
                    if entry.get("snapshot_id") == raw["snapshot_id"]:
                        pinned, pinned_report = self._entry(db, row)
                        return {**response, "status": pinned_report["status"], "selection": "explicit_historical_snapshot",
                                "evidence_state": "synthetic" if pinned["sample"] else "confirmed",
                                "sample": pinned["sample"], "receipt": pinned, "report": pinned_report,
                                "scoped_completeness":self._facets(db,pinned)}
                raise KeyError("snapshot not in requested refresh job")
            degraded = head is None or latest["outcome"].startswith("rejected_")
            return {**response, "status": "partial" if degraded else "ok", "selection": "last_complete_only",
                    "sample": latest["sample"], "evidence_state": "synthetic" if latest["sample"] else "confirmed",
                    "snapshot_id": head["snapshot_id"] if head else None, "last_complete": head,
                    "latest_attempt": latest, "serving_prior_complete_after_rejection": bool(head and degraded),
                    "report": report,"legacy_complete_semantics":"batch-refresh/v1_not_new_provider_finality_attestation",
                    "scoped_completeness":self._facets(db,head) if head else None}
