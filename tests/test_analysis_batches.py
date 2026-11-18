import json
import sqlite3
from pathlib import Path

import pytest

from website_analytics.analysis.batches import BatchStore
from website_analytics.analysis.contracts import AnalysisError, BatchDraft

EXAMPLES = Path(__file__).resolve().parents[1]/"examples/batch-analysis"


def draft_data():
    return json.loads((EXAMPLES/"draft.json").read_text())


def test_confirmation_requires_exact_reviewed_digest_and_does_not_touch_on_rejection(tmp_path):
    draft = BatchDraft.parse(draft_data())
    store = BatchStore(tmp_path/"batches.sqlite")
    with pytest.raises(AnalysisError, match="changed since review"):
        store.confirm(draft, "wrong", "sample-operator")
    assert not store.path.exists()
    saved = store.confirm(draft, draft.digest, "sample-operator", confirmed_at="2026-08-20T12:00:00Z")
    content = store.path.read_bytes()
    assert store.get(draft.batch_id, 1) == saved
    assert store.path.read_bytes() == content
    with pytest.raises(AnalysisError, match="stale or duplicate"):
        store.confirm(draft, draft.digest, "sample-operator")
    assert store.get(draft.batch_id, 1) == saved


def test_correction_appends_new_revision_and_preserves_history(tmp_path):
    first = BatchDraft.parse(draft_data())
    store = BatchStore(tmp_path/"batches.sqlite")
    store.confirm(first, first.digest, "sample-operator")
    data = draft_data()
    data.update(revision=2, previous_digest=first.digest, description="Corrected synthetic change explanation")
    second = BatchDraft.parse(data)
    store.confirm(second, second.digest, "sample-operator")
    assert store.get(first.batch_id, 1).draft.description == first.description
    assert store.get(first.batch_id, 2).draft.previous_digest == first.digest
    stale = {**data, "revision": 3, "previous_digest": first.digest}
    with pytest.raises(AnalysisError, match="stale or duplicate"):
        store.confirm(BatchDraft.parse(stale), BatchDraft.parse(stale).digest, "sample-operator")


def test_read_does_not_create_ledger_and_detects_modified_document(tmp_path):
    store = BatchStore(tmp_path/"missing.sqlite")
    with pytest.raises(AnalysisError, match="not found"):
        store.get("sample", 1)
    assert not store.path.exists()
    draft = BatchDraft.parse(draft_data())
    store.confirm(draft, draft.digest, "sample-operator")
    with sqlite3.connect(store.path) as db:
        bad = draft.document()
        bad["title"] = "tampered"
        db.execute("UPDATE batches SET document=?", (json.dumps(bad),))
    with pytest.raises(AnalysisError, match="integrity"):
        store.get(draft.batch_id, 1)


def test_real_batch_cannot_claim_future_actual_publication(tmp_path):
    data = draft_data()
    data["sample"] = False
    draft = BatchDraft.parse(data)
    store = BatchStore(tmp_path/"batches.sqlite")
    with pytest.raises(AnalysisError, match="future"):
        store.confirm(draft, draft.digest, "operator", confirmed_at="2026-08-01T00:00:00Z")
    assert not store.path.exists()
