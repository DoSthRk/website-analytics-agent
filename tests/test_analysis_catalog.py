"""Refresh discovery -> latest/pinned replay, synthetic only, loopback only."""
import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from test_analysis_refresh import envelope, inputs
from test_analysis_case_service import raw_query
from test_analysis_cases import record
from website_analytics.analysis.catalog import RefreshCatalog
from website_analytics.analysis.cases import CaseStore
from website_analytics.analysis.client import query_refresh, query_case, query_snapshot
from website_analytics.analysis.contracts import AnalysisError
from website_analytics.analysis.refresh import RefreshStore, prepare
from website_analytics.analysis.service import make_server
from website_analytics.analysis.snapshots import SnapshotStore

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def running(path, cases=None):
    server = make_server(SnapshotStore(path), 0, refreshes=RefreshCatalog(path), cases=cases)
    assert server.server_address[0] == "127.0.0.1"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try: yield server.server_port
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
        assert not thread.is_alive()


def request(operation, **fields):
    return {"contract": "batch-service/v1", "operation": operation, **fields}


def test_discovery_latest_history_two_clients_process_and_restart(tmp_path):
    packet = envelope(tmp_path)
    store = RefreshStore(tmp_path/"refresh.sqlite")
    first = store.import_envelope(packet)
    second = store.import_envelope(envelope(tmp_path, run="second", as_of=datetime(2026, 8, 22, tzinfo=timezone.utc)))
    before = store.path.read_bytes()
    with running(store.path) as port:
        jobs = query_refresh("refresh_jobs", port=port)
        status, other = raw_query(port, request("refresh_jobs"))
        assert status == 200 and jobs == other and len(jobs["jobs"]) == 1
        job = jobs["jobs"][0]["job_id"]
        latest = query_refresh("refresh_latest", job_id=job, port=port)
        status, other = raw_query(port, request("refresh_latest", job_id=job))
        assert status == 200 and latest == other and latest["snapshot_id"] == second["snapshot_id"]
        assert latest["last_complete"]["sources"][0]["complete_through"] == "2026-08-19"
        assert latest["last_complete"]["sources"][0]["fetched_at"] == "2026-08-20T12:00:00+00:00"
        assert "pages" not in latest["last_complete"]["sources"][0]["scope"]
        assert latest["last_complete"]["sources"][0]["scope"]["page_count"] == 2
        assert "freshness_sla_not_configured" in latest["limitations"]
        assert latest["report"]["visitor_label"] == "ordinary_traffic_target_visitors_undefined"
        pinned = query_refresh("refresh_snapshot", job_id=job, snapshot_id=first["snapshot_id"], port=port)
        assert pinned["report"] == query_snapshot(first["snapshot_id"], port=port)["report"]
        versions = query_refresh("refresh_versions", job_id=job, limit=1, port=port)
        assert versions["versions"][0]["snapshot_id"] == second["snapshot_id"]
        older = query_refresh("refresh_versions", job_id=job, before_sequence=versions["next_before_sequence"], port=port)
        assert older["versions"][0]["snapshot_id"] == first["snapshot_id"]
        proc = subprocess.run([sys.executable, "-m", "website_analytics.analysis.client", "--operation", "refresh_latest",
            "--job-id", job, "--port", str(port)], cwd=ROOT, env={**os.environ,"PYTHONPATH":str(ROOT/"src")}, capture_output=True, text=True, timeout=10)
        assert proc.returncode == 0 and json.loads(proc.stdout) == latest
    with running(store.path) as port:
        assert query_refresh("refresh_latest", job_id=job, port=port) == latest
        assert query_refresh("refresh_snapshot", job_id=job, snapshot_id=first["snapshot_id"], port=port) == pinned
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("kind", ["error", "truncated", "unverified_inquiry_timezone", "estimated_ga4_finality", "stale"])
def test_rejected_refresh_never_silently_replaces_complete(tmp_path, kind):
    store = RefreshStore(tmp_path/"refresh.sqlite")
    good = store.import_envelope(envelope(tmp_path))
    def patch(raw):
        row = next(r["evidence"] for r in raw["records"] if r["source"] == ("inquiry" if kind == "unverified_inquiry_timezone" else "ga4"))
        if kind == "error": row.update(status="error",metrics={})
        elif kind == "truncated": row["truncated"] = True
        elif kind == "unverified_inquiry_timezone": row["date_boundary"] = "unverified"
        elif kind == "estimated_ga4_finality": row["cutoff_basis"] = "configured_lag"
    at = datetime(2026,8,20 if kind=="stale" else 22,18,tzinfo=timezone.utc)
    failed = store.import_envelope(envelope(tmp_path, run=kind, patch=patch, as_of=at))
    with running(store.path) as port:
        latest = query_refresh("refresh_latest", job_id=good["job_id"], port=port)
        assert latest["status"] == "partial" and latest["serving_prior_complete_after_rejection"]
        assert latest["snapshot_id"] == good["snapshot_id"]
        assert latest["latest_attempt"]["snapshot_id"] == failed["snapshot_id"]
        assert latest["latest_attempt"]["outcome"].startswith("rejected_")
        pinned = query_refresh("refresh_snapshot", job_id=good["job_id"], snapshot_id=failed["snapshot_id"], port=port)
        assert pinned["receipt"]["outcome"] == failed["outcome"]


def test_first_partial_has_no_invented_complete_report(tmp_path):
    store = RefreshStore(tmp_path/"refresh.sqlite")
    bad = store.import_envelope(envelope(tmp_path, patch=lambda r:r["records"][0]["evidence"].update(truncated=True)))
    with running(store.path) as port:
        result = query_refresh("refresh_latest", job_id=bad["job_id"], port=port)
        assert result["status"] == "partial" and result["snapshot_id"] is None and result["report"] is None
        assert any(r["truncated"] is True for r in result["latest_attempt"]["sources"])


def test_query_rejects_cross_job_pinning_and_paginates_jobs(tmp_path):
    store = RefreshStore(tmp_path/"refresh.sqlite")
    first = store.import_envelope(envelope(tmp_path))
    batch, query, reader, dimension, mapping = inputs(tmp_path)
    second = store.import_envelope(prepare(batch,replace(query,include_control=False),reader,dimension,mapping,
        run_id="other-scope",mode="on_demand",as_of=datetime(2026,8,21,tzinfo=timezone.utc)))
    with running(store.path) as port:
        one = query_refresh("refresh_jobs",limit=1,port=port)
        two = query_refresh("refresh_jobs",limit=1,after_job=one["next_after_job"],port=port)
        assert len(one["jobs"]) == len(two["jobs"]) == 1
        assert {one["jobs"][0]["job_id"],two["jobs"][0]["job_id"]} == {first["job_id"],second["job_id"]}
        status,_ = raw_query(port,request("refresh_snapshot",job_id=first["job_id"],snapshot_id=second["snapshot_id"]))
        assert status == 404
        assert raw_query(port,request("refresh_latest",job_id="0"*64))[0] == 404


@pytest.mark.parametrize("extra", [{"sql":"select 1"},{"path":"/private"},{"url":"https://invalid.example"},
    {"command":"fetch"},{"limit":True},{"limit":21},{"after_job":"../private"},{"before_sequence":1}])
def test_catalog_boundary_rejects_extra_or_malformed_requests_without_writes(tmp_path, extra):
    store = RefreshStore(tmp_path/"refresh.sqlite")
    store.import_envelope(envelope(tmp_path));before=store.path.read_bytes()
    with running(store.path) as port:
        assert raw_query(port,request("refresh_jobs",**extra))[0] == 400
        for operation in ["refresh","fetch","import"]:
            assert raw_query(port,request(operation))[0] == 400
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("kind", ["head", "receipt", "payload"])
def test_catalog_corruption_fails_closed_without_fallback_or_repair(tmp_path, kind):
    store = RefreshStore(tmp_path/"refresh.sqlite")
    receipt=store.import_envelope(envelope(tmp_path))
    with sqlite3.connect(store.path) as db:
        if kind=="head": db.execute("UPDATE refresh_heads SET snapshot_id=?",("0"*64,))
        elif kind=="receipt":
            r=json.loads(db.execute("SELECT receipt FROM refresh_runs").fetchone()[0]);r["sources"][0]["complete_through"]="2099-01-01"
            db.execute("UPDATE refresh_runs SET receipt=?",(json.dumps(r),))
        else: db.execute("UPDATE snapshots SET payload='{}'")
    before=store.path.read_bytes()
    with pytest.raises(AnalysisError): RefreshCatalog(store.path).query(request("refresh_latest",job_id=receipt["job_id"]))
    assert store.path.read_bytes() == before


def test_refresh_writes_and_catalog_reads_are_transactionally_consistent(tmp_path):
    store=RefreshStore(tmp_path/"refresh.sqlite")
    first=store.import_envelope(envelope(tmp_path))
    packets=[envelope(tmp_path,run="run-"+str(i),as_of=datetime(2026,8,22+i,tzinfo=timezone.utc)) for i in range(3)]
    def writer():
        for packet in packets: store.import_envelope(packet)
    def reader():
        for _ in range(12):
            r=RefreshCatalog(store.path).query(request("refresh_latest",job_id=first["job_id"]))
            assert r["snapshot_id"] == r["latest_attempt"]["snapshot_id"] == r["last_complete"]["snapshot_id"]
            assert r["report"]["generated_at"] == r["last_complete"]["captured_at"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks=[pool.submit(writer),pool.submit(reader),pool.submit(reader)]
        for task in tasks:task.result(timeout=15)


def test_fixed_provisional_cases_coexist_without_relabelling(tmp_path):
    cases=CaseStore(tmp_path/"cases.sqlite");eid=cases.put(record())
    store=RefreshStore(tmp_path/"refresh.sqlite");store.import_envelope(envelope(tmp_path))
    original=cases.path.read_bytes()
    with running(store.path,cases) as port:
        case=query_case("case_trend",case_id="synthetic-case-test",evidence_id=eid,port=port)
        assert case["evidence_state"]=="provisional" and not case["batch_confirmed"]
        assert case["interpretation"]["october14_20"]=="completion_and_stability_unconfirmed"
        assert len(query_refresh("refresh_jobs",port=port)["jobs"])==1
    assert cases.path.read_bytes()==original


def test_reader_catalog_does_not_import_refresh_writer():
    proc=subprocess.run([sys.executable,"-c","import sys; import website_analytics.analysis.service; assert all('website_analytics.analysis.'+m not in sys.modules for m in ['refresh','worker','case_refresh','publication']); assert 'google' not in sys.modules and 'pymysql' not in sys.modules"],
        cwd=ROOT,env={**os.environ,"PYTHONPATH":str(ROOT/"src")},capture_output=True,text=True,timeout=10)
    assert proc.returncode==0


def test_abrupt_fetch_failure_does_not_claim_new_refresh_or_replace_head(tmp_path):
    store=RefreshStore(tmp_path/"refresh.sqlite");good=store.import_envelope(envelope(tmp_path))
    before=store.path.read_bytes()
    args=inputs(tmp_path)
    def fail(*a,**k): raise RuntimeError("sensitive-not-for-query")
    args[2].read=fail
    with pytest.raises(AnalysisError):
        prepare(*args,run_id="failed-fetch",mode="on_demand",as_of=datetime(2026,8,23,tzinfo=timezone.utc))
    result=RefreshCatalog(store.path).query(request("refresh_latest",job_id=good["job_id"]))
    assert result["snapshot_id"]==good["snapshot_id"] and result["last_complete"]["captured_at"]==good["captured_at"]
    assert result["latest_attempt"]["run_id"]==good["run_id"]
    assert result["latest_attempt_basis"]=="imported_envelopes_only_not_worker_health"
    assert store.path.read_bytes()==before
