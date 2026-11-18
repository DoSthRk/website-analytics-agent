"""Unified loopback interface, using synthetic test packets only."""
import http.client
import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

from test_analysis_cases import record
from test_analysis_snapshots import payload
from website_analytics.analysis.cases import CaseStore
from website_analytics.analysis.client import query_case, query_snapshot
from website_analytics.analysis.service import make_server
from website_analytics.analysis.snapshots import SnapshotStore

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def running(cases, snapshots=None):
    server = make_server(snapshots, 0, cases=cases)
    assert server.server_address[0] == "127.0.0.1"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def raw_query(port, request):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("POST", "/v1/query", json.dumps(request), {"Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_case_catalog_detail_trend_two_clients_and_restart_are_identical(tmp_path):
    cases = CaseStore(tmp_path/"cases.sqlite")
    trend, preview = cases.put_many([record(), record("approximate_window")])
    before = cases.path.read_bytes()
    common = {"case_id": "synthetic-case-test", "evidence_id": trend}
    with running(cases) as port:
        assert len(query_case("list_cases", port=port)["cases"]) == 1
        assert len(query_case("case_versions", case_id=common["case_id"], port=port)["versions"]) == 2
        first = query_case("case_trend", **common, port=port)
        status, second = raw_query(port, {"contract": "batch-service/v1", "operation": "case_trend", **common})
        assert status == 200 and first == second
        process = subprocess.run([sys.executable, "-m", "website_analytics.analysis.client", "--operation", "case_trend",
            "--case-id", common["case_id"], "--evidence-id", trend, "--port", str(port)], cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT/"src")}, capture_output=True, text=True, timeout=10)
        assert process.returncode == 0 and json.loads(process.stdout) == first
        assert first["evidence_state"] == "provisional" and not first["batch_confirmed"]
        detail = query_case("case_detail", case_id=common["case_id"], evidence_id=preview, port=port)
        assert detail["report"]["single_event_interpretation_superseded"]
        assert detail["interpretation"]["october14_20"] == "completion_and_stability_unconfirmed"
        key = first["pages"][0]["page_key"]
        page = query_case("page_contribution", **common, page_key=key, port=port)
        assert page["contribution_is_descriptive_not_causal"]
    with running(CaseStore(cases.path)) as port:
        assert query_case("case_trend", **common, port=port) == first
    assert cases.path.read_bytes() == before


def test_old_snapshot_replay_and_real_provisional_share_one_endpoint_without_relabelling(tmp_path):
    cases = CaseStore(tmp_path/"cases.sqlite")
    case_id = cases.put(record())
    sample_dir = tmp_path/"sample"
    sample_dir.mkdir()
    snapshots = SnapshotStore(tmp_path/"snapshots.sqlite")
    snapshot_id = snapshots.put(payload(sample_dir))
    before = (cases.path.read_bytes(), snapshots.path.read_bytes())
    with running(cases, snapshots) as port:
        old = query_snapshot(snapshot_id, port=port)
        assert old["sample"] and old["evidence_state"] == "synthetic"
        real = query_case("case_detail", case_id="synthetic-case-test", evidence_id=case_id, port=port)
        assert not real["sample"] and real["evidence_state"] == "provisional" and not real["batch_confirmed"]
    assert before == (cases.path.read_bytes(), snapshots.path.read_bytes())


def test_no_http_import_fetch_path_or_unpinned_case_operation(tmp_path):
    cases = CaseStore(tmp_path/"cases.sqlite")
    evidence_id = cases.put(record())
    before = cases.path.read_bytes()
    common = {"contract": "batch-service/v1", "case_id": "synthetic-case-test", "evidence_id": evidence_id}
    bad = [({"contract": "batch-service/v1", "operation": "list_cases", "path": "/private"}, 400),
        ({**common, "operation": "import_case"}, 400), ({**common, "operation": "fetch"}, 400),
        ({**common, "operation": "case_detail", "sql": "SELECT secret"}, 400),
        ({"contract": "batch-service/v1", "operation": "case_trend", "case_id": "synthetic-case-test"}, 400),
        ({**common, "operation": "page_contribution", "page_key": "../../private"}, 400),
        ({**common, "operation": "page_contribution", "page_key": "0"*64}, 404),
        ({**common, "operation": "case_detail", "case_id": "unknown"}, 404)]
    with running(cases) as port:
        for request, expected in bad:
            status, result = raw_query(port, request)
            assert status == expected and result["status"] == "error"
            assert "/private" not in json.dumps(result) and "SELECT secret" not in json.dumps(result)
    assert cases.path.read_bytes() == before
