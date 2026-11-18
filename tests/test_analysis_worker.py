import copy
import json
import re
import sqlite3
from dataclasses import asdict
from datetime import datetime,timezone
from types import SimpleNamespace

import pytest

from test_analysis_readers import site,GAClient,GSCService
from test_analysis_cases import record
from website_analytics.adapters.ga4 import GA4Adapter
from website_analytics.adapters.gsc import GSCAdapter
from website_analytics.adapters.inquiry import InquiryQueryResult
from website_analytics.analysis.readers import AdapterReader
from website_analytics.analysis.contracts import AnalysisError
from website_analytics.analysis.worker import query,RunLog,window_metadata,check_adapters,ExactFixtureReader,main
from website_analytics.analysis.case_refresh import append,build,collect,run
from website_analytics.analysis.cases import CaseStore,render_case

NOW=datetime(2026,10,29,12,tzinfo=timezone.utc)


def request():
    return {"contract":"bounded-worker/v1","site":"genemedi-net","raw_urls":["https://www.genemedi.net/i/Mixed-Example","https://www.genemedi.net/i/other"],
            "variant_paths":["/i/mixed-example"],"weeks":[{"start":"2026-10-12","end":"2026-10-18"},{"start":"2026-10-19","end":"2026-10-25"}]}


def reader():
    def row(path,n):return {"sourceUrl":"https://www.genemedi.net"+path,"storedSubmissions":n,"quarantinedSubmissions":0,"nonQuarantinedSubmissions":n}
    iq=InquiryQueryResult(daily=(),pages=(row("/i/Mixed-Example",3),row("/i/mixed-example",5)),totals={"storedSubmissions":10,"quarantinedSubmissions":0,"nonQuarantinedSubmissions":10},page_rows_truncated=False)
    return AdapterReader(site(),ga4=GA4Adapter(GAClient()),gsc=GSCAdapter(GSCService()),inquiry=SimpleNamespace(query=lambda period:iq),clock=lambda:NOW,exact_paths=True)


def test_exact_worker_case_boundary_uv_grain_unknown_finality_and_private_log(tmp_path):
    r=reader();result=query(request(),r,run_id="test",run_log=RunLog(tmp_path/"run.sqlite"),as_of=NOW)
    assert result["status"]=="partial" and len(result["records"])==12
    ga=[x for x in result["records"] if x["evidence"]["source"]=="ga4"]
    assert all(x["evidence"]["metrics"]["totalUsers"]==70 for x in ga)
    assert all(x["assessment"]["complete_through"] is None for x in ga)
    counts={x["group"]:x["evidence"]["metrics"]["storedSubmissions"] for x in result["records"] if x["evidence"]["source"]=="inquiry"}
    assert counts=={"submitted":3,"variant_0":5}
    assert (tmp_path/"run.sqlite").stat().st_mode&0o777==0o600
    with sqlite3.connect(tmp_path/"run.sqlite") as db: assert db.execute("SELECT outcome FROM events ORDER BY sequence DESC LIMIT 1").fetchone()[0]=="partial"
    for call in r.adapters["ga4"]._client.requests:
        f=call.dimension_filter.and_group.expressions[1].filter
        assert f.string_filter.case_sensitive
        if "Mixed" in f.string_filter.value:
            assert re.fullmatch(f.string_filter.value,"/i/Mixed-Example")
            assert not re.fullmatch(f.string_filter.value,"/i/mixed-example")
    gsc=r.adapters["gsc"]._service.requests[0]["body"]["dimensionFilterGroups"][0]["filters"][0]["expression"]
    assert re.fullmatch(gsc,"https://www.genemedi.net/i/Mixed-Example")
    assert not re.fullmatch(gsc,"https://www.genemedi.net/i/mixed-example")


@pytest.mark.parametrize("kind",["old_ga4","old_gsc","wrong_signature","legacy_case_mode"])
def test_unsupported_production_contract_fails_before_requests_and_is_logged(tmp_path,kind):
    r=reader();ga=r.adapters["ga4"]
    if kind=="old_ga4":r.adapters["ga4"]=SimpleNamespace(aggregate=lambda *a:None)
    elif kind=="old_gsc":r.adapters["gsc"]=SimpleNamespace(query_result=lambda *a:None)
    elif kind=="wrong_signature":r.adapters["ga4"]=SimpleNamespace(exact_scope_contract="genemedi-exact/v1",scoped_exact_aggregate=lambda:None)
    else:r.exact_paths=False
    with pytest.raises(AnalysisError):query(request(),r,run_id="failure",run_log=RunLog(tmp_path/"run.sqlite"),as_of=NOW)
    assert not ga._client.requests
    with sqlite3.connect(tmp_path/"run.sqlite") as db:assert db.execute("SELECT stage,outcome FROM events ORDER BY sequence DESC LIMIT 1").fetchone()==("preflight","failed")


@pytest.mark.parametrize("kind",["third_week","current_week","foreign","case_duplicate","extra_command"])
def test_request_is_bounded_before_any_source(tmp_path,kind):
    raw=request();r=reader()
    if kind=="third_week":raw["weeks"]*=2
    elif kind=="current_week":raw["weeks"]=[{"start":"2026-10-26","end":"2026-11-01"}]
    elif kind=="foreign":raw["raw_urls"][0]="https://tarmart.com/i/x"
    elif kind=="case_duplicate":raw["variant_paths"]=["/i/Mixed-Example"]
    else:raw["command"]="anything"
    with pytest.raises(AnalysisError):query(raw,r,run_id="bad",run_log=RunLog(tmp_path/"run.sqlite"),as_of=NOW)
    assert not r.adapters["ga4"]._client.requests


def test_metadata_bridge_preserves_unknowns_observed_gsc_and_estimated_ga4(tmp_path):
    unknown=window_metadata("inquiry",{})
    assert unknown["date_boundary"]=="unverified" and unknown["complete_through"] is None and unknown["status"]=="partial"
    estimated=window_metadata("ga4",{"timezone":"America/Los_Angeles","estimated_complete_through":"2026-10-25"})
    assert estimated["cutoff_basis"]=="configured_lag" and estimated["status"]=="partial"
    observed=window_metadata("gsc",{"timezone":"America/Los_Angeles","first_incomplete_date":"2026-10-26","availability_scope_verified":True,"truncated":False})
    assert observed["complete_through"]=="2026-10-25" and observed["cutoff_basis"]=="observed"
    with pytest.raises(AnalysisError):window_metadata("ga4",{"observed_complete_through":"2026-10-25"})
    quality=window_metadata("ga4",{"sampled":True,"thresholded":False,"truncated":False})
    assert quality["truncated"] and "source_sampled" in quality["notes"]
    r=reader()
    for week in request()["weeks"]:
        r.window_evidence[("ga4",week["start"],week["end"])]=estimated
    result=query(request(),r,run_id="estimate",run_log=RunLog(tmp_path/"run.sqlite"),as_of=NOW)
    assert all(not row["assessment"]["comparison_eligible"] for row in result["records"])


def test_reader_exception_and_source_error_are_safe_and_not_success(tmp_path):
    r=reader()
    def fail(*a,**k):raise RuntimeError("mysql://secret-private-value")
    r.adapters["ga4"]._client.run_report=fail
    result=query(request(),r,run_id="failed-source",run_log=RunLog(tmp_path/"run.sqlite"),as_of=NOW)
    assert result["status"]=="partial" and "secret-private" not in json.dumps(result)
    assert any(x["evidence"]["status"]=="error" and x["assessment"]["metrics"]=={} for x in result["records"])


def test_sample_entry_never_initializes_live_clients_and_logs_bad_fixture(tmp_path,capsys,monkeypatch):
    r=reader();raw=request();out=query(raw,r,run_id="source-stand-in",run_log=RunLog(tmp_path/"seed.sqlite"),as_of=NOW)
    fixture={"contract":"worker-evidence/v1","sample":True,"records":[{"scope":x["scope"],"evidence":x["evidence"]} for x in out["records"]]}
    fp=tmp_path/"fixture.json";fp.write_text(json.dumps(fixture));rp=tmp_path/"request.json";rp.write_text(json.dumps(raw))
    import socket
    monkeypatch.setattr(socket.socket,"connect",lambda *a,**k:(_ for _ in ()).throw(AssertionError("network forbidden")))
    args=["--request",str(rp),"--sample-evidence",str(fp),"--run-id","sample","--run-log",str(tmp_path/"log.sqlite"),"--output",str(tmp_path/"result.json"),"--as-of",NOW.isoformat()]
    assert main(args)==3
    assert json.loads((tmp_path/"result.json").read_text())["sample"] is True
    fixture["records"][0]["evidence"]["email"]="not-persist@example.invalid";fp.write_text(json.dumps(fixture))
    args[args.index("sample")]="invalid-fixture"
    assert main(args)==2
    with sqlite3.connect(tmp_path/"log.sqlite") as db:assert db.execute("SELECT stage,outcome FROM events ORDER BY sequence DESC LIMIT 1").fetchone()==("preflight","failed")


def fresh_case():
    old=record();packet=copy.deepcopy(old["source_packet"]);packet["weeks"]=packet["weeks"][:2]
    packet["gsc_page_days"]["result"]["rows"]=packet["gsc_page_days"]["result"]["rows"][:2]
    packet["queried_at"]="2026-10-29T10:00:00Z"
    def stamp(v):
        if isinstance(v,dict):
            if "fetched_at" in v:v["fetched_at"]="2026-10-29T09:00:00Z"
            for child in v.values():stamp(child)
        elif isinstance(v,list):
            for child in v:stamp(child)
    stamp(packet)
    req={"contract":"bounded-worker/v1","site":"genemedi-net","raw_urls":old["current_intervention"]["raw_urls"],"variant_paths":packet["variant_paths_not_merged"],"weeks":[{k:w[k] for k in ["start","end"]} for w in packet["weeks"]]}
    return old,req,packet


def test_provisional_builder_preserves_history_uncertainty_and_partial_evidence(tmp_path):
    old,req,packet=fresh_case();store=CaseStore(tmp_path/"cases.sqlite");eid=store.put(old);before=store.get(old["case_id"],eid)
    log=RunLog(tmp_path/"run.sqlite")
    receipt=append(old,req,packet,store,run_id="fresh",run_log=log,as_of=NOW)
    assert append(old,req,packet,store,run_id="retry",run_log=log,as_of=NOW)["evidence_id"]==receipt["evidence_id"]
    new=store.get(old["case_id"],receipt["evidence_id"]);report=render_case(new)
    assert new["current_intervention"]==old["current_intervention"] and not receipt["batch_confirmed"] and not receipt["effect_confirmed"]
    assert report["interpretation"]["october14_20"]=="completion_and_stability_unconfirmed"
    assert report["weekly_rows"][0]["pages"][0]["exact_path"]=="/i/Mixed-Example"
    assert store.get(old["case_id"],eid)==before
    packet["weeks"][0]["ga4_submitted"]={"status":"error","fetched_at":"2026-10-29T09:00:00Z","error_category":"source_failed"}
    partial=append(old,req,packet,store,run_id="partial",run_log=log,as_of=NOW)
    assert partial["source_requests_partial"] and len(store.versions(old["case_id"]))==3


@pytest.mark.parametrize("kind",["stale","url_case","windows","pii","end_date"])
def test_case_builder_refuses_scope_precision_and_stale_changes(tmp_path,kind):
    old,req,packet=fresh_case();store=CaseStore(tmp_path/"cases.sqlite");store.put(old);before=store.path.read_bytes()
    if kind=="stale":packet["queried_at"]=old["source_packet"]["queried_at"]
    elif kind=="url_case":req["raw_urls"][0]=req["raw_urls"][0].lower()
    elif kind=="windows":req["weeks"]=req["weeks"][:1]
    elif kind=="pii":packet["customer_email"]="never-store@example.invalid"
    else:old["current_intervention"]["change"]["end"]["date"]="2026-10-13"
    with pytest.raises(AnalysisError):append(old,req,packet,store,run_id="bad",run_log=RunLog(tmp_path/"log.sqlite"),as_of=NOW)
    assert store.path.read_bytes()==before


def test_bounded_collector_builds_case_without_inventing_missing_daily_grain(tmp_path):
    old,req,_=fresh_case();r=reader();log=RunLog(tmp_path/"run.sqlite")
    for week in req["weeks"]:
        r.window_evidence[("ga4",week["start"],week["end"])]=window_metadata("ga4",{"timezone":"America/Los_Angeles","truncated":False,"sampled":False,"thresholded":False})
    packet=collect(old,req,r,run_id="collect",run_log=log,runtime=old["source_packet"]["runtime"],as_of=NOW)
    assert packet["gsc_page_days"]["status"]=="error"  # Test transport lacks daily grain; never synthesize dates.
    assert packet["gsc_availability"]["status"]=="error"
    new=build(old,req,packet,as_of=NOW);report=render_case(new)
    assert report["weekly_rows"][0]["ga4_cohort_weekly_interval"]["totalUsers"]==70
    assert report["weekly_rows"][0]["pages"][0]["gsc_page_day_sum"] is None
    assert any(x["date_boundary"]=="America/Los_Angeles" for x in report["worker_window_evidence"])
    assert all(x["complete_through"] is None for x in report["worker_window_evidence"])
    assert report["interpretation"]["intervention"]["change"]["end"]["date"] is None
    forged=copy.deepcopy(new);forged["source_packet"]["worker_window_evidence"][0]["assessment"]["complete_through"]="2099-01-01"
    with pytest.raises(AnalysisError):render_case(forged)
    store=CaseStore(tmp_path/"cases.sqlite");store.put(old)
    receipt=run(old,req,reader(),store,run_id="pipeline",run_log=log,runtime=old["source_packet"]["runtime"],as_of=NOW)
    assert receipt["source_requests_partial"] and not receipt["publication_performed"]


def test_capture_clock_is_after_injected_source_reads_and_unknowns_remain(tmp_path):
    # Without a fixed test clock, the real source reader timestamps its reads
    # after the query begins. Capture/assessment must use the finishing clock.
    r=reader();r.clock=lambda:datetime.now(timezone.utc)
    result=query(request(),r,run_id="clock",run_log=RunLog(tmp_path/"log.sqlite"))
    from website_analytics.analysis.contracts import timestamp
    assert all(timestamp(x["evidence"]["fetched_at"])<=timestamp(result["captured_at"]) for x in result["records"])
    assert result["status"]=="partial"


def test_live_entry_refuses_unbounded_request_before_factory(tmp_path,monkeypatch):
    raw=request();raw["site"]="tarmart";p=tmp_path/"request.json";p.write_text(json.dumps(raw));called=[]
    monkeypatch.setattr("website_analytics.analysis.worker.registered_reader",lambda *a:called.append(True))
    assert main(["--request",str(p),"--registered-config","unused","--run-id","bad-scope","--run-log",str(tmp_path/"runs.sqlite"),"--output",str(tmp_path/"absent.json")])==2
    assert not called and not (tmp_path/"absent.json").exists()


def test_saved_case_cli_and_missing_history_are_safe(tmp_path,capsys):
    from website_analytics.analysis.case_refresh import main as case_main
    old,req,packet=fresh_case();store=CaseStore(tmp_path/"cases.sqlite");eid=store.put(old)
    p=tmp_path/"packet.json";p.write_text(json.dumps(packet));r=tmp_path/"request.json";r.write_text(json.dumps(req))
    args=["--previous-store",str(store.path),"--case-id",old["case_id"],"--previous-evidence-id",eid,"--request",str(r),"--run-id","saved","--run-log",str(tmp_path/"runs.sqlite"),"--output-store",str(store.path),"--saved-packet",str(p),"--as-of",NOW.isoformat()]
    assert case_main(args)==0
    assert json.loads(capsys.readouterr().out)["evidence_state"]=="provisional"
    assert len(store.versions(old["case_id"]))==2
    args[args.index(str(store.path),args.index("--output-store"))]=str(tmp_path/"empty.sqlite")
    assert case_main(args)==2 and not (tmp_path/"empty.sqlite").exists()


def test_ga4_response_metadata_bridge_preserves_observed_zone_without_finality(tmp_path):
    r=reader();client=r.adapters["ga4"]._client;original=client.run_report
    def report(request):
        response=original(request)
        response.metadata=SimpleNamespace(time_zone="Asia/Shanghai",sampling_metadatas=(),data_loss_from_other_row=False,subject_to_thresholding=True)
        return response
    client.run_report=report
    result=query(request(),r,run_id="metadata",run_log=RunLog(tmp_path/"log.sqlite"),as_of=NOW)
    rows=[row for row in result["records"] if row["evidence"]["source"]=="ga4"]
    assert all(row["assessment"]["date_boundary"]=="Asia/Shanghai" for row in rows)
    assert all(row["assessment"]["truncated"] and row["assessment"]["complete_through"] is None for row in rows)
    assert result["status"]=="partial"


def test_case_collector_default_clock_includes_availability_error_timestamp(tmp_path):
    old,req,_=fresh_case();r=reader();r.clock=lambda:datetime.now(timezone.utc)
    packet=collect(old,req,r,run_id="finishing-clock",run_log=RunLog(tmp_path/"log.sqlite"),runtime=old["source_packet"]["runtime"])
    from website_analytics.analysis.contracts import timestamp
    assert timestamp(packet["gsc_availability"]["fetched_at"])<=timestamp(packet["queried_at"])
    assert build(old,req,packet)["evidence_state"]=="provisional"


@pytest.mark.parametrize("operation",["case_detail","case_trend","page_contribution"])
@pytest.mark.parametrize("mode",["unknown","source_error","observed_fixture","missing_rows"])
def test_all_case_views_preserve_window_completeness_without_changing_history(tmp_path,operation,mode):
    from website_analytics.analysis.cases import query_case,page_choices
    old,req,_=fresh_case();r=reader()
    for week in req["weeks"]:
        r.window_evidence[("ga4",week["start"],week["end"]) ]={"date_boundary":"America/Los_Angeles"}
    if mode=="source_error":
        def fail(*args,**kwargs):raise RuntimeError("synthetic source unavailable")
        r.adapters["gsc"].query_exact_result=fail
    elif mode in {"observed_fixture","missing_rows"}:
        original=r.adapters["ga4"]._client.run_report
        def with_metadata(request):
            result=original(request)
            result.metadata=SimpleNamespace(time_zone="America/Los_Angeles",data_loss_from_other_row=False,subject_to_thresholding=False,sampling_metadatas=[])
            if mode=="missing_rows":result.rows=[];result.row_count=0
            return result
        r.adapters["ga4"]._client.run_report=with_metadata
        for week in req["weeks"]:
            for source in ("ga4","gsc","inquiry"):
                r.window_evidence[(source,week["start"],week["end"]) ]={"date_boundary":"America/Los_Angeles","coverage_start":week["start"],"coverage_end":week["end"],"complete_through":week["end"],"cutoff_basis":"observed"}
    store=CaseStore(tmp_path/"cases.sqlite");old_id=store.put(old)
    old_request={"contract":"batch-service/v1","operation":"case_detail","case_id":old["case_id"],"evidence_id":old_id}
    historical=query_case(store,old_request)
    packet=collect(old,req,r,run_id="projection",run_log=RunLog(tmp_path/"run.sqlite"),runtime=old["source_packet"]["runtime"],as_of=NOW)
    new=build(old,req,packet,as_of=NOW);eid=store.put(new);new=store.get(new["case_id"],eid);before=store.path.read_bytes()
    request={"contract":"batch-service/v1","operation":operation,"case_id":new["case_id"],"evidence_id":eid}
    if operation=="page_contribution":request["page_key"]=page_choices(new)[0]["page_key"]
    result=query_case(store,request)
    assert result["source_window_assessments"]==render_case(new)["worker_window_evidence"]
    complete=mode=="observed_fixture"
    assert result["data_completeness"]["status"]==("complete" if complete else "partial")
    assert result["data_completeness"]["comparison_eligible"] is complete
    assert result["data_completeness"]["batch_confirmation_or_causal_claim"] is False
    assert result["source_quality"]["ga4_property_timezones"]==["America/Los_Angeles"]
    assert result["source_quality"]["timezone_basis"]=="captured_worker_window_metadata"
    assert query_case(store,old_request)==historical and "data_completeness" not in historical
    assert store.get(new["case_id"],eid)==new and store.path.read_bytes()==before
