import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from website_analytics.analysis.batches import BatchStore
from website_analytics.analysis.contracts import AnalysisError, BatchDraft, Query
from website_analytics.analysis.readers import FixtureReader
from website_analytics.analysis.snapshots import MAX_SNAPSHOT_BYTES, SnapshotStore, capture, replay
from website_analytics.page_classification import build_page_dimension, load_page_classification
from website_analytics.product_mapping import load_product_mapping

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT/"examples/batch-analysis"
AS_OF = datetime(2026, 8, 21, tzinfo=timezone.utc)


def inputs(tmp_path, patch=None):
    draft = BatchDraft.parse(json.loads((EXAMPLES/"draft.json").read_text()))
    batch = BatchStore(tmp_path/"batches.sqlite").confirm(draft, draft.digest, "synthetic-snapshot-test", confirmed_at="2026-08-20T12:00:00Z")
    query = Query.parse(json.loads((EXAMPLES/"query.json").read_text()), draft)
    raw = json.loads((EXAMPLES/"evidence.json").read_text())
    if patch:
        patch(raw)
    fixture = tmp_path/"synthetic-source.json"
    fixture.write_text(json.dumps(raw))
    reader = FixtureReader(fixture)
    dimension = build_page_dimension(load_page_classification(ROOT/"config/page_classifications/genemedi-net.yaml", "genemedi-net"), reader.dimension_rows)
    mapping = load_product_mapping(ROOT/"config/product_mappings/genemedi-net.yaml", "genemedi-net")
    return batch, query, reader, dimension, mapping


def payload(tmp_path, patch=None):
    return capture(*inputs(tmp_path, patch), as_of=AS_OF)


def test_immutable_capture_restart_replay_and_duplicate_put(tmp_path, monkeypatch):
    args = inputs(tmp_path)
    frozen = capture(*args, as_of=AS_OF)
    store = SnapshotStore(tmp_path/"snapshots.sqlite")
    snapshot_id = store.put(frozen)
    original = store.path.read_bytes()
    assert store.put(frozen) == snapshot_id
    assert store.path.read_bytes() == original
    def forbidden(*args, **kwargs):
        raise AssertionError("replay must not access sources or current configuration")
    monkeypatch.setattr(args[2], "read", forbidden)
    monkeypatch.setattr("website_analytics.page_classification.load_page_classification", forbidden)
    (tmp_path/"synthetic-source.json").unlink()
    restarted = SnapshotStore(store.path)
    first = store.report(snapshot_id)
    assert restarted.report(snapshot_id) == first == replay(frozen)
    assert first["generated_at"] == AS_OF.isoformat()
    assert first["pages"][1]["page_class"] == "unknown_unmapped"
    assert store.path.read_bytes() == original
    assert first["sample"] is True


def test_concurrent_duplicate_snapshot_has_single_immutable_record(tmp_path):
    frozen = payload(tmp_path)
    store = SnapshotStore(tmp_path/"snapshots.sqlite")
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: store.put(frozen), range(4)))
    assert len(set(ids)) == 1
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1


@pytest.mark.parametrize("kind", ["truncated", "missing", "error", "estimate"])
def test_partial_or_failed_source_is_pinned_as_partial_not_zero(tmp_path, kind):
    def patch(raw):
        row = next(r for r in raw["records"] if r["source"] == "gsc")
        evidence = row["evidence"]
        if kind == "truncated":
            evidence["truncated"] = True
        elif kind == "missing":
            evidence["missing_dates"] = ["2026-08-03"]
        elif kind == "error":
            evidence.update(status="error", metrics={})
        else:
            evidence["cutoff_basis"] = "configured_lag"
    frozen = payload(tmp_path, patch)
    store = SnapshotStore(tmp_path/"snapshots.sqlite")
    sid = store.put(frozen)
    result = store.report(sid)
    assert result["status"] == "partial"
    value = result["cohorts"]["changed"]["comparison"]["metrics"]["gsc"]["clicks"]
    assert value["delta"] is None
    assert value["previous"] == (None if kind == "error" else 10)


def test_invalid_or_abruptly_failed_capture_cannot_overwrite_good_snapshot(tmp_path):
    args = inputs(tmp_path)
    frozen = capture(*args, as_of=AS_OF)
    store = SnapshotStore(tmp_path/"snapshots.sqlite")
    sid = store.put(frozen)
    original = store.path.read_bytes()
    wrong = copy.deepcopy(frozen)
    wrong["query"]["selection"]["pages"] = ["/not-confirmed"]
    with pytest.raises(AnalysisError):
        store.put(wrong)
    def failure(*a, **kw):
        raise RuntimeError("sensitive-upstream-value-do-not-echo")
    args[2].read = failure
    with pytest.raises(AnalysisError, match="source reader failed") as caught:
        capture(*args, as_of=AS_OF)
    assert "sensitive-upstream-value" not in str(caught.value)
    assert store.path.read_bytes() == original
    assert store.report(sid)["status"] == "ok"


@pytest.mark.parametrize("kind", ["extra_pii", "scope", "version", "metric_version", "digest", "sample", "model_scope"])
def test_snapshot_contract_rejects_unknown_fields_or_relabelled_evidence(tmp_path, kind):
    frozen = payload(tmp_path)
    if kind == "extra_pii":
        frozen["sources"][0]["evidence"]["customer_email"] = "synthetic-not-persisted@example.invalid"
    elif kind == "scope":
        frozen["sources"][0]["scope"]["pages"] = ["/another-page"]
    elif kind == "version":
        frozen["engine_version"] = "unrecognized/v99"
    elif kind == "metric_version":
        frozen["metric_version"] = "batch-metrics/future"
    elif kind == "digest":
        frozen["expected_result_digest"] = "0"*64
    elif kind == "sample":
        frozen["sample"] = False
    else:
        frozen["model"]["effective_pages"][0]["path"] = "/foreign"
    store = SnapshotStore(tmp_path/"never-created.sqlite")
    with pytest.raises(AnalysisError):
        store.put(frozen)
    assert not store.path.exists()


def test_oversized_snapshot_or_invalid_identifier_never_creates_store(tmp_path):
    store = SnapshotStore(tmp_path/"absent.sqlite")
    with pytest.raises(AnalysisError):
        store.put({"padding": "x"*MAX_SNAPSHOT_BYTES})
    for invalid in ("../../secret", "file:///tmp/private", "https://evil.test", "A"*64, "0"*63):
        with pytest.raises(AnalysisError):
            store.get(invalid)
    with pytest.raises(AnalysisError):
        store.get("0"*64)
    assert not store.path.exists()


def test_modified_stored_payload_is_detected_without_repair_or_source_fallback(tmp_path):
    store = SnapshotStore(tmp_path/"snapshots.sqlite")
    frozen = payload(tmp_path)
    sid = store.put(frozen)
    frozen["query"]["include_control"] = False
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE snapshots SET payload=? WHERE snapshot_id=?", (json.dumps(frozen), sid))
    original = store.path.read_bytes()
    with pytest.raises(AnalysisError, match="hash mismatch"):
        store.get(sid)
    with pytest.raises(AnalysisError, match="no overwrite"):
        # A valid capture with same ID must never repair/overwrite corruption.
        frozen = payload_again(tmp_path)
        store.put(frozen)
    assert store.path.read_bytes() == original


def payload_again(tmp_path):
    # Reconstruct the exact same source/query/time but reuse original confirmation.
    draft = BatchDraft.parse(json.loads((EXAMPLES/"draft.json").read_text()))
    batch = BatchStore(tmp_path/"batches.sqlite").get(draft.batch_id, 1)
    query = Query.parse(json.loads((EXAMPLES/"query.json").read_text()), draft)
    reader = FixtureReader(EXAMPLES/"evidence.json")
    dimension = build_page_dimension(load_page_classification(ROOT/"config/page_classifications/genemedi-net.yaml", "genemedi-net"), reader.dimension_rows)
    mapping = load_product_mapping(ROOT/"config/product_mappings/genemedi-net.yaml", "genemedi-net")
    return capture(batch, query, reader, dimension, mapping, as_of=AS_OF)
