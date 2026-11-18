"""Offline refresh regressions; source data are explicitly synthetic."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from test_analysis_snapshots import inputs as snapshot_inputs, AS_OF, EXAMPLES
from website_analytics.analysis.contracts import AnalysisError
from website_analytics.analysis.refresh import RefreshStore, prepare, main
from website_analytics.analysis.snapshots import SnapshotStore
from website_analytics.analysis.service import dispatch


def inputs(tmp_path, patch=None):
    # Each source fixture is independent; confirmations themselves are immutable.
    directory = tmp_path / ("input-"+str(len(list(tmp_path.glob("input-*")))))
    directory.mkdir()
    return snapshot_inputs(directory, patch)


def envelope(tmp_path, run="daily-2026-08-21", patch=None, as_of=AS_OF):
    return prepare(*inputs(tmp_path, patch), run_id=run, mode="daily", as_of=as_of)


def test_restart_idempotence_and_readonly_query_compatibility(tmp_path):
    packet = envelope(tmp_path)
    store = RefreshStore(tmp_path/"evidence.sqlite")
    receipt = store.import_envelope(packet)
    before = store.path.read_bytes()
    assert store.import_envelope(packet) == receipt
    assert store.path.read_bytes() == before
    restarted = RefreshStore(store.path)
    assert restarted.status(packet["job_id"])["last_complete"] == receipt
    result = dispatch(SnapshotStore(store.path), {"contract": "batch-service/v1", "operation": "replay", "snapshot_id": receipt["snapshot_id"]})
    assert result["status"] == "ok" and result["sample"] is True
    assert receipt["sources"][0]["complete_through"] != receipt["imported_at"]
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("kind", ["error", "unavailable", "truncated", "missing", "estimated", "boundary"])
def test_incomplete_attempt_preserves_last_complete_and_missing_not_zero(tmp_path, kind):
    store = RefreshStore(tmp_path/"evidence.sqlite")
    good = store.import_envelope(envelope(tmp_path))
    def patch(raw):
        e = next(r["evidence"] for r in raw["records"] if r["source"] == "gsc")
        if kind in {"error", "unavailable"}: e.update(status=kind, metrics={})
        elif kind == "truncated": e["truncated"] = True
        elif kind == "missing": e["missing_dates"] = ["2026-08-03"]
        elif kind == "estimated": e["cutoff_basis"] = "configured_lag"
        else: e["date_boundary"] = "unverified"
    packet = envelope(tmp_path, run="on-demand-partial", patch=patch)
    packet["mode"] = "on_demand"
    receipt = store.import_envelope(packet)
    assert receipt["outcome"] == "rejected_partial"
    state = store.status(packet["job_id"])
    assert state["last_complete"] == good and not state["head_is_latest_attempt"]
    assert state["latest_attempt"] == receipt
    report = SnapshotStore(store.path).report(receipt["snapshot_id"])
    if kind in {"error", "unavailable"}:
        assert report["cohorts"]["changed"]["comparison"]["metrics"]["gsc"]["clicks"]["previous"] is None


def test_first_partial_has_no_head_and_no_false_latest(tmp_path):
    packet = envelope(tmp_path, patch=lambda r: r["records"][0]["evidence"].update(truncated=True))
    store = RefreshStore(tmp_path/"evidence.sqlite")
    assert store.import_envelope(packet)["outcome"] == "rejected_partial"
    assert store.status(packet["job_id"])["last_complete"] is None


def test_newer_complete_promotes_but_old_capture_and_cutoff_regression_do_not(tmp_path):
    store = RefreshStore(tmp_path/"evidence.sqlite")
    old = envelope(tmp_path, run="old")
    new = envelope(tmp_path, run="new", as_of=datetime(2026, 8, 22, tzinfo=timezone.utc))
    current = store.import_envelope(new)
    assert store.import_envelope(old)["outcome"] == "rejected_stale"
    assert store.status(old["job_id"])["last_complete"] == current
    newer = envelope(tmp_path, run="newer", as_of=datetime(2026, 8, 23, tzinfo=timezone.utc))
    assert store.import_envelope(newer)["outcome"] == "promoted"


def test_duplicate_run_conflict_cannot_overwrite_and_concurrent_import_is_single(tmp_path):
    packet = envelope(tmp_path)
    store = RefreshStore(tmp_path/"evidence.sqlite")
    # Initialize schema, then test writer serialization and exact retry receipts.
    original = store.import_envelope(packet)
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: store.import_envelope(packet), range(4)))
    assert receipts == [original]*4
    conflict = envelope(tmp_path, as_of=datetime(2026, 8, 22, tzinfo=timezone.utc))
    before = store.path.read_bytes()
    with pytest.raises(AnalysisError, match="run conflict"):
        store.import_envelope(conflict)
    assert store.path.read_bytes() == before
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM refresh_runs").fetchone()[0] == 1


def test_transaction_failure_rolls_back_snapshot_attempt_and_head(tmp_path):
    store = RefreshStore(tmp_path/"evidence.sqlite")
    first = store.import_envelope(envelope(tmp_path))
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TRIGGER fail_head BEFORE UPDATE ON refresh_heads BEGIN SELECT RAISE(ABORT, 'injected'); END")
    later = envelope(tmp_path, run="later", as_of=datetime(2026, 8, 22, tzinfo=timezone.utc))
    with pytest.raises(sqlite3.IntegrityError): store.import_envelope(later)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM refresh_runs").fetchone()[0] == 1
    assert store.status(later["job_id"])["last_complete"] == first


@pytest.mark.parametrize("kind", ["pii", "foreign_site", "url_case", "job", "version", "sample", "run_path"])
def test_invalid_import_never_creates_store(tmp_path, kind):
    packet = envelope(tmp_path)
    if kind == "pii": packet["snapshot"]["sources"][0]["evidence"]["email"] = "never-persist@example.invalid"
    elif kind == "foreign_site": packet["snapshot"]["sources"][0]["scope"]["site"] = "tarmart"
    elif kind == "url_case": packet["snapshot"]["sources"][0]["scope"]["pages"][0] = "/i/ChangedCase"
    elif kind == "job": packet["job_id"] = "0"*64
    elif kind == "version": packet["snapshot"]["engine_version"] = "future"
    elif kind == "sample": packet["snapshot"]["sample"] = False
    else: packet["run_id"] = "../../secret"
    store = RefreshStore(tmp_path/"absent.sqlite")
    with pytest.raises(AnalysisError): store.import_envelope(packet)
    assert not store.path.exists()


def test_reader_exception_does_not_touch_previous_evidence(tmp_path):
    store = RefreshStore(tmp_path/"evidence.sqlite")
    first = store.import_envelope(envelope(tmp_path))
    before = store.path.read_bytes()
    args = inputs(tmp_path)
    def fail(*a, **k): raise RuntimeError("sensitive-do-not-echo")
    args[2].read = fail
    with pytest.raises(AnalysisError) as caught:
        prepare(*args, run_id="failure", mode="on_demand", as_of=AS_OF)
    assert "sensitive" not in str(caught.value)
    assert store.path.read_bytes() == before
    assert store.status(first["job_id"])["last_complete"] == first


def test_offline_cli_preparation_import_status_and_exclusive_file(tmp_path, capsys, monkeypatch):
    import socket
    def forbidden(*a, **k): raise AssertionError("network forbidden")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    snapshot_inputs(tmp_path)
    output = tmp_path/"envelope.json"
    args = ["prepare-sample", "--request", str(EXAMPLES/"query.json"), "--batches", str(tmp_path/"batches.sqlite"),
            "--sample-evidence", str(EXAMPLES/"evidence.json"), "--run-id", "daily-2026-08-21", "--mode", "daily",
            "--as-of", AS_OF.isoformat(), "--output", str(output)]
    assert main(args) == 0
    capsys.readouterr()
    original = output.read_bytes()
    assert main(args) == 2 and output.read_bytes() == original
    capsys.readouterr()
    store = tmp_path/"snapshots.sqlite"
    assert main(["import", "--input", str(output), "--store", str(store)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert main(["status", "--store", str(store), "--job-id", receipt["job_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["last_complete"]["snapshot_id"] == receipt["snapshot_id"]


def test_cold_concurrent_imports_and_stale_source_metadata(tmp_path):
    packet = envelope(tmp_path)
    store = RefreshStore(tmp_path/"cold.sqlite")
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: store.import_envelope(packet), range(4)))
    assert receipts == [receipts[0]]*4
    for kind in ["fetch_time", "cutoff"]:
        def patch(raw):
            evidence = raw["records"][0]["evidence"]
            if kind == "fetch_time": evidence["fetched_at"] = "2026-08-20T11:00:00+00:00"
            else: evidence["complete_through"] = "2026-08-18"
        stale = envelope(tmp_path, run=kind, patch=patch, as_of=datetime(2026, 8, 22, tzinfo=timezone.utc))
        assert store.import_envelope(stale)["outcome"] == "rejected_stale"
    assert store.status(packet["job_id"])["last_complete"] == receipts[0]


def test_refresh_uses_existing_adapters_without_credentials_or_summed_uv(tmp_path, monkeypatch):
    import socket
    from test_analysis_readers import site, GAClient, GSCService, inquiry_result
    from website_analytics.adapters.ga4 import GA4Adapter
    from website_analytics.adapters.gsc import GSCAdapter
    from website_analytics.analysis.readers import AdapterReader
    from types import SimpleNamespace
    def forbidden(*a, **k): raise AssertionError("external network forbidden")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    batch, query, fixture, dimension, mapping = inputs(tmp_path)
    metadata = {}
    for row in fixture.records.values():
        assert row["complete_through"] == "2026-08-19"
    for period in [query.before, query.after]:
        for source in ["ga4", "gsc", "inquiry"]:
            metadata[(source, period.start.isoformat(), period.end.isoformat())] = {
                "date_boundary": "America/Los_Angeles" if source != "gsc" else "America/Los_Angeles",
                "coverage_start": period.start.isoformat(), "coverage_end": period.end.isoformat(),
                "complete_through": "2026-08-19", "cutoff_basis": "observed"}
    ga = GAClient()
    reader = AdapterReader(site(), ga4=GA4Adapter(ga), gsc=GSCAdapter(GSCService()),
        inquiry=SimpleNamespace(query=lambda period: inquiry_result()), window_evidence=metadata, clock=lambda: AS_OF)
    reader.sample = True  # Fake source responses remain explicitly synthetic.
    packet = prepare(batch, query, reader, dimension, mapping, run_id="adapter-test", mode="on_demand", as_of=AS_OF)
    store = RefreshStore(tmp_path/"adapters.sqlite")
    receipt = store.import_envelope(packet)
    assert receipt["outcome"] == "promoted" and receipt["sample"] is True
    report = SnapshotStore(store.path).report(receipt["snapshot_id"])
    assert report["cohorts"]["changed"]["before"]["ga4"]["metrics"]["totalUsers"] == 70
    assert len(ga.requests) == 4 and all(not list(r.dimensions) for r in ga.requests)


def test_refresh_and_two_readonly_clients_restart_without_fetch(tmp_path):
    from test_analysis_service import running, post, request_for
    from website_analytics.analysis.client import query_snapshot
    store = RefreshStore(tmp_path/"evidence.sqlite")
    receipt = store.import_envelope(envelope(tmp_path))
    sid = receipt["snapshot_id"]
    with running(SnapshotStore(store.path)) as port:
        first = query_snapshot(sid, port=port)
        status, second = post(port, request_for(sid))
        assert status == 200 and first == second
        # HTTP never exposes the refresh writer, arbitrary paths or source fetch.
        for operation in ["refresh", "import", "fetch"]:
            assert post(port, {"contract": "batch-service/v1", "operation": operation})[0] == 400
    before = store.path.read_bytes()
    with running(SnapshotStore(store.path)) as port:
        assert query_snapshot(sid, port=port) == first
    assert store.path.read_bytes() == before


def test_future_capture_rejected_before_filesystem_changes(tmp_path):
    packet = envelope(tmp_path, as_of=datetime(2099, 1, 1, tzinfo=timezone.utc))
    store = RefreshStore(tmp_path/"future.sqlite")
    with pytest.raises(AnalysisError, match="future"):
        store.import_envelope(packet)
    assert not store.path.exists()
