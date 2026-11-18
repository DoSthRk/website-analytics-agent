"""Offline refresh boundary: injected readers -> envelope -> atomic local import.

No credentials, network clients, scheduler or HTTP write operation are created.
The existing credential-free service can replay IDs from this SQLite store.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from website_analytics.analysis.contracts import AnalysisError, Query, canonical_json, fingerprint, strict_keys, timestamp
from website_analytics.analysis.readers import read_json
from website_analytics.analysis.snapshots import SnapshotStore, capture, replay, snapshot_identifier

CONTRACT = "batch-refresh/v1"


def _run_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
        raise AnalysisError("invalid refresh run identifier")
    return value


def _job(payload):
    # Exact windows/segments and batch revision; changing windows is a new job.
    return fingerprint({"batch": payload["batch"], "query": payload["query"], "sample": payload["sample"]})


def prepare(batch, query, reader, dimension, mapping=None, *, run_id, mode, as_of=None):
    """Fetch through an injected FixtureReader/AdapterReader, never during import.

    A thrown/malformed source aborts with no file writes. Valid partial/error
    envelopes are retained by import as evidence but never replace the head.
    Daily/on_demand are invocation labels, not a finality or scheduling claim.
    """
    _run_id(run_id)
    if mode not in {"daily", "on_demand"}:
        raise AnalysisError("invalid refresh mode")
    payload = capture(batch, query, reader, dimension, mapping, as_of=as_of)
    return {"contract": CONTRACT, "job_id": _job(payload), "run_id": run_id,
            "mode": mode, "snapshot": payload}


def _validate(envelope):
    raw = strict_keys(envelope, {"contract", "job_id", "run_id", "mode", "snapshot"})
    if raw["contract"] != CONTRACT or raw["mode"] not in {"daily", "on_demand"}:
        raise AnalysisError("unsupported refresh envelope")
    _run_id(raw["run_id"])
    snapshot_identifier(raw["job_id"])
    payload = raw["snapshot"]
    # Reuse full replay validation, including PII/unknown-field, site, version,
    # exact URL case, scope digest, confirmation and classification checks.
    from website_analytics.analysis.snapshots import _encode
    _encode(payload)
    report = replay(payload)
    if timestamp(payload["evaluated_at"]) > datetime.now(timezone.utc):
        raise AnalysisError("refresh capture cannot be in the future")
    if raw["job_id"] != _job(payload):
        raise AnalysisError("refresh job scope mismatch")
    return payload, report


def _summary(payload):
    return [{"source": row["source"], "scope": row["scope"], **{
        k: row["evidence"][k] for k in ("status", "fetched_at", "date_boundary",
        "coverage_start", "coverage_end", "complete_through", "cutoff_basis",
        "truncated", "missing_dates")}} for row in payload["sources"]]


def _regresses(new, old):
    previous = {(r["source"], fingerprint(r["scope"])): r["evidence"] for r in old["sources"]}
    for row in new["sources"]:
        prior = previous[(row["source"], fingerprint(row["scope"]))]
        evidence = row["evidence"]
        if timestamp(evidence["fetched_at"]) < timestamp(prior["fetched_at"]):
            return True
        if evidence["complete_through"] < prior["complete_through"]:
            return True
    return False


class RefreshStore:
    """Writer-side transaction; snapshots and head advance atomically.

    SnapshotStore and the existing HTTP service need no schema/API changes.
    Run keys are immutable per job. Retry the exact envelope idempotently;
    use a new run ID after fetching different evidence. Failed/partial refresh
    does not masquerade as a newly refreshed last-good result.
    """
    def __init__(self, path):
        self.path = Path(path)

    def import_envelope(self, envelope):
        payload, report = _validate(envelope)  # Before any filesystem mutation.
        job, run = envelope["job_id"], envelope["run_id"]
        sid, digest = fingerprint(payload), fingerprint(envelope)
        sources = _summary(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("CREATE TABLE IF NOT EXISTS snapshots (snapshot_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS refresh_runs (job_id TEXT, run_id TEXT, digest TEXT NOT NULL, receipt TEXT NOT NULL, PRIMARY KEY(job_id, run_id))")
            db.execute("CREATE TABLE IF NOT EXISTS refresh_heads (job_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, receipt TEXT NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT digest, receipt FROM refresh_runs WHERE job_id=? AND run_id=?", (job, run)).fetchone()
            if existing:
                if existing[0] != digest:
                    raise AnalysisError("refresh run conflict; no overwrite")
                # Validate referenced evidence even on duplicate imports.
                SnapshotStore(self.path).get(sid)
                return json.loads(existing[1])
            head = db.execute("SELECT snapshot_id, receipt FROM refresh_heads WHERE job_id=?", (job,)).fetchone()
            previous = SnapshotStore(self.path).get(head[0]) if head else None
            outcome = "rejected_partial" if report["status"] != "ok" else "promoted"
            if outcome == "promoted" and previous:
                if sid == head[0]:
                    outcome = "unchanged"
                elif timestamp(payload["evaluated_at"]) <= timestamp(previous["evaluated_at"]) or _regresses(payload, previous):
                    outcome = "rejected_stale"
            body = canonical_json(payload)
            saved = db.execute("SELECT payload FROM snapshots WHERE snapshot_id=?", (sid,)).fetchone()
            if saved and saved[0] != body:
                raise AnalysisError("snapshot conflict; no overwrite")
            db.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?)", (sid, body))
            receipt = {"contract": CONTRACT, "job_id": job, "run_id": run, "mode": envelope["mode"],
                "outcome": outcome, "sample": payload["sample"], "snapshot_id": sid,
                "head_snapshot_id": sid if outcome == "promoted" else (head[0] if head else None),
                "report_status": report["status"], "captured_at": payload["evaluated_at"],
                "imported_at": datetime.now(timezone.utc).isoformat(), "sources": sources,
                "data_through_label": "per_source_complete_through_not_import_time",
                "interpretation": "observational_not_causal"}
            encoded = canonical_json(receipt)
            db.execute("INSERT INTO refresh_runs VALUES (?,?,?,?)", (job, run, digest, encoded))
            if outcome == "promoted":
                db.execute("INSERT INTO refresh_heads VALUES (?,?,?) ON CONFLICT(job_id) DO UPDATE SET snapshot_id=excluded.snapshot_id, receipt=excluded.receipt", (job, sid, encoded))
        return receipt

    def status(self, job_id):
        snapshot_identifier(job_id)
        if not self.path.is_file():
            raise AnalysisError("refresh store not found")
        with sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro", uri=True) as db:
            head = db.execute("SELECT snapshot_id, receipt FROM refresh_heads WHERE job_id=?", (job_id,)).fetchone()
            last = db.execute("SELECT receipt FROM refresh_runs WHERE job_id=? ORDER BY rowid DESC LIMIT 1", (job_id,)).fetchone()
        if not last:
            raise AnalysisError("refresh job not found")
        if head:
            SnapshotStore(self.path).get(head[0])
        return {"contract": CONTRACT, "job_id": job_id, "latest_attempt": json.loads(last[0]),
                "last_complete": json.loads(head[1]) if head else None,
                "live_sources": False, "head_is_latest_attempt": bool(head and head[0] == json.loads(last[0])["snapshot_id"])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sample = commands.add_parser("prepare-sample", help="offline capture only; no automatic batch confirmation")
    for arg in ["request", "batches", "sample-evidence", "run-id", "mode", "as-of", "output"]:
        sample.add_argument("--"+arg, required=True)
    importer = commands.add_parser("import", help="validate and atomically import existing aggregate evidence")
    importer.add_argument("--input", required=True)
    importer.add_argument("--store", required=True)
    status = commands.add_parser("status", help="read last complete plus latest attempt; no fetch")
    status.add_argument("--store", required=True)
    status.add_argument("--job-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare-sample":
            from website_analytics.analysis.__main__ import ROOT
            from website_analytics.analysis.batches import BatchStore
            from website_analytics.analysis.readers import FixtureReader
            from website_analytics.page_classification import build_page_dimension, load_page_classification
            from website_analytics.product_mapping import load_product_mapping
            raw = read_json(args.request)
            batch = BatchStore(args.batches).get(raw.get("batch_id"), raw.get("revision"))
            reader = FixtureReader(args.sample_evidence)
            dimension = build_page_dimension(load_page_classification(ROOT/"config/page_classifications/genemedi-net.yaml", "genemedi-net"), reader.dimension_rows)
            mapping = load_product_mapping(ROOT/"config/product_mappings/genemedi-net.yaml", "genemedi-net")
            envelope = prepare(batch, Query.parse(raw, batch.draft), reader, dimension, mapping,
                run_id=args.run_id, mode=args.mode, as_of=timestamp(args.as_of))
            with Path(args.output).open("x", encoding="utf-8") as stream:
                stream.write(canonical_json(envelope)+"\n")
            output = {"contract": CONTRACT, "sample": True, "job_id": envelope["job_id"], "prepared_not_imported": True}
        elif args.command == "import":
            output = RefreshStore(args.store).import_envelope(read_json(args.input))
        else:
            output = RefreshStore(args.store).status(args.job_id)
        print(json.dumps(output, ensure_ascii=False, allow_nan=False))
        return 3 if output.get("outcome", "").startswith("rejected_") else 0
    except (AnalysisError, OSError, sqlite3.Error, ValueError, KeyError, TypeError, AttributeError):
        print(json.dumps({"status": "error", "error": "invalid_refresh_input_or_local_state"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
