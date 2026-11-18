import http.client
import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from test_analysis_snapshots import payload
from website_analytics.analysis.client import query_snapshot
from website_analytics.analysis.contracts import AnalysisError
from website_analytics.analysis.service import SERVICE_VERSION, make_server
from website_analytics.analysis.snapshots import SnapshotStore

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def running(store):
    server = make_server(store, 0)
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


def post(port, body, *, path="/v1/query", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    if not isinstance(body, (bytes, str)):
        body = json.dumps(body)
    try:
        connection.request("POST", path, body=body, headers={"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def request_for(sid):
    return {"contract": SERVICE_VERSION, "operation": "replay", "snapshot_id": sid}


def test_two_independent_clients_same_version_and_service_restart_are_identical_readonly(tmp_path):
    store = SnapshotStore(tmp_path/"snapshots.sqlite")
    sid = store.put(payload(tmp_path))
    original = store.path.read_bytes()
    with running(store) as port:
        first = query_snapshot(sid, port=port)  # urllib client
        status, second = post(port, request_for(sid))  # independent http.client connection
        assert status == 200 and second == first
        env = {**os.environ, "PYTHONPATH": str(ROOT/"src")}
        process = subprocess.run([sys.executable, "-m", "website_analytics.analysis.client", "--snapshot-id", sid, "--port", str(port)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
        assert process.returncode == 0
        assert json.loads(process.stdout) == first  # independent process/agent-style client
    with running(SnapshotStore(store.path)) as port:
        assert query_snapshot(sid, port=port) == first
    assert store.path.read_bytes() == original


@pytest.mark.parametrize("extra", [{"sql": "SELECT secret"}, {"file": "/etc/private"}, {"command": "ssh root@server"}, {"query": {"site": "tarmart"}}, {"url": "https://external.invalid"}])
def test_request_cannot_choose_sql_paths_commands_sources_or_urls(tmp_path, extra):
    store = SnapshotStore(tmp_path/"snapshots.sqlite")
    sid = store.put(payload(tmp_path))
    original = store.path.read_bytes()
    with running(store) as port:
        status, result = post(port, {**request_for(sid), **extra})
        assert status == 400 and result["status"] == "error"
        assert not any(str(value) in json.dumps(result) for value in extra.values())
    assert store.path.read_bytes() == original


@pytest.mark.parametrize("sid", ["../../private", "file:///etc/private", "http://external.invalid", "A"*64, "0"*63])
def test_invalid_snapshot_path_or_identifier_is_rejected(tmp_path, sid):
    store = SnapshotStore(tmp_path/"snapshots.sqlite")
    store.put(payload(tmp_path))
    with running(store) as port:
        status, result = post(port, request_for(sid))
        assert status == 400
        assert sid not in json.dumps(result)


@pytest.mark.parametrize("headers,status", [({"Origin": "https://external.invalid"}, 403), ({"Host": "external.invalid"}, 403), ({"Sec-Fetch-Site": "cross-site"}, 403), ({"Content-Type": "text/plain"}, 415), ({"Transfer-Encoding": "chunked"}, 400)])
def test_cross_site_context_and_ambiguous_framing_rejected(tmp_path, headers, status):
    store = SnapshotStore(tmp_path/"snapshots.sqlite")
    sid = store.put(payload(tmp_path))
    with running(store) as port:
        actual, _ = post(port, request_for(sid), headers=headers)
        assert actual == status


def test_error_unknown_operation_duplicate_json_oversize_and_unknown_routes(tmp_path):
    store = SnapshotStore(tmp_path/"snapshots.sqlite")
    sid = store.put(payload(tmp_path))
    with running(store) as port:
        assert post(port, {**request_for(sid), "operation": "capture"})[0] == 400
        assert post(port, request_for("0"*64))[0] == 404
        assert post(port, '{"operation":"replay","operation":"exec"}')[0] == 400
        assert post(port, "x"*4097)[0] == 413
        assert post(port, request_for(sid), path="/../../etc/private")[0] == 404
        assert post(port, request_for(sid), path="/v1/query?file=private")[0] == 404
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("DELETE", "/v1/query")
        response = connection.getresponse()
        assert response.status == 405
        response.read()
        connection.close()
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["live_sources"] is False
        connection.close()


def test_partial_snapshot_is_identical_and_withholds_unknown_effect_for_both_clients(tmp_path):
    def patch(raw):
        raw["records"][0]["evidence"]["truncated"] = True
    store = SnapshotStore(tmp_path/"snapshots.sqlite")
    sid = store.put(payload(tmp_path, patch))
    with running(store) as port:
        first = query_snapshot(sid, port=port)
        status, second = post(port, request_for(sid))
        assert first == second and first["status"] == "partial" and status == 200
        metric = first["report"]["cohorts"]["changed"]["comparison"]["metrics"]["ga4"]["sessions"]
        assert metric["delta"] is None


def test_service_start_cannot_create_store_or_bind_arbitrary_host(tmp_path):
    store = SnapshotStore(tmp_path/"missing.sqlite")
    with pytest.raises(AnalysisError):
        make_server(store)
    assert not store.path.exists()
    for port in (-1, 65536, True):
        with pytest.raises(AnalysisError):
            make_server(store, port)
    with pytest.raises(TypeError):
        make_server(store, host="0.0.0.0")


def test_double_content_length_is_rejected(tmp_path):
    store = SnapshotStore(tmp_path/"snapshots.sqlite")
    sid = store.put(payload(tmp_path))
    with running(store) as port:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        body = json.dumps(request_for(sid)).encode()
        connection.putrequest("POST", "/v1/query")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(body)))
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        assert response.status == 400
        response.read()
        connection.close()
