"""Actual adapter interfaces, injected clients only; no real-source requests."""
import copy
import json
from dataclasses import replace
from datetime import date,datetime,timedelta
from types import SimpleNamespace

import pytest

from test_analysis_readers import GAClient,site
from test_analysis_worker import reader,request,fresh_case,NOW
from test_analysis_case_service import running
from website_analytics.models import DateRange
from website_analytics.adapters.ga4 import GA4Adapter
from website_analytics.adapters.gsc import GSCAdapter,GSCMetadataResult
from website_analytics.analysis.adapter_metadata import capture_observation
from website_analytics.analysis.worker import query,RunLog,parse_metadata_windows
from website_analytics.analysis.case_refresh import collect,build
from website_analytics.analysis.cases import CaseStore,query_case,render_case,page_choices
from website_analytics.analysis.client import query_case as client_query
from website_analytics.analysis.contracts import AnalysisError,Selection


class MetadataGA(GAClient):
    def __init__(self,supported=False,truncated=False,absent=False):
        super().__init__();self.supported=supported;self.truncated=truncated;self.absent=absent
    def run_report(self,request):
        value=super().run_report(request)
        if self.absent:value.metadata=None;return value
        value.metadata=SimpleNamespace(time_zone="America/Los_Angeles",sampling_metadatas=[],subject_to_thresholding=False,data_loss_from_other_row=False,
            schema_restriction_response=SimpleNamespace(active_metric_restrictions=[]))
        if self.supported:value.metadata.data_truncation_reasons=[SimpleNamespace(data_truncation_message="do not retain private text")] if self.truncated else []
        return value


class MetadataGSC:
    def __init__(self,mode="camel"):
        self.mode=mode;self.requests=[]
    def searchanalytics(self):return self
    def query(self,**kwargs):
        self.requests.append(copy.deepcopy(kwargs));body=kwargs["body"]
        def execute():
            if body["dataState"]=="all":
                start=date.fromisoformat(body["startDate"]);end=date.fromisoformat(body["endDate"])
                days=[(start+timedelta(days=i)).isoformat() for i in range((end-start).days+1)]
                result={"rows":[{"keys":[d]} for d in days],"responseAggregationType":"byPage"}
                if self.mode!="absent":result["metadata"]={"firstIncompleteDate" if self.mode!="snake" else "first_incomplete_date":end.isoformat()}
                if self.mode=="conflict":result["metadata"]["first_incomplete_date"]=start.isoformat()
                if self.mode=="bad_date":result["metadata"]["firstIncompleteDate"]="2020-01-01"
                if self.mode=="bad_rows":result["rows"]=[{"keys":[]}]
                return result
            dimensions=body["dimensions"]
            if dimensions==["date","page"]:return {"rows":[]}
            return {"rows":[{"keys":[],"clicks":3,"impressions":30,"ctr":.1,"position":10}]}
        return SimpleNamespace(execute=execute)


def integrated(supported=False,mode="camel",at=NOW):
    r=reader();r.clock=lambda:at
    r.adapters["ga4"]=GA4Adapter(MetadataGA(supported=supported))
    r.adapters["gsc"]=GSCAdapter(MetadataGSC(mode))
    return r


@pytest.mark.parametrize("supported,truncated,absent",[(False,False,False),(True,False,False),(True,True,False),(False,False,True)])
def test_ga4_actual_interface_capability_unknown_and_flags_are_separate(supported,truncated,absent):
    r=integrated();client=MetadataGA(supported,truncated,absent);r.adapters["ga4"]=GA4Adapter(client)
    period=DateRange(date(2026,10,19),date(2026,10,25));pages=("/i/Mixed-Example",)
    evidence=r.read("ga4",period,pages,Selection());obs,diag=capture_observation(r,"ga4",period,pages,Selection(),evidence)
    assert diag is None and obs["maturity"]["basis"]=="unknown"
    assert obs["quality"]["truncation_reasons_supported"]==(None if absent else supported)
    assert obs["quality"]["truncated"]==(None if absent or not supported else truncated)
    assert "private text" not in json.dumps(obs) and len(client.requests)==1
    if absent:assert obs["calendar"]["name"] is None


@pytest.mark.parametrize("present",[True,False])
def test_installed_sdk_proto_capability_is_detected_without_a_source_call(present):
    from google.analytics.data_v1beta.types import RunReportResponse,ResponseMetaData,Row,MetricValue
    response=RunReportResponse(row_count=1,rows=[Row(metric_values=[MetricValue(value=str(n)) for n in (100,70,65,50,.5,200,4)])])
    if present:response.metadata=ResponseMetaData(time_zone="America/Los_Angeles")
    r=integrated();r.adapters["ga4"]=GA4Adapter(SimpleNamespace(run_report=lambda request:response))
    period=DateRange(date(2026,10,19),date(2026,10,25));paths=("/i/Mixed-Example",)
    evidence=r.read("ga4",period,paths,Selection());obs,diag=capture_observation(r,"ga4",period,paths,Selection(),evidence)
    assert diag is None
    supported="data_truncation_reasons" in ResponseMetaData.meta.fields
    assert obs["quality"]["truncation_reasons_supported"] is supported
    assert obs["quality"]["truncated"]==(False if present and supported else None)
    assert obs["calendar"]["name"]==("America/Los_Angeles" if present else None)


@pytest.mark.parametrize("mode",["camel","snake","absent"])
def test_gsc_all_state_exact_date_scope_aliases_and_absence(mode):
    service=MetadataGSC(mode);adapter=GSCAdapter(service);period=DateRange(date(2026,10,19),date(2026,10,26));pages=("/i/Mixed-Example","/i/mixed-example")
    result=adapter.availability_exact_result(site(),period,page_paths=pages)
    assert result.first_incomplete_date==(None if mode=="absent" else "2026-10-26")
    body=service.requests[0]["body"]
    assert body["dataState"]=="all" and body["dimensions"]==["date"] and body["rowLimit"]==100
    assert result.request_scope["pages"]==list(pages) and result.request_scope["property"]==site().gsc_property_url
    assert not result.truncated and len(result.dates_observed)==8


@pytest.mark.parametrize("mode",["conflict","bad_date","bad_rows"])
def test_gsc_invalid_metadata_never_silently_attests_finality(mode):
    with pytest.raises(ValueError):GSCAdapter(MetadataGSC(mode)).availability_exact_result(site(),DateRange(date(2026,10,19),date(2026,10,26)),page_paths=("/i/Mixed-Example",))


def test_bridge_rejects_stale_or_mismatched_actual_request_provenance():
    r=integrated();period=DateRange(date(2026,10,19),date(2026,10,25));paths=("/i/Mixed-Example",)
    evidence=r.read("ga4",period,paths,Selection());r.adapters["ga4"].last_exact_scope["pages"]=["/i/mixed-example"]
    obs,diag=capture_observation(r,"ga4",period,paths,Selection(),evidence)
    assert obs is None and "metadata_request_scope_mismatch" in diag["notes"]


def test_wrong_aggregate_scope_and_interposed_requests_cannot_attest_metadata():
    r=integrated();period=DateRange(date(2026,10,19),date(2026,10,25));paths=("/i/Mixed-Example",)
    evidence=r.read("gsc",period,paths,Selection());before=len(r.adapters["gsc"]._service.requests)
    obs,diag=capture_observation(r,"gsc",period,paths,Selection(),replace(evidence,scope_digest="0"*64))
    assert obs is None and "metadata_request_scope_mismatch" in diag["notes"]
    assert len(r.adapters["gsc"]._service.requests)==before
    evidence=r.read("ga4",period,paths,Selection())
    r.adapters["ga4"].aggregate(r.site,period)
    obs,diag=capture_observation(r,"ga4",period,paths,Selection(),evidence)
    assert obs is None and "metadata_request_scope_mismatch" in diag["notes"]
    evidence=r.read("gsc",period,paths,Selection())
    r.adapters["gsc"].availability_exact_result=lambda *a,**k:GSCMetadataResult({"property":"wrong"},None,(),False,None)
    obs,diag=capture_observation(r,"gsc",period,paths,Selection(),evidence)
    assert obs is None and "metadata_request_scope_mismatch" in diag["notes"]


def test_worker_captures_metadata_without_extra_ga4_or_inquiry_queries(tmp_path):
    r=integrated();calls=[];original=r.adapters["inquiry"].query
    def count(period):calls.append(period);return original(period)
    r.adapters["inquiry"].query=count
    raw=request();last=raw["weeks"][-1];window=DateRange(date.fromisoformat(last["start"]),date.fromisoformat(last["end"])+timedelta(days=1))
    result=query(raw,r,run_id="metadata",run_log=RunLog(tmp_path/"runs.sqlite"),as_of=NOW,metadata_windows={last["start"]:window})
    assert len(r.adapters["ga4"]._client.requests)==4 and len(calls)==4
    assert len(r.adapters["gsc"]._service.requests)==8
    assert len(result["observations"])==12 and result["metadata_diagnostics"]==[]
    ga=[o for o in result["observations"] if o["source"]=="ga4"]
    iq=[o for o in result["observations"] if o["source"]=="inquiry"]
    assert all(o["quality"]["truncation_reasons_supported"] is False and o["quality"]["truncated"] is None for o in ga)
    assert all(o["calendar"]=={"name":None,"basis":"unknown"} and o["dates_observed"]==[] for o in iq)
    gs=[row for row in result["scoped_completeness"]["source_windows"] if row["source"]=="gsc" and row["scope"]["start"]==last["start"]]
    assert all(row["maturity"]["state"]=="provider_final_for_window" and row["maturity"]["observed_complete_through"]==last["end"] for row in gs)
    assert result["status"]=="partial" and not result["scoped_completeness"]["strict_complete"]


def test_identical_explicit_metadata_windows_are_fetched_once_per_page_cohort(tmp_path):
    r=integrated();raw=request();window=DateRange(date.fromisoformat(raw["weeks"][0]["start"]),date.fromisoformat(raw["weeks"][-1]["end"]))
    result=query(raw,r,run_id="shared-metadata",run_log=RunLog(tmp_path/"runs.sqlite"),as_of=NOW,
        metadata_windows={row["start"]:window for row in raw["weeks"]})
    assert len(r.adapters["gsc"]._service.requests)==6
    assert len([obs for obs in result["observations"] if obs["source"]=="gsc"])==2
    assert result["metadata_diagnostics"]==[]


@pytest.mark.parametrize("bad",[[],{"other":DateRange(date(2026,10,1),date(2026,10,2))},{"2026-10-12":DateRange(date(2026,10,12),date(2026,11,20))}])
def test_bad_metadata_window_fails_before_source_requests_and_is_logged(tmp_path,bad):
    r=integrated()
    with pytest.raises(AnalysisError):query(request(),r,run_id="bad",run_log=RunLog(tmp_path/"runs.sqlite"),as_of=NOW,metadata_windows=bad)
    assert r.adapters["ga4"]._client.requests==[] and r.adapters["gsc"]._service.requests==[]


def test_explicit_operator_window_document_is_bounded_and_no_range_is_guessed():
    raw={"2026-10-19":{"start":"2026-10-19","end":"2026-10-26"}}
    parsed=parse_metadata_windows(raw)
    assert parsed=={"2026-10-19":DateRange(date(2026,10,19),date(2026,10,26))}
    with pytest.raises(AnalysisError):parse_metadata_windows({"2026-10-19":{**raw["2026-10-19"],"command":"forbidden"}})


def test_unsupported_or_failed_metadata_is_diagnostic_not_a_fallback(tmp_path):
    r=integrated();r.adapters["gsc"].exact_metadata_contract=None
    result=query(request(),r,run_id="old-metadata",run_log=RunLog(tmp_path/"runs.sqlite"),as_of=NOW)
    assert len(r.adapters["gsc"]._service.requests)==4
    assert all("metadata_adapter_unsupported" in d["notes"] for d in result["metadata_diagnostics"])
    assert all(row["evidence"]["metrics"] for row in result["records"])


def test_metadata_access_denial_is_sanitized_and_stops_further_source_requests(tmp_path):
    from test_analysis_diagnostics import HTTPFailure
    r=integrated();service=r.adapters["gsc"]._service;original=service.query
    def deny_metadata(**kwargs):
        req=original(**kwargs)
        if kwargs["body"]["dataState"]=="all":
            def fail():raise HTTPFailure(403)
            return SimpleNamespace(execute=fail)
        return req
    service.query=deny_metadata
    old,raw,_=fresh_case();packet=collect(old,raw,r,run_id="denied",run_log=RunLog(tmp_path/"runs.sqlite"),runtime=old["source_packet"]["runtime"],as_of=NOW)
    assert len(service.requests)==2  # One aggregate and its metadata; no followup daily request.
    assert "http_status_403" in packet["worker_metadata_diagnostics"][0]["notes"]
    assert "secret" not in json.dumps(packet) and packet["gsc_page_days"]["status"]=="error"


def test_metadata_revisions_persist_and_all_query_projections_match(tmp_path):
    old,req,_=fresh_case();r=integrated();log=RunLog(tmp_path/"runs.sqlite");store=CaseStore(tmp_path/"cases.sqlite")
    old_id=store.put(old);first=collect(old,req,r,run_id="old-sdk",run_log=log,runtime=old["source_packet"]["runtime"],as_of=NOW);one=build(old,req,first,as_of=NOW);one_id=store.put(one)
    later=NOW+timedelta(days=1);r=integrated(supported=True,at=later)
    second=collect(old,req,r,run_id="new-sdk-fixture",run_log=log,runtime=old["source_packet"]["runtime"],as_of=later);two=build(old,req,second,as_of=later);two_id=store.put(two)
    before=store.path.read_bytes();common={"case_id":old["case_id"],"evidence_id":two_id}
    with running(store) as port:
        views=[]
        for operation in ["case_detail","case_trend","page_contribution"]:
            args={**common}
            if operation=="page_contribution":args["page_key"]=page_choices(two)[0]["page_key"]
            views.append(client_query(operation,**args,port=port)["scoped_completeness"])
        assert views[0]==views[1]==views[2] and not views[0]["strict_complete"]
        discovered=client_query("case_observations",case_id=old["case_id"],port=port)
        assert any(g["latest_provisional_observation"] and g["latest_provisional_observation"]["evidence_id"]==two_id for g in discovered["groups"])
    assert store.get(old["case_id"],old_id)==old and render_case(store.get(old["case_id"],one_id))["report_digest"]==one["expected_report_digest"]
    assert store.path.read_bytes()==before
