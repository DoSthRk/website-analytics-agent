import hashlib
import json
import socket
from pathlib import Path

from website_analytics.analysis.__main__ import main

EXAMPLES = Path(__file__).resolve().parents[1]/"examples/batch-analysis"


def test_entire_local_cli_slice_is_offline_and_query_does_not_write(tmp_path, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network access is forbidden in this local slice")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    ledger = tmp_path/"batches.sqlite"
    assert main(["draft", "--input", str(EXAMPLES/"draft.json")]) == 0
    draft = json.loads(capsys.readouterr().out)
    assert draft["status"] == "draft_not_saved" and not ledger.exists()
    assert main(["confirm", "--input", str(EXAMPLES/"draft.json"), "--digest", draft["digest"], "--confirmed-by", "synthetic-cli-test", "--store", str(ledger)]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["batch"]["sample"] is True
    before = hashlib.sha256(ledger.read_bytes()).hexdigest()
    assert main(["query", "--request", str(EXAMPLES/"query.json"), "--store", str(ledger), "--sample-evidence", str(EXAMPLES/"evidence.json")]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok" and report["sample"] is True
    assert hashlib.sha256(ledger.read_bytes()).hexdigest() == before
    assert set(p.name for p in tmp_path.iterdir()) == {"batches.sqlite"}


def test_cli_refuses_unconfirmed_batches_and_sanitizes_malformed_inputs(tmp_path, capsys):
    ledger = tmp_path/"missing.sqlite"
    assert main(["query", "--request", str(EXAMPLES/"query.json"), "--store", str(ledger), "--sample-evidence", str(EXAMPLES/"evidence.json")]) == 2
    assert not ledger.exists()
    capsys.readouterr()
    secret_input = tmp_path/"bad.json"
    secret_input.write_text('{"secret":"do-not-echo-me"}')
    assert main(["draft", "--input", str(secret_input)]) == 2
    assert "do-not-echo-me" not in capsys.readouterr().out


def test_cli_partial_exit_code_for_unavailable_exact_scope(tmp_path, capsys):
    assert main(["draft", "--input", str(EXAMPLES/"draft.json")]) == 0
    digest = json.loads(capsys.readouterr().out)["digest"]
    ledger = tmp_path/"batches.sqlite"
    assert main(["confirm", "--input", str(EXAMPLES/"draft.json"), "--digest", digest, "--confirmed-by", "synthetic-test", "--store", str(ledger)]) == 0
    capsys.readouterr()
    request = json.loads((EXAMPLES/"query.json").read_text())
    request["selection"] = {"countries": ["JP"]}
    path = tmp_path/"request.json"
    path.write_text(json.dumps(request))
    assert main(["query", "--request", str(path), "--store", str(ledger), "--sample-evidence", str(EXAMPLES/"evidence.json")]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "partial"


def test_cli_capture_and_fixed_replay_does_not_need_current_source_or_modify_snapshot(tmp_path, capsys, monkeypatch):
    assert main(["draft", "--input", str(EXAMPLES/"draft.json")]) == 0
    digest = json.loads(capsys.readouterr().out)["digest"]
    ledger = tmp_path/"batches.sqlite"
    assert main(["confirm", "--input", str(EXAMPLES/"draft.json"), "--digest", digest, "--confirmed-by", "synthetic-test", "--store", str(ledger)]) == 0
    capsys.readouterr()
    snapshots = tmp_path/"snapshots.sqlite"
    assert main(["snapshot", "--request", str(EXAMPLES/"query.json"), "--store", str(ledger), "--sample-evidence", str(EXAMPLES/"evidence.json"), "--snapshots", str(snapshots)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["saved_locally"] and result["sample"]
    before = snapshots.read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError("fixed replay must not load mutable source/config inputs")
    monkeypatch.setattr("website_analytics.analysis.__main__.FixtureReader", forbidden)
    monkeypatch.setattr("website_analytics.analysis.__main__.load_page_classification", forbidden)
    assert main(["replay", "--snapshots", str(snapshots), "--snapshot-id", result["snapshot_id"]]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["sample"] and report["status"] == "ok"
    assert snapshots.read_bytes() == before
