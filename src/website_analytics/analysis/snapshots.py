"""Bounded, content-addressed, append-only evidence and deterministic replay.

Only approved aggregate source fields and effective classification are retained.
No CMS/form rows, credentials, SQL, request headers or arbitrary source payloads.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path

from website_analytics.analysis import CONTRACT_VERSION, METRIC_VERSION
from website_analytics.analysis.batches import ConfirmedBatch
from website_analytics.analysis.contracts import AnalysisError, Query, Selection, canonical_json, fingerprint, page_set, scope_document, strict_keys, text, timestamp
from website_analytics.analysis.query import METRICS, SourceEvidence, analyze
from website_analytics.page_classification import PageDimension, PageDimensionEntry
from website_analytics.product_mapping import ProductMapping, ProductRule, match_product_rule

SNAPSHOT_VERSION = "batch-snapshot/v1"
ENGINE_VERSION = "batch-engine/2"
MAX_SNAPSHOT_BYTES = 512_000
_CLASSES = {"product_page", "information_page", "technical_page", "unknown_unmapped", "invalid_broken", "pdf_asset"}
_STATUSES = {"asset", "runtime_path_rule", "template_rule_via_route_alias", "manual_override", "template_rule", "dynamic_route_rule", "invalid", "unmapped"}


def snapshot_identifier(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise AnalysisError("invalid snapshot identifier")
    return value


def _model_document(batch, query, dimension, mapping) -> dict:
    paths = set(query.selection.pages or batch.draft.pages)
    if query.include_control:
        paths.update(batch.draft.control_pages)
    rows = []
    for path in sorted(paths):
        entry = dimension.classify(path)
        rule = match_product_rule(mapping, path, entry.page_class, entry.template) if mapping else None
        product = rule.product_line_id if rule and rule.mapping_status == "approved" and entry.page_class == "product_page" else None
        rows.append({"path": path, "page_class": entry.page_class,
                     "classification_status": entry.classification_status, "product_line": product})
    return {"classification_version": dimension.version,
            "product_mapping_version": mapping.version if mapping else None,
            "effective_pages": rows}


def _models(value: object, batch, query):
    raw = strict_keys(value, {"classification_version", "product_mapping_version", "effective_pages"})
    version = text(raw["classification_version"], limit=80)
    mapping_version = raw["product_mapping_version"]
    if mapping_version is not None:
        text(mapping_version, limit=80)
    expected = set(query.selection.pages or batch.draft.pages)
    if query.include_control:
        expected.update(batch.draft.control_pages)
    rows = raw["effective_pages"]
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise AnalysisError("snapshot classification scope mismatch")
    entries, rules = {}, []
    for row in rows:
        strict_keys(row, {"path", "page_class", "classification_status", "product_line"})
        path = row["path"]
        if path not in expected or path in entries or row["page_class"] not in _CLASSES or row["classification_status"] not in _STATUSES:
            raise AnalysisError("invalid snapshot classification")
        product = row["product_line"]
        if product is not None and (mapping_version is None or row["page_class"] != "product_page" or not isinstance(product, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", product)):
            raise AnalysisError("invalid snapshot product classification")
        entries[path] = PageDimensionEntry(path, None, "", row["page_class"], row["classification_status"], "Frozen effective classification for replay")
        if product is not None:
            rules.append(ProductRule("frozen-"+str(len(rules)), len(rules), "exact_path", (path,), product, None, True, "approved", "Frozen effective mapping for replay"))
    dimension = PageDimension("genemedi-net", version, entries, {})
    mapping = ProductMapping("genemedi-net", mapping_version, (), tuple(rules)) if mapping_version is not None else None
    return dimension, mapping


class _RecordingReader:
    def __init__(self, reader):
        self.reader = reader
        self.sample, self.identity = reader.sample, reader.identity
        self.records = []

    def read(self, source, period, pages, selection):
        try:
            evidence = self.reader.read(source, period, pages, selection)
        except Exception:
            raise AnalysisError("source reader failed; no snapshot was captured") from None
        self.records.append({"source": source, "scope": scope_document(period, pages, selection),
                             "evidence": json.loads(canonical_json(asdict(evidence)))})
        return evidence


class SnapshotReader:
    """Exact pinned inputs; never calls a source or falls back to new evidence."""
    def __init__(self, sample: bool, identity: str, records: object):
        if type(sample) is not bool or not isinstance(identity, str) or not re.fullmatch(r"(?:sample-sha256:[0-9a-f]{64}|existing-adapters/v1)", identity):
            raise AnalysisError("unsupported snapshot reader identity")
        if not isinstance(records, list) or len(records) not in {6, 12}:
            raise AnalysisError("invalid snapshot source record count")
        self.sample, self.identity, self.records = sample, identity, {}
        keys = {f.name for f in fields(SourceEvidence)}
        for record in records:
            strict_keys(record, {"source", "scope", "evidence"})
            scope = strict_keys(record["scope"], {"site", "start", "end", "pages", "countries", "channels"})
            if scope["site"] != "genemedi-net" or list(page_set(scope["pages"])) != scope["pages"]:
                raise AnalysisError("invalid snapshot page scope")
            Selection.parse({"countries": scope["countries"], "channels": scope["channels"]})
            raw = strict_keys(record["evidence"], keys)
            digest = fingerprint(scope)
            key = (record["source"], digest)
            if record["source"] not in METRICS or key in self.records or raw["source"] != record["source"] or raw["scope_digest"] != digest:
                raise AnalysisError("snapshot source scope mismatch")
            self.records[key] = SourceEvidence(**raw)
        self.used = set()

    def read(self, source, period, pages, selection):
        key = (source, fingerprint(scope_document(period, pages, selection)))
        if key not in self.records:
            raise AnalysisError("snapshot has no exact source scope")
        self.used.add(key)
        return self.records[key]


def capture(batch: ConfirmedBatch, query: Query, reader, dimension, mapping=None,
            *, as_of: datetime | None = None) -> dict:
    """Read sources once, freeze approved inputs, replay before any persistence.

    Source error/partial envelopes are valid evidence. A malformed envelope or
    uncaught reader exception aborts capture; an older snapshot is never changed.
    """
    batch = ConfirmedBatch.parse(batch.document())
    query = Query.parse(query.document(), batch.draft)
    recorder = _RecordingReader(reader)
    initial = analyze(batch, query, recorder, dimension, mapping, as_of=as_of)
    model = _model_document(batch, query, dimension, mapping)
    frozen_dimension, frozen_mapping = _models(model, batch, query)
    frozen_reader = SnapshotReader(reader.sample, reader.identity, recorder.records)
    evaluated_at = timestamp(initial["generated_at"])
    report = analyze(batch, query, frozen_reader, frozen_dimension, frozen_mapping, as_of=evaluated_at)
    if report["result_digest"] != initial["result_digest"]:
        raise AnalysisError("frozen model does not reproduce initial analysis")
    payload = {"snapshot_contract": SNAPSHOT_VERSION, "query_contract": CONTRACT_VERSION,
        "metric_version": METRIC_VERSION, "engine_version": ENGINE_VERSION,
        "sample": reader.sample, "evaluated_at": evaluated_at.isoformat(),
        "batch": batch.document(), "query": query.document(), "model": model,
        "reader_identity": reader.identity, "sources": recorder.records,
        "expected_result_digest": report["result_digest"]}
    _encode(payload)
    replay(payload)
    return payload


def replay(payload: object) -> dict:
    raw = strict_keys(payload, {"snapshot_contract", "query_contract", "metric_version", "engine_version", "sample", "evaluated_at", "batch", "query", "model", "reader_identity", "sources", "expected_result_digest"})
    if (raw["snapshot_contract"], raw["query_contract"], raw["metric_version"], raw["engine_version"]) != (SNAPSHOT_VERSION, CONTRACT_VERSION, METRIC_VERSION, ENGINE_VERSION):
        raise AnalysisError("unsupported pinned analysis version")
    batch = ConfirmedBatch.parse(raw["batch"])
    query = Query.parse(raw["query"], batch.draft)
    reader = SnapshotReader(raw["sample"], raw["reader_identity"], raw["sources"])
    dimension, mapping = _models(raw["model"], batch, query)
    report = analyze(batch, query, reader, dimension, mapping, as_of=timestamp(raw["evaluated_at"]))
    if len(reader.used) != len(reader.records) or snapshot_identifier(raw["expected_result_digest"]) != report["result_digest"]:
        raise AnalysisError("snapshot replay integrity mismatch")
    return report


def _encode(payload: object) -> str:
    data = canonical_json(payload)
    if len(data.encode()) > MAX_SNAPSHOT_BYTES:
        raise AnalysisError("snapshot exceeds aggregate evidence size limit")
    return data


class SnapshotStore:
    """Logical append-only SQLite records with hash verification on every read.

    Not a WORM archive or a signature. Local administrators can alter files;
    content hashes detect accidental changes, not a malicious administrator.
    """
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def put(self, payload: object) -> str:
        body = _encode(payload)
        replay(payload)  # Validate completely before creating/touching the store.
        digest = fingerprint(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS snapshots (snapshot_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT payload FROM snapshots WHERE snapshot_id=?", (digest,)).fetchone()
            if existing is None:
                db.execute("INSERT INTO snapshots VALUES (?,?)", (digest, body))
            elif existing[0] != body:
                raise AnalysisError("existing snapshot integrity mismatch; no overwrite")
        return digest

    def get(self, snapshot_id: str) -> dict:
        snapshot_identifier(snapshot_id)
        if not self.path.is_file():
            raise AnalysisError("snapshot store not found")
        with sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro", uri=True) as db:
            size = db.execute("SELECT length(CAST(payload AS BLOB)) FROM snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
            if size is None:
                raise KeyError("snapshot not found")
            if size[0] > MAX_SNAPSHOT_BYTES:
                raise AnalysisError("stored snapshot exceeds size limit")
            row = db.execute("SELECT payload FROM snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        payload = json.loads(row[0])
        if fingerprint(payload) != snapshot_id:
            raise AnalysisError("stored snapshot hash mismatch")
        replay(payload)
        return payload

    def report(self, snapshot_id: str) -> dict:
        return replay(self.get(snapshot_id))
