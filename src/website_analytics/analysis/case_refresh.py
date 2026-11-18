"""Append new bounded provisional weekly evidence; never confirm intervention dates."""
from __future__ import annotations

import copy
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlsplit

from website_analytics.analysis.cases import CaseStore, make_case_record, render_case
from website_analytics.analysis.contracts import AnalysisError, Selection, fingerprint, timestamp, metric_number, page_path
from website_analytics.analysis.query import METRICS
from website_analytics.models import DateRange
from website_analytics.analysis.continuous import ContinuousDraft,period_labels
from website_analytics.analysis.worker import parse_request, query


def _fetch_times(value):
    times=[]
    if isinstance(value,dict):
        if "fetched_at" in value: times.append(timestamp(value["fetched_at"]))
        for item in value.values(): times.extend(_fetch_times(item))
    elif isinstance(value,list):
        for item in value: times.extend(_fetch_times(item))
    return times


def build(previous, request, packet, *, url_evidence=None, as_of=None):
    now=as_of or datetime.now(timezone.utc)
    render_case(previous)  # Strictly validated historical record, no mutation.
    if previous["kind"]!="continuous_trend": raise AnalysisError("continuous case required")
    pages,variants,periods=parse_request(request,as_of=now)
    if request["raw_urls"]!=previous["current_intervention"]["raw_urls"]:
        raise AnalysisError("cannot alter original URL identities")
    if list(variants)!=previous["source_packet"]["variant_paths_not_merged"]:
        raise AnalysisError("cannot silently merge or change variant identities")
    if packet.get("submitted_paths")!=list(pages) or packet.get("variant_paths_not_merged")!=list(variants):
        raise AnalysisError("packet does not match bounded worker scope")
    if [(r.get("start"),r.get("end")) for r in packet.get("weeks",[])] != [(p.start.isoformat(),p.end.isoformat()) for p in periods]:
        raise AnalysisError("packet windows exceed requested scope")
    captured=timestamp(packet["queried_at"])
    old=timestamp(previous["source_packet"]["queried_at"])
    times=_fetch_times(packet)
    old_times=_fetch_times(previous["source_packet"])
    if not old<captured<=now or not times or max(times)>captured or max(times)<=max(old_times):
        raise AnalysisError("stale or future case evidence")
    # Full existing packet validation forbids PII and fabricated inquiry scope,
    # enforces interval UV grain, exact URL case, bounded source fields and gaps.
    # No conversion of aggregate weekly values into invented daily/page rows.
    return make_case_record(previous["case_id"],"continuous_trend",copy.deepcopy(previous["current_intervention"]),
        copy.deepcopy(packet),copy.deepcopy(url_evidence if url_evidence is not None else previous["url_evidence"]),
        copy.deepcopy(previous["current_intervention"]),superseded=previous["superseded_digests"])


def append(previous,request,packet,store:CaseStore,*,run_id,run_log,as_of=None,url_evidence=None,scope_digest=None):
    scope=scope_digest or fingerprint(request);run_log.append(run_id,scope,"case_build","started")
    try:
        if store.get(previous["case_id"],fingerprint(previous))!=previous:
            raise AnalysisError("private store must retain the previous evidence")
        record=build(previous,request,packet,url_evidence=url_evidence,as_of=as_of)
        report=render_case(record)
        partial=any(value!="ok" for row in report["weekly_rows"] for value in row["source_status"].values())
        partial=partial or packet["gsc_page_days"]["status"]!="ok" or bool(report["source_quality"]["gsc_page_day_truncated"]) or any(
            row["ga4_pages"]["status"]!="ok" or
            row["inquiry"].get("result",{}).get("page_rows_truncated",False) or
            row["inquiry"].get("result",{}).get("foreign_origin_group_count",0) for row in packet["weeks"])
        eid=store.put(record)
        run_log.append(run_id,scope,"case_build","partial" if partial else "ok")
        return {"evidence_id":eid,"evidence_state":"provisional","batch_confirmed":False,
                "effect_confirmed":False,"source_requests_partial":partial,
                "source_quality":report["source_quality"],"source_queried_at":report["queried_at"],
                "completeness":"not_independently_verified",
                "limitations":report["limitations"],
                "prior_evidence_id":fingerprint(previous),"publication_performed":False}
    except Exception:
        run_log.append(run_id,scope,"case_build","failed")
        raise AnalysisError("case_refresh_failed_no_historical_overwrite") from None


def collect(previous,request,reader,*,run_id,run_log,runtime,as_of=None,observations=None,metadata_windows=None,clock=None):
    """Compose the existing strict case packet from injected aggregate adapters.

    No daily GSC rows are invented from weekly totals. Missing availability and
    response timezone metadata stay unknown. This does not save/publish a case.
    """
    now_fn=clock or (lambda:datetime.now(timezone.utc))
    now=as_of or now_fn()
    render_case(previous)
    pages,variants,periods=parse_request(request,as_of=now)
    if request["raw_urls"]!=previous["current_intervention"]["raw_urls"] or list(variants)!=previous["source_packet"]["variant_paths_not_merged"]:
        raise AnalysisError("case collector scope mismatch")
    draft=ContinuousDraft.parse(previous["current_intervention"])
    expected=period_labels(draft,periods,open_intervals=request["contract"]=="bounded-worker/v2",as_of=now)
    if len(expected)!=len(periods):raise AnalysisError("case windows must be contiguous")
    aggregate_result=query(request,reader,run_id=run_id,run_log=run_log,as_of=as_of,metadata_windows=metadata_windows,clock=clock)
    blocked=set(aggregate_result.get("blocked_sources",[]))
    if aggregate_result["sample"] is not False:raise AnalysisError("case collector requires injected aggregate adapters")
    def stamp():return (as_of or now_fn()).isoformat()
    indexed={(r["scope"]["start"],r["group"],r["evidence"]["source"]):r["evidence"] for r in aggregate_result["records"]}
    def error(code):return {"status":"error","fetched_at":stamp(),"error_category":code}
    def envelope(source,evidence):
        if evidence["status"] in {"error","unavailable"}:return error("source_request_failed")
        rows=[evidence["metrics"]] if evidence["metrics"] else []
        value=rows if source=="ga4" else {"rows":rows,"truncated":evidence["truncated"],"row_cap":50000}
        return {"status":"ok","fetched_at":evidence["fetched_at"],"result":value}
    weeks=[]
    for labels,period in zip(expected,periods,strict=True):
        key=period.start.isoformat()
        ga=envelope("ga4",indexed[(key,"submitted","ga4")]);gs=envelope("gsc",indexed[(key,"submitted","gsc")])
        ga_rows=[];ga_pages_error=False
        for path in (*pages,*variants):
            if "ga4" in blocked:ga_pages_error=True;continue
            evidence=reader.read("ga4",period,(path,),Selection())
            if evidence.status in {"error","unavailable"}:ga_pages_error=True
            elif evidence.metrics:ga_rows.append({"landingPage":path,**evidence.metrics})
        ga_pages=error("page_aggregate_unavailable") if ga_pages_error else {"status":"ok","fetched_at":stamp(),"result":ga_rows}
        try:
            if "inquiry" in blocked:raise AnalysisError("source access denied")
            iq=reader.adapters["inquiry"].query(period)
            counts={};attributed=0.0
            for row in iq.pages:
                raw=urlsplit(row["sourceUrl"])
                if raw.netloc and (raw.hostname not in reader.site.domains or raw.username or raw.password):
                    raise AnalysisError("foreign inquiry origin")
                try:page_path(raw.path)
                except AnalysisError:continue
                path=raw.path.rstrip("/") or "/"
                values={k:metric_number(row[k]) for k in METRICS["inquiry"]}
                attributed+=values["storedSubmissions"]
                if path in (*pages,*variants):
                    total=counts.setdefault(path,{k:0.0 for k in METRICS["inquiry"]})
                    for k,v in values.items():total[k]+=v
            database_total=metric_number(iq.totals["storedSubmissions"])
            if attributed>database_total:raise AnalysisError("inconsistent inquiry totals")
            rows=[{"exact_path":path,**values} for path,values in sorted(counts.items())]
            cohorts={name:{k:sum(row[k] for row in rows if row["exact_path"] in cohort) for k in METRICS["inquiry"]}
                     for name,cohort in [("submitted",pages),("lowercase_variant",variants)]}
            inquiry={"status":"ok","fetched_at":stamp(),"result":{"rows":rows,"cohorts":cohorts,
                "page_rows_truncated":iq.page_rows_truncated,"database_total":database_total,
                "database_unattributed_count":None if iq.page_rows_truncated else database_total-attributed,
                "foreign_origin_group_count":0,"database_scope":"configured_database_not_independently_proven_dedicated",
                "date_boundary":"legacy_server_calendar_unverified","days_with_database_rows":[row["date"] for row in iq.daily]}}
        except Exception:inquiry=error("inquiry_scope_or_source_unavailable")
        weeks.append({**labels,"ga4_submitted":ga,"ga4_pages":ga_pages,"gsc_submitted":gs,"inquiry":inquiry})
    try:
        if "gsc" in blocked:raise AnalysisError("source access denied")
        result=reader.adapters["gsc"].query_exact_result(reader.site,DateRange(periods[0].start,periods[-1].end),("date","page"),page_paths=(*pages,*variants),**({"data_state":"all"} if request["contract"]=="bounded-worker/v2" else {}))
        gsc_days={"status":"ok","fetched_at":stamp(),"result":{"rows":list(result.rows),"truncated":result.truncated,"row_cap":result.row_cap}}
    except Exception:gsc_days=error("gsc_daily_grain_unavailable")
    availability=error("availability_not_observed")
    now=as_of or now_fn()
    packet={"protocol":"seo-weekly-read/1","sample":False,"exploratory":True,"batch_confirmed":False,
        "queried_at":now.isoformat(),"runtime":copy.deepcopy(runtime),"submitted_paths":list(pages),
        "variant_paths_not_merged":list(variants),"weeks":weeks,"gsc_availability":availability,
        "skipped_weeks":[],"gsc_page_days":gsc_days,"query_metadata":{"ga4":[],"gsc":[]},"writes":0,"cache_or_audit_persisted":False,
        "worker_window_evidence":aggregate_result["records"],"worker_captured_at":aggregate_result["captured_at"]}
    if request["contract"]=="bounded-worker/v2":
        packet["protocol"]="seo-period-read/2";packet["selection_timezone"]=request["selection_timezone"]
    from website_analytics.analysis.completeness import registration_ids,validate_observations
    packet["worker_source_ids"]=registration_ids(reader.site)
    packet["worker_metadata_diagnostics"]=copy.deepcopy(aggregate_result.get("metadata_diagnostics",[]))
    combined=copy.deepcopy(aggregate_result.get("observations",[]))
    if observations is not None:combined.extend(copy.deepcopy(observations))
    if combined:
        validate_observations(combined,as_of=now)
        packet["worker_observations"]=combined
    # Validate all composed grain/schema before handing it to persistence.
    build(previous,request,packet,as_of=now)
    return packet


def run(previous,request,reader,store,*,run_id,run_log,runtime,as_of=None,observations=None,metadata_windows=None):
    scope=fingerprint(request) if metadata_windows is None else fingerprint({"request":request,"metadata_windows":{k:{"start":v.start.isoformat(),"end":v.end.isoformat()} for k,v in metadata_windows.items()}})
    try:
        store.get(previous["case_id"],fingerprint(previous))
        packet=collect(previous,request,reader,run_id=run_id,run_log=run_log,runtime=runtime,as_of=as_of,observations=observations,metadata_windows=metadata_windows)
    except Exception:
        run_log.append(run_id,scope,"case_build","failed")
        raise AnalysisError("case_collection_failed_no_evidence_written") from None
    return append(previous,request,packet,store,run_id=run_id,run_log=run_log,as_of=as_of,scope_digest=scope)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("previous-store","case-id","previous-evidence-id","request","run-id","run-log","output-store"):
        parser.add_argument("--"+name,required=True)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--saved-packet");source.add_argument("--registered-config")
    parser.add_argument("--runtime-manifest",help="required public provenance in registered-source mode")
    parser.add_argument("--metadata-windows",help="explicit operator-owned GSC metadata windows; registered-source mode only")
    parser.add_argument("--as-of")
    args=parser.parse_args(argv)
    from website_analytics.analysis.readers import read_json
    from website_analytics.analysis.worker import RunLog,registered_reader
    request=None;log=RunLog(args.run_log)
    try:
        request=read_json(args.request);now=timestamp(args.as_of) if args.as_of else datetime.now(timezone.utc)
        parse_request(request,as_of=now)
        previous=CaseStore(args.previous_store).get(args.case_id,args.previous_evidence_id)
        CaseStore(args.output_store).get(args.case_id,args.previous_evidence_id)
        if args.saved_packet:
            if args.metadata_windows:raise AnalysisError("saved replay cannot request metadata fetching")
            result=append(previous,request,read_json(args.saved_packet),CaseStore(args.output_store),
                run_id=args.run_id,run_log=log,as_of=now)
        else:
            if not args.runtime_manifest:raise AnalysisError("reviewed runtime manifest required")
            from website_analytics.analysis.worker import parse_metadata_windows,validate_metadata_windows
            windows=parse_metadata_windows(read_json(args.metadata_windows)) if args.metadata_windows else None
            _,_,periods=parse_request(request,as_of=now);validate_metadata_windows(periods,windows,now)
            from website_analytics.analysis.case_validation import runtime as validate_runtime
            manifest=read_json(args.runtime_manifest);validate_runtime(manifest)
            # Scope and unchanged intervention are checked before auth factories.
            if request["raw_urls"]!=previous["current_intervention"]["raw_urls"] or request["variant_paths"]!=previous["source_packet"]["variant_paths_not_merged"]:
                raise AnalysisError("case request differs from reviewed identities")
            reader=registered_reader(args.registered_config)
            result=run(previous,request,reader,CaseStore(args.output_store),run_id=args.run_id,run_log=log,runtime=manifest,
                       as_of=timestamp(args.as_of) if args.as_of else None,metadata_windows=windows)
        print(json.dumps(result,ensure_ascii=False,allow_nan=False))
        return 3 if result["source_requests_partial"] else 0
    except Exception:
        try:log.append(args.run_id,fingerprint(request if request is not None else {"invalid_input":True}),"preflight","failed")
        except Exception:pass
        print(json.dumps({"status":"error","error":"invalid_case_worker_input_or_state"}));return 2


if __name__=="__main__":raise SystemExit(main())
