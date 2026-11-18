"""Focused stage-two regressions, including transaction races and stale evidence."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from website_analytics.analysis.batches import BatchStore
from website_analytics.analysis.contracts import AnalysisError, BatchDraft
from website_analytics.analysis.query import SourceEvidence
from website_analytics.models import DateRange

EXAMPLES = Path(__file__).resolve().parents[1]/"examples/batch-analysis"
NOW = datetime(2026, 8, 21, tzinfo=timezone.utc)
PERIOD = DateRange(date(2026, 8, 1), date(2026, 8, 7))


def draft():
    return BatchDraft.parse(json.loads((EXAMPLES/"draft.json").read_text()))


def evidence(**kwargs):
    return SourceEvidence("ga4", "scope", "complete", {"sessions": 10, "engagedSessions": 5},
        NOW.isoformat(), "America/Los_Angeles", "2026-08-01", "2026-08-07", "2026-08-19", "observed", **kwargs)


def test_older_document_corruption_is_detected_before_reading_or_appending_revision(tmp_path):
    store = BatchStore(tmp_path/"batches.sqlite")
    first = draft()
    store.confirm(first, first.digest, "sample-operator")
    second = replace(first, revision=2, previous_digest=first.digest, description="second")
    store.confirm(second, second.digest, "sample-operator")
    with sqlite3.connect(store.path) as db:
        bad = first.document()
        bad["description"] = "changed behind the recorded digest"
        db.execute("UPDATE batches SET document=? WHERE revision=1", (json.dumps(bad),))
    with pytest.raises(AnalysisError):
        store.get(first.batch_id, 2)
    third = replace(second, revision=3, previous_digest=second.digest)
    with pytest.raises(AnalysisError):
        store.confirm(third, third.digest, "sample-operator")


@pytest.mark.parametrize("revision", [1, 2])
def test_duplicate_or_revision_race_has_exactly_one_winner(tmp_path, revision):
    store = BatchStore(tmp_path/"batches.sqlite")
    batch = draft()
    if revision == 2:
        store.confirm(batch, batch.digest, "first")
        batch = replace(batch, revision=2, previous_digest=batch.digest, description="correction")
    def attempt(n):
        try:
            store.confirm(batch, batch.digest, "operator-"+str(n))
            return "saved"
        except AnalysisError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert results.count("saved") == 1 and results.count("conflict") == 3
    assert store.get(batch.batch_id, revision).draft.digest == batch.digest


def test_digest_confirmation_revalidates_constructed_dataclass_before_writing(tmp_path):
    batch = replace(draft(), pages=("https://evil.test/outside",))
    store = BatchStore(tmp_path/"batches.sqlite")
    with pytest.raises(AnalysisError):
        store.confirm(batch, batch.digest, "operator")
    assert not store.path.exists()


def test_unknown_day_boundary_is_not_comparison_ready():
    result = replace(evidence(), date_boundary="unverified").assess(PERIOD, "scope", sample=False, as_of=NOW)
    assert not result["comparison_eligible"]


def test_cutoff_after_fetch_time_is_rejected():
    with pytest.raises(AnalysisError):
        replace(evidence(), complete_through="2027-01-01").assess(PERIOD, "scope", sample=False, as_of=NOW)


def test_impossible_denominator_or_inquiry_partition_is_rejected():
    with pytest.raises(AnalysisError):
        replace(evidence(), metrics={"sessions": 0, "engagedSessions": 5}).assess(PERIOD, "scope", sample=False, as_of=NOW)
    with pytest.raises(AnalysisError):
        replace(evidence(), source="inquiry", metrics={"storedSubmissions": 10, "quarantinedSubmissions": 3, "nonQuarantinedSubmissions": 9}).assess(PERIOD, "scope", sample=False, as_of=NOW)
