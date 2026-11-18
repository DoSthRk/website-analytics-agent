"""Offline scope, maturity and revision tests; all new source data synthetic."""
import copy
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest

from test_analysis_worker import reader, fresh_case, NOW
from test_analysis_refresh import envelope
from test_analysis_catalog import running, request
from website_analytics.analysis.completeness import project_records, registration_ids, validate_observations
from website_analytics.analysis.contracts import AnalysisError, fingerprint
from website_analytics.analysis.case_refresh import collect, build
from website_analytics.analysis.worker import RunLog
from website_analytics.analysis.cases import CaseStore, query_case, render_case, page_choices
from website_analytics.analysis.refresh import RefreshStore
from website_analytics.analysis.catalog import RefreshCatalog
from website_analytics.analysis.client import query_refresh


def packet(tmp_path,at=NOW,observations=None):
    old,req,_=fresh_case();r=reader();r.clock=lambda:at
    value=collect(old,req,r,run_id="synthetic",run_log=RunLog(tmp_path/"runs.sqlite"),runtime=old["source_packet"]["runtime"],as_of=at,observations=observations)
    return old,req,r,value


def observed(record,ids,source=None):
    source=source or record["evidence"]["source"]
    scope=copy.deepcopy(record["scope"])
    end=datetime.fromisoformat(scope["end"]).date()+timedelta(days=1)
    scope["end"]=end.isoformat()
    first=datetime.fromisoformat(scope["start"]).date()
    quality={"gsc":{"truncated":False},"ga4":{"sampled":False,"thresholded":False,"other_row_loss":False,"truncation_reasons_supported":False,"truncated":None,"restricted":None},"inquiry":{"truncated":False,"unknown_attribution_count":0}}[source]
    maturity={"gsc":{"basis":"gsc_api_metadata","data_state":"all","first_incomplete_date":end.isoformat()},
        "ga4":{"basis":"configured_lag","estimated_through":record["scope"]["end"],"policy_version":"synthetic-lag2/v1"},
        "inquiry":{"basis":"stored_date_observation","max_record_date":"2026-11-04"}}[source]
    return {"contract":"source-observation/v1","source":source,"source_id":ids[source],"scope":scope,"fetched_at":NOW.isoformat(),
        "calendar":{"name":"America/Los_Angeles" if source!="inquiry" else None,"basis":{"gsc":"api_contract","ga4":"api_response","inquiry":"unknown"}[source]},
        "dates_observed":[(first+timedelta(days=i)).isoformat() for i in range((end-first).days+1)],"maturity":maturity,"quality":quality}


def row_for(value,source):
    return next(r for r in value["worker_window_evidence"] if r["evidence"]["source"]==source)


def test_axes_separate_success_scope_coverage_calendar_and_provider_processing(tmp_path):
    _,_,r,value=packet(tmp_path);record=row_for(value,"gsc");obs=observed(record,registration_ids(r.site))
    view=project_records([record],as_of=NOW,observations=[obs],source_ids=value["worker_source_ids"])
    row=view["source_windows"][0]
    assert row["request"]["state"]=="succeeded"
    assert row["coverage"]["state"]=="observed_dates" and row["coverage"]["missing_rows_are_zero"] is False
    assert row["calendar"]=={"name":"America/Los_Angeles","basis":"api_contract"}
    assert row["maturity"]["state"]=="provider_final_for_window"
    assert row["maturity"]["observed_complete_through"]==record["scope"]["end"]
    assert row["quality"]["state"]=="limited" and view["strict_complete"] is False
    assert row["provenance"]["observation_id"]==fingerprint(obs)


@pytest.mark.parametrize("mismatch",["source_id","url_case","country","range"])
def test_gsc_watermark_never_crosses_scope_registration_case_or_range(tmp_path,mismatch):
    _,_,r,value=packet(tmp_path);record=row_for(value,"gsc");obs=observed(record,registration_ids(r.site))
    if mismatch=="source_id":obs["source_id"]="f"*64
    elif mismatch=="url_case":obs["scope"]["pages"][0]=obs["scope"]["pages"][0].lower()
    elif mismatch=="country":obs["scope"]["countries"]=["JP"]
    else:obs["scope"]["start"]=(datetime.fromisoformat(obs["scope"]["start"])+timedelta(days=1)).date().isoformat();obs["dates_observed"]=obs["dates_observed"][1:]
    row=project_records([record],as_of=NOW,observations=[obs],source_ids=value["worker_source_ids"])["source_windows"][0]
    assert row["maturity"]["observed_complete_through"] is None and row["strict_complete"] is False
    assert "no_matching_scope_registration_and_range_proof" in row["limitations"]


def test_absent_metadata_and_missing_dates_are_not_watermarks_or_zeroes(tmp_path):
    _,_,r,value=packet(tmp_path);record=row_for(value,"gsc");obs=observed(record,registration_ids(r.site))
    obs["maturity"]["first_incomplete_date"]=None;obs["dates_observed"].pop(1)
    row=project_records([record],as_of=NOW,observations=[obs],source_ids=value["worker_source_ids"])["source_windows"][0]
    assert row["maturity"]["state"]=="unknown" and row["maturity"]["observed_complete_through"] is None
    assert row["coverage"]["state"]=="partial_date_observation" and not row["coverage"]["missing_rows_are_zero"]
    obs["dates_observed"].reverse()
    absent=project_records([record],as_of=NOW)["source_windows"][0]
    assert absent["provenance"]["observation_id"] is None and absent["maturity"]["observed_complete_through"] is None


def test_overlapping_proofs_and_final_only_rows_do_not_invent_a_cutoff(tmp_path):
    _,_,r,value=packet(tmp_path);record=row_for(value,"gsc");one=observed(record,registration_ids(r.site));two=copy.deepcopy(one)
    two["scope"]["start"]=(datetime.fromisoformat(two["scope"]["start"])-timedelta(days=1)).date().isoformat()
    view=project_records([record],as_of=NOW,observations=[one,two],source_ids=value["worker_source_ids"])["source_windows"][0]
    assert view["maturity"]["observed_complete_through"] is None and "ambiguous_overlapping_observations" in view["limitations"]
    one["maturity"].update(first_incomplete_date=None,data_state="final")
    row=project_records([record],as_of=NOW,observations=[one],source_ids=value["worker_source_ids"])["source_windows"][0]
    assert row["maturity"]["state"]=="finalized_rows_without_watermark" and row["maturity"]["observed_complete_through"] is None


def test_gsc_window_intersecting_incomplete_date_remains_provider_partial(tmp_path):
    _,_,r,value=packet(tmp_path);record=row_for(value,"gsc");obs=observed(record,registration_ids(r.site));obs["maturity"]["first_incomplete_date"]=record["scope"]["end"]
    row=project_records([record],as_of=NOW,observations=[obs],source_ids=value["worker_source_ids"])["source_windows"][0]
    assert row["maturity"]["state"]=="provider_partial" and not row["strict_complete"]


@pytest.mark.parametrize("supported",[False,None,True])
def test_ga4_sdk_unknown_and_mature_estimate_never_mean_final(tmp_path,supported):
    _,_,r,value=packet(tmp_path);record=row_for(value,"ga4");obs=observed(record,registration_ids(r.site))
    obs["quality"]["truncation_reasons_supported"]=supported
    if supported:obs["quality"].update(truncated=False,restricted=False)
    row=project_records([record],as_of=NOW,observations=[obs],source_ids=value["worker_source_ids"])["source_windows"][0]
    assert row["maturity"]["state"]=="estimated" and row["maturity"]["observed_complete_through"] is None
    assert row["strict_complete"] is False
    assert row["quality"]["state"]==("no_observed_flags" if supported else "unknown")
    if supported is not True:assert "sdk_truncation_metadata_unknown" in row["quality"]["limitations"]


def test_conflicting_calendar_does_not_rewrite_captured_day_boundary(tmp_path):
    _,_,r,value=packet(tmp_path);record=copy.deepcopy(row_for(value,"ga4"));record["evidence"]["date_boundary"]="America/Los_Angeles"
    obs=observed(record,registration_ids(r.site));obs["calendar"]["name"]="Asia/Shanghai"
    row=project_records([record],as_of=NOW,observations=[obs],source_ids=value["worker_source_ids"])["source_windows"][0]
    assert row["calendar"]["name"]=="America/Los_Angeles" and row["provenance"]["observation_id"] is None
    assert "source_calendar_observation_conflict" in row["limitations"]


def test_inquiry_max_date_and_server_timezone_do_not_create_a_calendar_or_cutoff(tmp_path):
    _,_,r,value=packet(tmp_path);record=row_for(value,"inquiry");obs=observed(record,registration_ids(r.site))
    row=project_records([record],as_of=NOW,observations=[obs],source_ids=value["worker_source_ids"])["source_windows"][0]
    assert row["calendar"]["name"] is None and row["maturity"]["state"]=="mutable_as_of"
    assert row["maturity"]["max_record_date"]=="2026-11-04" and row["maturity"]["observed_complete_through"] is None
    assert row["strict_complete"] is False


@pytest.mark.parametrize("bad",["unknown_field","future","duplicate","unsupported_false","invented_ga4_final","gsc_timezone","gsc_date","future_scope"])
def test_metadata_schema_rejects_unbounded_future_or_fabricated_proofs(tmp_path,bad):
    _,_,r,value=packet(tmp_path);source="ga4" if bad in {"unsupported_false","invented_ga4_final"} else "gsc"
    obs=observed(row_for(value,source),registration_ids(r.site));rows=[obs]
    if bad=="unknown_field":obs["response_body"]="not accepted"
    elif bad=="future":obs["fetched_at"]=(NOW+timedelta(days=1)).isoformat()
    elif bad=="duplicate":rows.append(copy.deepcopy(obs))
    elif bad=="unsupported_false":obs["quality"]["truncated"]=False
    elif bad=="invented_ga4_final":obs["maturity"]["basis"]="provider_final"
    elif bad=="gsc_timezone":obs["calendar"]["name"]="Asia/Shanghai"
    elif bad=="future_scope":obs["scope"]["end"]=(NOW+timedelta(days=1)).date().isoformat()
    else:obs["maturity"]["first_incomplete_date"]="2026-01-01"
    with pytest.raises(AnalysisError):validate_observations(rows,as_of=NOW)


def test_case_revision_discovery_projections_and_pinned_history(tmp_path):
    old,req,r,first=packet(tmp_path);obs=observed(row_for(first,"gsc"),registration_ids(r.site));first["worker_observations"]=[obs]
    store=CaseStore(tmp_path/"cases.sqlite");old_id=store.put(old)
    one=build(old,req,first,as_of=NOW);one_id=store.put(one);digest=render_case(store.get(one["case_id"],one_id))["report_digest"]
    _,_,_,second=packet(tmp_path,at=NOW+timedelta(days=1));two=build(old,req,second,as_of=NOW+timedelta(days=1));two_id=store.put(two)
    # A late import of the older observation cannot replace the newer pointer.
    assert store.put(one)==one_id
    discovered=query_case(store,{"contract":"batch-service/v1","operation":"case_observations","case_id":one["case_id"]})
    group=next(g for g in discovered["groups"] if g["latest_provisional_observation"] and g["latest_provisional_observation"]["evidence_id"]==two_id)
    assert group["strict_complete"] is None
    facets=[]
    for operation in ["case_detail","case_trend","page_contribution"]:
        request={"contract":"batch-service/v1","operation":operation,"case_id":one["case_id"],"evidence_id":one_id}
        if operation=="page_contribution":request["page_key"]=page_choices(one)[0]["page_key"]
        result=query_case(store,request);facets.append(result["scoped_completeness"])
    assert facets[0]==facets[1]==facets[2] and not facets[0]["strict_complete"]
    assert render_case(store.get(one["case_id"],one_id))["report_digest"]==digest
    assert store.get(old["case_id"],old_id)==old


def test_case_equal_capture_time_conflict_needs_explicit_pinning(tmp_path):
    old,req,r,value=packet(tmp_path);store=CaseStore(tmp_path/"cases.sqlite")
    one=build(old,req,value,as_of=NOW);one_id=store.put(one)
    changed=copy.deepcopy(value);changed["worker_observations"]=[observed(row_for(value,"gsc"),registration_ids(r.site))]
    two_id=store.put(build(old,req,changed,as_of=NOW))
    groups=query_case(store,{"contract":"batch-service/v1","operation":"case_observations","case_id":one["case_id"]})["groups"]
    assert len(groups)==1 and groups[0]["latest_provisional_observation"] is None
    assert set(groups[0]["observation_time_conflicts"])=={one_id,two_id}


def test_failed_case_attempt_keeps_previous_provisional_observation(tmp_path):
    old,req,r,value=packet(tmp_path);store=CaseStore(tmp_path/"cases.sqlite");one=build(old,req,value,as_of=NOW);one_id=store.put(one)
    def fail(*args,**kwargs):raise RuntimeError("synthetic failure")
    r.adapters["ga4"].scoped_exact_aggregate=fail;r.adapters["gsc"].query_exact_result=fail;r.adapters["inquiry"].query=fail
    later=NOW+timedelta(days=1);r.clock=lambda:later
    failed=collect(old,req,r,run_id="failed",run_log=RunLog(tmp_path/"runs.sqlite"),runtime=old["source_packet"]["runtime"],as_of=later)
    failed_id=store.put(build(old,req,failed,as_of=later))
    group=query_case(store,{"contract":"batch-service/v1","operation":"case_observations","case_id":one["case_id"]})["groups"][0]
    assert group["latest_provisional_observation"]["evidence_id"]==one_id
    assert group["latest_attempt"]["evidence_id"]==failed_id and group["strict_complete"] is None


def test_refresh_partial_latest_observation_separate_from_legacy_complete_and_failure(tmp_path):
    store=RefreshStore(tmp_path/"refresh.sqlite");good=store.import_envelope(envelope(tmp_path))
    partial=store.import_envelope(envelope(tmp_path,run="estimated",as_of=datetime(2026,8,22,tzinfo=timezone.utc),patch=lambda r:r["records"][0]["evidence"].update(cutoff_basis="configured_lag")))
    def failed(raw):
        for row in raw["records"]:row["evidence"].update(status="error",metrics={})
    failure=store.import_envelope(envelope(tmp_path,run="source-failed",as_of=datetime(2026,8,23,tzinfo=timezone.utc),patch=failed))
    before=store.path.read_bytes()
    with running(store.path) as port:
        observed_result=query_refresh("refresh_observed",job_id=good["job_id"],port=port)
        assert observed_result["latest_provisional_observation"]["receipt"]["snapshot_id"]==partial["snapshot_id"]
        assert observed_result["latest_attempt"]["snapshot_id"]==failure["snapshot_id"]
        assert observed_result["strict_complete"] is None
        assert observed_result["legacy_v1_complete_head"]["snapshot_id"]==good["snapshot_id"]
        assert observed_result["serving_prior_observation_after_failed_attempt"]
        pinned=query_refresh("refresh_snapshot",job_id=good["job_id"],snapshot_id=partial["snapshot_id"],port=port)
        assert pinned["scoped_completeness"]==observed_result["latest_provisional_observation"]["scoped_completeness"]
    with running(store.path) as port:assert query_refresh("refresh_observed",job_id=good["job_id"],port=port)==observed_result
    assert store.path.read_bytes()==before
