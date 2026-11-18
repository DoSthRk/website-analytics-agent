import copy
import hashlib
import json
import os
import sqlite3
from datetime import datetime,timezone
from pathlib import Path

import pytest

from test_analysis_refresh import envelope
from test_analysis_worker import fresh_case,NOW
from test_analysis_cases import record
from test_analysis_catalog import running
from website_analytics.analysis.refresh import RefreshStore
from website_analytics.analysis.cases import CaseStore
from website_analytics.analysis.case_refresh import append
from website_analytics.analysis.case_refresh import run
from test_analysis_worker import reader
from website_analytics.analysis.worker import RunLog
from website_analytics.analysis.catalog import RefreshCatalog
from website_analytics.analysis.client import query_refresh,query_snapshot,query_case
from website_analytics.analysis.publication import export,publish,validate,PublicationUncertain
from website_analytics.analysis.contracts import AnalysisError


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def setup(tmp_path):
    worker=tmp_path/"private";worker.mkdir(mode=0o700)
    public=tmp_path/"query";public.mkdir(mode=0o750)
    store=RefreshStore(worker/"worker.sqlite");first=store.import_envelope(envelope(worker))
    candidate=worker/"first-export.sqlite";receipt=export(store.path,candidate,"refreshes")
    assert candidate.stat().st_mode&0o777==0o400
    result=publish(candidate,public,"refreshes",expected_candidate=receipt["sha256"],expected_previous=None)
    return store,first,candidate,public,result


def test_clean_export_readonly_publish_and_two_query_clients(tmp_path):
    store,first,candidate,public,result=setup(tmp_path);target=public/"refreshes.sqlite"
    original=store.path.read_bytes();assert target.stat().st_mode&0o777==0o440
    assert result["published"] and not result["source_fetch_performed"]
    assert validate(target,"refreshes")["sha256"]==sha(candidate)==sha(target)
    with running(target) as port:
        latest=query_refresh("refresh_latest",job_id=first["job_id"],port=port)
        assert latest["snapshot_id"]==first["snapshot_id"]
        assert query_snapshot(first["snapshot_id"],port=port)["report"]==latest["report"]
    assert store.path.read_bytes()==original


def test_atomic_refresh_publication_preserves_historical_ids_and_partial_head(tmp_path):
    store,first,_,public,previous=setup(tmp_path);target=public/"refreshes.sqlite"
    def patch(raw):raw["records"][0]["evidence"].update(status="error",metrics={})
    bad=store.import_envelope(envelope(store.path.parent,run="partial",patch=patch,as_of=datetime(2026,8,22,tzinfo=timezone.utc)))
    candidate=store.path.parent/"second-export.sqlite";receipt=export(store.path,candidate,"refreshes")
    with running(target) as port:
        prior=query_refresh("refresh_latest",job_id=first["job_id"],port=port)
        publish(candidate,public,"refreshes",expected_candidate=receipt["sha256"],expected_previous=previous["sha256"])
        current=query_refresh("refresh_latest",job_id=first["job_id"],port=port)
        assert current["status"]=="partial" and current["snapshot_id"]==prior["snapshot_id"]
        assert current["latest_attempt"]["snapshot_id"]==bad["snapshot_id"]
        assert current["last_complete"]["captured_at"]==prior["last_complete"]["captured_at"]
        assert query_snapshot(first["snapshot_id"],port=port)["report"]==prior["report"]
    assert (public/".history"/("refreshes-"+previous["sha256"]+".sqlite")).is_file()


@pytest.mark.parametrize("stage",["validated","before_replace"])
def test_interrupted_publish_keeps_old_database_and_removes_temporary(tmp_path,stage):
    store,first,_,public,previous=setup(tmp_path);target=public/"refreshes.sqlite";before=target.read_bytes()
    store.import_envelope(envelope(store.path.parent,run="new",as_of=datetime(2026,8,22,tzinfo=timezone.utc)))
    candidate=store.path.parent/"new-export.sqlite";receipt=export(store.path,candidate,"refreshes")
    def interrupt(at):
        if at==stage:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):publish(candidate,public,"refreshes",expected_candidate=receipt["sha256"],expected_previous=previous["sha256"],checkpoint=interrupt)
    assert target.read_bytes()==before and not list(public.glob(".candidate-*"))
    assert RefreshCatalog(target).query({"contract":"batch-service/v1","operation":"refresh_latest","job_id":first["job_id"]})["snapshot_id"]==first["snapshot_id"]


def test_after_replace_uncertainty_requires_hash_inspection_not_blind_retry(tmp_path):
    store,first,_,public,previous=setup(tmp_path)
    store.import_envelope(envelope(store.path.parent,run="new",as_of=datetime(2026,8,22,tzinfo=timezone.utc)))
    candidate=store.path.parent/"new-export.sqlite";receipt=export(store.path,candidate,"refreshes")
    def interrupt(stage):
        if stage=="after_replace":raise KeyboardInterrupt()
    with pytest.raises(PublicationUncertain):publish(candidate,public,"refreshes",expected_candidate=receipt["sha256"],expected_previous=previous["sha256"],checkpoint=interrupt)
    assert validate(public/"refreshes.sqlite","refreshes")["sha256"]==receipt["sha256"]
    with pytest.raises(AnalysisError,match="target changed"):
        publish(candidate,public,"refreshes",expected_candidate=receipt["sha256"],expected_previous=previous["sha256"])


@pytest.mark.parametrize("kind",["extra_table","trigger","corrupt_payload","bad_hash","symlink"])
def test_invalid_or_unreviewed_evidence_cannot_replace_query_database(tmp_path,kind):
    store,first,good,public,previous=setup(tmp_path);target=public/"refreshes.sqlite";before=target.read_bytes()
    candidate=store.path.parent/"untrusted.sqlite"
    if kind=="symlink":candidate.symlink_to(good)
    else:
        candidate.write_bytes(good.read_bytes())
        if kind!="bad_hash":
            with sqlite3.connect(candidate) as db:
                if kind=="extra_table":db.execute("CREATE TABLE customers(email TEXT)")
                elif kind=="trigger":db.execute("CREATE TRIGGER forbidden AFTER INSERT ON snapshots BEGIN SELECT 1; END")
                else:db.execute("UPDATE snapshots SET payload='{}'")
    expected=sha(candidate) if kind!="bad_hash" else "0"*64
    with pytest.raises((AnalysisError,OSError)):
        publish(candidate,public,"refreshes",expected_candidate=expected,expected_previous=previous["sha256"])
    assert target.read_bytes()==before and not list(public.glob(".candidate-*"))


def test_export_rejects_private_logs_or_extra_tables_and_existing_output(tmp_path):
    store,first,candidate,public,previous=setup(tmp_path)
    with pytest.raises(AnalysisError):export(store.path,candidate,"refreshes")
    with sqlite3.connect(store.path) as db:db.execute("CREATE TABLE run_log(payload TEXT)")
    destination=store.path.parent/"should-not-exist.sqlite"
    with pytest.raises(AnalysisError):export(store.path,destination,"refreshes")
    assert not destination.exists()


def test_provisional_refresh_export_query_and_historical_case_survive(tmp_path):
    private=tmp_path/"private";private.mkdir(mode=0o700);public=tmp_path/"query";public.mkdir()
    old,req,packet=fresh_case();store=CaseStore(private/"worker-cases.sqlite");eid=store.put(old)
    new=append(old,req,packet,store,run_id="fresh",run_log=RunLog(private/"runs.sqlite"),as_of=NOW)
    candidate=private/"cases-export.sqlite";receipt=export(store.path,candidate,"cases")
    publish(candidate,public,"cases",expected_candidate=receipt["sha256"],expected_previous=None)
    from test_analysis_case_service import running as case_server
    with case_server(CaseStore(public/"cases.sqlite")) as port:
        assert len(query_case("case_versions",case_id=old["case_id"],port=port)["versions"])==2
        for version in [eid,new["evidence_id"]]:
            report=query_case("case_trend",case_id=old["case_id"],evidence_id=version,port=port)
            assert report["evidence_state"]=="provisional" and not report["batch_confirmed"]
            assert report["interpretation"]["october14_20"]=="completion_and_stability_unconfirmed"
    assert store.get(old["case_id"],eid)==old


def test_injected_source_to_provisional_export_published_query_end_to_end(tmp_path):
    private=tmp_path/"private";private.mkdir(mode=0o700);public=tmp_path/"query";public.mkdir()
    old,req,_=fresh_case();store=CaseStore(private/"worker.sqlite");old_id=store.put(old)
    receipt=run(old,req,reader(),store,run_id="source-run",run_log=RunLog(private/"runs.sqlite"),runtime=old["source_packet"]["runtime"],as_of=NOW)
    candidate=private/"export.sqlite";manifest=export(store.path,candidate,"cases")
    publish(candidate,public,"cases",expected_candidate=manifest["sha256"],expected_previous=None)
    from test_analysis_case_service import running as case_server
    with case_server(CaseStore(public/"cases.sqlite")) as port:
        new=query_case("case_detail",case_id=old["case_id"],evidence_id=receipt["evidence_id"],port=port)
        assert new["evidence_state"]=="provisional" and not new["batch_confirmed"]
        assert new["report"]["worker_window_evidence"][0]["complete_through"] is None
        assert query_case("case_trend",case_id=old["case_id"],evidence_id=old_id,port=port)["weekly_rows"]


def test_publisher_refuses_history_loss_even_when_new_database_valid(tmp_path):
    store,first,_,public,previous=setup(tmp_path)
    new_store=RefreshStore(tmp_path/"unrelated.sqlite")
    new_store.import_envelope(envelope(tmp_path,run="unrelated",as_of=datetime(2026,8,22,tzinfo=timezone.utc)))
    candidate=tmp_path/"lossy-export.sqlite";receipt=export(new_store.path,candidate,"refreshes")
    before=(public/"refreshes.sqlite").read_bytes()
    with pytest.raises(AnalysisError,match="history"):
        publish(candidate,public,"refreshes",expected_candidate=receipt["sha256"],expected_previous=previous["sha256"])
    assert (public/"refreshes.sqlite").read_bytes()==before


def test_export_and_publisher_cli_are_separate_from_source_worker(tmp_path,capsys):
    from website_analytics.analysis.publication import main
    private=tmp_path/"private";private.mkdir();public=tmp_path/"public";public.mkdir()
    store=RefreshStore(private/"worker.sqlite");store.import_envelope(envelope(private))
    output=private/"export.sqlite"
    assert main(["export","--source",str(store.path),"--output",str(output),"--kind","refreshes"])==0
    result=json.loads(capsys.readouterr().out)
    assert main(["publish","--candidate",str(output),"--directory",str(public),"--kind","refreshes","--candidate-sha",result["sha256"],"--expect-absent"])==0
    assert json.loads(capsys.readouterr().out)["published"]
