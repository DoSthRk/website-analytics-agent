"""Bounded injected-source worker. No credentials, CLI fallback or source factories."""
from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import sqlite3
from dataclasses import asdict, fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from website_analytics.analysis.contracts import AnalysisError, Selection, calendar_date, canonical_json, fingerprint, page_path, scope_document, strict_keys, timestamp, timezone_name
from website_analytics.analysis.query import METRICS, SourceEvidence
from website_analytics.analysis.readers import AdapterReader, read_json
from website_analytics.models import DateRange


def window_metadata(source, raw):
    """Bridge explicit source observations; HTTP success/end-date is not finality."""
    if source not in METRICS: raise AnalysisError("unsupported source")
    strict_keys(raw, set(), {"timezone", "coverage_start", "coverage_end", "observed_complete_through",
        "completion_evidence", "estimated_complete_through", "missing_dates", "truncated", "sampled", "thresholded",
        "first_incomplete_date", "availability_scope_verified"})
    for key in ("coverage_start", "coverage_end", "observed_complete_through", "estimated_complete_through"):
        if raw.get(key) is not None: calendar_date(raw[key])
    boundary = timezone_name(raw["timezone"]) if raw.get("timezone") is not None else "unverified"
    observed = raw.get("observed_complete_through")
    proof = raw.get("completion_evidence")
    if "availability_scope_verified" in raw and type(raw["availability_scope_verified"]) is not bool:
        raise AnalysisError("invalid availability scope proof")
    if raw.get("first_incomplete_date") is not None:
        incomplete=calendar_date(raw["first_incomplete_date"])
        if source!="gsc": raise AnalysisError("GSC availability belongs to GSC only")
        if raw.get("availability_scope_verified") is True:
            derived=(incomplete-timedelta(days=1)).isoformat()
            if observed is not None and observed!=derived: raise AnalysisError("conflicting observed cutoffs")
            observed,proof=derived,"independent_observation"
    if proof not in {None, "independent_observation"}: raise AnalysisError("unsupported completeness proof")
    if observed is not None and proof != "independent_observation":
        raise AnalysisError("observed cutoff needs explicit independent evidence")
    for key in ("truncated", "sampled", "thresholded"):
        if key in raw and type(raw[key]) is not bool: raise AnalysisError("invalid source flag")
    missing = raw.get("missing_dates", [])
    if not isinstance(missing, list) or len(missing)>14: raise AnalysisError("invalid source missing dates")
    for date in missing: calendar_date(date)
    flags = tuple(key for key in ("sampled", "thresholded") if raw.get(key))
    quality_known=all(key in raw for key in (("truncated","sampled","thresholded") if source=="ga4" else ("truncated",)))
    return {"date_boundary": boundary, "coverage_start": raw.get("coverage_start"),
        "coverage_end": raw.get("coverage_end"), "complete_through": observed or raw.get("estimated_complete_through"),
        "cutoff_basis": "observed" if observed else ("configured_lag" if raw.get("estimated_complete_through") else "unknown"),
        "missing_dates": missing, "truncated": raw.get("truncated", False) or bool(flags),
        "notes": tuple("source_"+flag for flag in flags)+( () if quality_known else ("source_quality_unknown",)),
        "status": "complete" if quality_known else "partial"}


def check_adapters(reader):
    if not isinstance(reader, AdapterReader) or reader.exact_paths is not True:
        raise AnalysisError("exact adapter reader required")
    ga, gs, iq = (reader.adapters[k] for k in ("ga4", "gsc", "inquiry"))
    try:
        assert ga.exact_scope_contract == gs.exact_scope_contract == "genemedi-exact/v1"
        inspect.signature(ga.scoped_exact_aggregate).bind(reader.site, None, (), countries=(), channels=())
        inspect.signature(gs.query_exact_result).bind(reader.site, None, (), page_paths=())
        inspect.signature(iq.query).bind(None)
    except (AttributeError, TypeError, ValueError, AssertionError):
        raise AnalysisError("unsupported_adapter_contract") from None


def registered_reader(config):
    """Explicit worker-only wiring; never called by imports, fixtures or HTTP.

    Uses existing registered configuration/auth factories. In particular, missing
    verified inquiry scope fails before Google client initialization; it is never
    filled with a guessed dedicated_database declaration.
    """
    from website_analytics.config import load_sites,require_site
    from website_analytics.adapters.inquiry import create_inquiry_adapter
    from website_analytics.cli import _create_live_adapters
    site=require_site(load_sites(config),"genemedi-net")
    AdapterReader(site,exact_paths=True)  # Validate product/hostname boundary before factories.
    if site.inquiry_source is None:raise AnalysisError("registered inquiry source required")
    inquiry=create_inquiry_adapter(site.inquiry_source,domains=site.domains)
    ga4,gsc=_create_live_adapters()
    reader=AdapterReader(site,ga4=ga4,gsc=gsc,inquiry=inquiry,exact_paths=True)
    check_adapters(reader)
    return reader


def parse_request(raw, *, as_of):
    v2=isinstance(raw,dict) and raw.get("contract")=="bounded-worker/v2"
    strict_keys(raw, {"contract", "site", "raw_urls", "variant_paths", "weeks"}|({"selection_timezone"} if v2 else set()))
    if raw["contract"] not in {"bounded-worker/v1","bounded-worker/v2"} or raw["site"] != "genemedi-net":
        raise AnalysisError("unsupported worker scope")
    urls = raw["raw_urls"]
    if not isinstance(urls, list) or not 1<=len(urls)<=20: raise AnalysisError("bounded URL list required")
    pages=[]
    for url in urls:
        page_path(url)  # Safety only; no canonical lowercasing of identity.
        parsed=urlsplit(url)
        if parsed.scheme!="https": raise AnalysisError("registered full HTTPS URL required")
        pages.append(parsed.path.rstrip("/") or "/")
    variants=raw["variant_paths"]
    if not isinstance(variants,list) or len(variants)>5: raise AnalysisError("invalid separate variants")
    for path in variants:
        page_path(path)
        if not path.startswith("/") or path.startswith("//") or path.rstrip("/")!=path:
            raise AnalysisError("exact variant path required")
    if len(set(pages+variants))!=len(pages)+len(variants): raise AnalysisError("duplicate exact path")
    weeks=raw["weeks"]
    if not isinstance(weeks,list) or not 1<=len(weeks)<=2: raise AnalysisError("one or two completed weeks required")
    if v2 and (as_of.tzinfo is None or as_of.utcoffset() is None):raise AnalysisError("aware selection time required")
    if v2 and raw["selection_timezone"]!="America/Los_Angeles":raise AnalysisError("unsupported selection calendar")
    today=as_of.astimezone(ZoneInfo(raw["selection_timezone"])).date() if v2 else as_of.date()
    periods=[]
    for week in weeks:
        strict_keys(week,{"start","end"}|({"window_kind"} if v2 else set()));a,b=calendar_date(week["start"]),calendar_date(week["end"])
        if v2:
            if a.weekday()!=0 or not 0<=(b-a).days<=6 or b>today:raise AnalysisError("bounded Monday-start interval required")
            expected="completed_week" if b.weekday()==6 and b<today else "open_week"
            if week["window_kind"]!=expected or expected=="open_week" and a!=today-timedelta(days=today.weekday()):raise AnalysisError("open interval must name current selection week")
        elif a.weekday()!=0 or b-a!=timedelta(days=6) or b>=as_of.date():
            raise AnalysisError("completed calendar week required; not proof of source finality")
        if any(a<=p.end and p.start<=b for p in periods): raise AnalysisError("overlapping weeks")
        periods.append(DateRange(a,b))
    if v2 and (periods!=sorted(periods,key=lambda p:p.start) or (periods[-1].end-periods[0].start).days>13 or any(a.end+timedelta(days=1)!=b.start for a,b in zip(periods,periods[1:]))):raise AnalysisError("contiguous bounded periods required")
    return tuple(pages),tuple(variants),tuple(periods)


class RunLog:
    """Private append-only stage events; no exception text, paths or source payloads."""
    def __init__(self,path): self.path=Path(path)
    def append(self,run_id,scope_digest,stage,outcome):
        if not isinstance(run_id,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}",run_id): raise AnalysisError("invalid run id")
        if not re.fullmatch(r"[a-f0-9]{64}",scope_digest): raise AnalysisError("invalid scope digest")
        if stage not in {"preflight","capture","case_build","export","publish"} or outcome not in {"started","ok","partial","failed"}:
            raise AnalysisError("invalid run event")
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.path.is_symlink():raise AnalysisError("run log must not be a symlink")
        if not self.path.exists():
            try:fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
            except FileExistsError:pass
        if self.path.stat().st_uid!=os.geteuid() or self.path.stat().st_mode&0o777!=0o600:
            raise AnalysisError("private run log permissions required")
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY, run_id TEXT, scope_digest TEXT, stage TEXT, outcome TEXT, recorded_at TEXT)")
            prior=db.execute("SELECT scope_digest FROM events WHERE run_id=? LIMIT 1",(run_id,)).fetchone()
            if prior and prior[0]!=scope_digest: raise AnalysisError("run id scope conflict")
            if db.execute("SELECT count(*) FROM events").fetchone()[0]>=10000:raise AnalysisError("run log budget exceeded")
            db.execute("INSERT INTO events(run_id,scope_digest,stage,outcome,recorded_at) VALUES (?,?,?,?,?)",
                       (run_id,scope_digest,stage,outcome,datetime.now(timezone.utc).isoformat()))


class ExactFixtureReader:
    """Strict synthetic aggregate fixture lookup, retaining exact path case."""
    sample=True
    def __init__(self,path):
        raw=read_json(path);strict_keys(raw,{"contract","sample","records"})
        if raw["contract"]!="worker-evidence/v1" or raw["sample"] is not True or not isinstance(raw["records"],list) or len(raw["records"])>36:
            raise AnalysisError("invalid synthetic worker fixture")
        self.records={}
        for row in raw["records"]:
            strict_keys(row,{"scope","evidence"});scope=row["scope"]
            strict_keys(scope,{"site","start","end","pages","countries","channels"})
            # Exact request validation is repeated against the requested cohort below.
            if scope["site"]!="genemedi-net": raise AnalysisError("foreign fixture scope")
            evidence=strict_keys(row["evidence"],{f.name for f in fields(SourceEvidence)})
            key=(evidence["source"],fingerprint(scope))
            if key in self.records or evidence["scope_digest"]!=key[1]: raise AnalysisError("duplicate or mismatched fixture")
            self.records[key]=SourceEvidence(**evidence)
    def read(self,source,period,pages,selection):
        key=(source,fingerprint(scope_document(period,pages,selection)))
        if key not in self.records: raise AnalysisError("no exact fixture cohort")
        return self.records[key]


def parse_metadata_windows(raw):
    if not isinstance(raw,dict) or len(raw)>2:raise AnalysisError("invalid metadata window map")
    result={}
    for key,value in raw.items():
        calendar_date(key);strict_keys(value,{"start","end"})
        result[key]=DateRange(calendar_date(value["start"]),calendar_date(value["end"]))
    return result


def validate_metadata_windows(periods,windows,as_of):
    if windows is None:return {}
    if not isinstance(windows,dict) or not set(windows)<={p.start.isoformat() for p in periods}:
        raise AnalysisError("metadata windows must name existing worker weeks")
    for period in periods:
        window=windows.get(period.start.isoformat(),period)
        if not isinstance(window,DateRange) or type(window.start) is not date or type(window.end) is not date or not 0<=(window.end-window.start).days<=13 or not window.start<=period.start<=period.end<=window.end or window.end>as_of.astimezone(ZoneInfo("America/Los_Angeles")).date():
            raise AnalysisError("invalid explicit GSC metadata window")
    return windows


def query(raw,reader,*,run_id,run_log,as_of=None,metadata_windows=None,clock=None):
    now_fn=clock or (lambda:datetime.now(timezone.utc))
    now=as_of or now_fn();scope=fingerprint(raw);stage="preflight"
    if metadata_windows is not None:
        valid_shape=isinstance(metadata_windows,dict) and all(isinstance(k,str) and isinstance(v,DateRange) and type(v.start) is date and type(v.end) is date for k,v in metadata_windows.items())
        scope=fingerprint({"request":raw,"metadata_windows":{k:{"start":v.start.isoformat(),"end":v.end.isoformat()} for k,v in metadata_windows.items()}}) if valid_shape else fingerprint({"request":raw,"invalid_metadata_windows":True})
    run_log.append(run_id,scope,stage,"started")
    try:
        if metadata_windows is not None and not valid_shape:raise AnalysisError("invalid explicit metadata windows")
        pages,variants,periods=parse_request(raw,as_of=now)
        if not isinstance(reader,ExactFixtureReader):
            check_adapters(reader)
            reader.gsc_data_state="final"
            if raw["contract"]=="bounded-worker/v2":
                inspect.signature(reader.adapters["gsc"].query_exact_result).bind(reader.site,None,(),page_paths=(),data_state="all")
                reader.gsc_data_state="all"
        windows=validate_metadata_windows(periods,metadata_windows,now)
        stage="capture";run_log.append(run_id,scope,stage,"started")
        groups={"submitted":pages,**{"variant_"+str(i):(path,) for i,path in enumerate(variants)}}
        records=[];observations=[];metadata_diagnostics=[];metadata_cache={};blocked_sources=set()
        def access_denied(notes):return any(n in {"error_category_access_denied","error_category_authorization_failed","http_status_401","http_status_403"} for n in notes)
        for period in periods:
            for group,paths in groups.items():
                expected=fingerprint(scope_document(period,paths,Selection()))
                for source in METRICS:
                    if source in blocked_sources:
                        evidence=SourceEvidence(source,expected,"unavailable",{},(reader.clock() if isinstance(reader,AdapterReader) else now).isoformat(),"unverified",notes=("source_skipped_after_access_denial",))
                    else:evidence=reader.read(source,period,paths,Selection())
                    if evidence.source!=source: raise AnalysisError("source identity mismatch")
                    records.append({"group":group,"scope":scope_document(period,paths,Selection()),
                                    "evidence":asdict(evidence)})
                    if access_denied(evidence.notes):blocked_sources.add(source)
                    if isinstance(reader,AdapterReader) and source not in blocked_sources:
                        from website_analytics.analysis.adapter_metadata import capture_observation
                        window=windows.get(period.start.isoformat(),period)
                        key=fingerprint({"source":source,"scope":scope_document(window if source=="gsc" else period,paths,Selection())})
                        if key not in metadata_cache:
                            observation,diagnostic=capture_observation(reader,source,period,paths,Selection(),evidence,metadata_period=window)
                            metadata_cache[key]=(observation,diagnostic)
                            if observation is not None:observations.append(observation)
                            if diagnostic is not None:
                                metadata_diagnostics.append(diagnostic)
                                if access_denied(diagnostic["notes"]):blocked_sources.add(source)
        captured=as_of or now_fn()
        for record in records:
            record_scope=record["scope"];period=DateRange(calendar_date(record_scope["start"]),calendar_date(record_scope["end"]))
            record["assessment"]=SourceEvidence(**record["evidence"]).assess(period,fingerprint(record_scope),sample=reader.sample,as_of=captured)
        complete=all(r["assessment"]["comparison_eligible"] and set(METRICS[r["evidence"]["source"]])<=r["assessment"]["metrics"].keys() for r in records)
        result={"contract":"worker-aggregate/v1","sample":reader.sample,"request":raw,
                "captured_at":captured.isoformat(),"status":"ok" if complete else "partial","records":records,
                "limitations":["calendar_complete_not_source_finality","interval_users_not_page_or_week_sums",
                               "independent_variants_not_merged","observational_not_causal"]}
        if isinstance(reader,AdapterReader):
            from website_analytics.analysis.completeness import registration_ids,project_records,validate_observations
            validate_observations(observations,as_of=captured)
            result.update(source_ids=registration_ids(reader.site),observations=observations,metadata_diagnostics=metadata_diagnostics,blocked_sources=sorted(blocked_sources))
            result["scoped_completeness"]=project_records(records,as_of=captured,observations=observations,source_ids=result["source_ids"])
            if not result["scoped_completeness"]["strict_complete"]:result["status"]="partial"
        if len(canonical_json(result).encode())>512000: raise AnalysisError("bounded worker output exceeded")
        run_log.append(run_id,scope,stage,"ok" if result["status"]=="ok" else "partial")
        return result
    except Exception:
        run_log.append(run_id,scope,stage,"failed")
        raise AnalysisError("worker_failed_see_safe_stage_log") from None


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request",required=True)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--sample-evidence");source.add_argument("--registered-config",help="explicit existing-access worker mode; never invoked by the query service")
    parser.add_argument("--run-log",required=True);parser.add_argument("--run-id",required=True)
    parser.add_argument("--output",required=True);parser.add_argument("--as-of")
    parser.add_argument("--metadata-windows",help="operator-owned bounded GSC metadata window JSON; no implicit range extension")
    args=parser.parse_args(argv)
    raw=None
    try:
        raw=read_json(args.request)
        try:
            clock=timestamp(args.as_of) if args.as_of else datetime.now(timezone.utc)
            _,_,periods=parse_request(raw,as_of=clock)
            metadata_windows=parse_metadata_windows(read_json(args.metadata_windows)) if args.metadata_windows else None
            validate_metadata_windows(periods,metadata_windows,clock)
            reader=ExactFixtureReader(args.sample_evidence) if args.sample_evidence else registered_reader(args.registered_config)
        except Exception:
            RunLog(args.run_log).append(args.run_id,fingerprint(raw),"preflight","failed")
            raise AnalysisError("reader_initialization_failed") from None
        result=query(raw,reader,run_id=args.run_id,
                     run_log=RunLog(args.run_log),as_of=timestamp(args.as_of) if args.as_of else None,metadata_windows=metadata_windows)
        with Path(args.output).open("x",encoding="utf-8") as stream:stream.write(canonical_json(result)+"\n")
        print(json.dumps({"sample":reader.sample,"status":result["status"],"source_network_initialized":not reader.sample}))
        return 0 if result["status"]=="ok" else 3
    except Exception:
        print(json.dumps({"status":"error","error":"invalid_local_worker_input_or_state"}));return 2


if __name__=="__main__": raise SystemExit(main())
