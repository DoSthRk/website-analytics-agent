"""Versioned, conservative scope-bound projections; no fetches or writes.

These views do not rewrite the legacy assessment or pinned report. In particular
an old observed-cutoff assertion is not a newly verified provider watermark.
"""
from __future__ import annotations

from datetime import timedelta
import re
from zoneinfo import ZoneInfo

from website_analytics.analysis.contracts import AnalysisError, Selection, calendar_date, fingerprint, metric_number, page_path, strict_keys, timestamp, timezone_name
from website_analytics.analysis.query import METRICS

CONTRACT = "scoped-completeness/v1"
OBSERVATION = "source-observation/v1"


def registration_ids(site):
    """Public registry identity only: never hash a DSN, credential or secret."""
    common = {"site": site.site_key, "hosts": sorted(site.domains),"scope_contract":"genemedi-exact/v1"}
    source = site.inquiry_source
    return {"ga4": fingerprint({**common, "source": "ga4", "property": site.ga4_property_id}),
        "gsc": fingerprint({**common, "source": "gsc", "property": site.gsc_property_url,"search_type":"web"}),
        "inquiry": fingerprint({**common, "source": "inquiry", "database": getattr(source,"registered_database",None),
            "table": getattr(source,"registered_table",None), "scope": getattr(source,"site_scope",None),
            "kind":getattr(source,"kind",None),"allowed_hosts":sorted(getattr(source,"allowed_hosts",()))})}


def scope(value):
    strict_keys(value, {"site","start","end","pages","countries","channels"})
    first,last=calendar_date(value["start"]),calendar_date(value["end"])
    if value["site"]!="genemedi-net" or first>last or (last-first).days>13:
        raise AnalysisError("invalid bounded observation scope")
    paths=value["pages"]
    if not isinstance(paths,list) or not 1<=len(paths)<=20 or len(set(paths))!=len(paths):
        raise AnalysisError("invalid exact observation pages")
    for path in paths:
        page_path(path)
        if not path.startswith("/") or "?" in path or "#" in path:
            raise AnalysisError("exact observation paths required")
    selection=Selection.parse({"countries":value["countries"],"channels":value["channels"]})
    if list(selection.countries)!=value["countries"] or list(selection.channels)!=value["channels"]:
        raise AnalysisError("noncanonical observation segment")
    return first,last


def validate_observations(rows, *, as_of):
    if not isinstance(rows,list) or len(rows)>36:
        raise AnalysisError("invalid observation budget")
    seen=set()
    for row in rows:
        strict_keys(row,{"contract","source","source_id","scope","fetched_at","calendar","dates_observed","maturity","quality"})
        source=row["source"]
        if row["contract"]!=OBSERVATION or source not in METRICS or not isinstance(row["source_id"],str) or not re.fullmatch(r"[a-f0-9]{64}",row["source_id"]):
            raise AnalysisError("invalid source observation identity")
        first,last=scope(row["scope"])
        if timestamp(row["fetched_at"])>as_of:
            raise AnalysisError("future source observation")
        key=(source,row["source_id"],fingerprint(row["scope"]))
        if key in seen:raise AnalysisError("duplicate source observation")
        seen.add(key)
        calendar=strict_keys(row["calendar"],{"name","basis"})
        allowed={"ga4":{"unknown","api_response"},"gsc":{"unknown","api_contract"},"inquiry":{"unknown","writer_verified"}}[source]
        if calendar["basis"] not in allowed or ((calendar["name"] is None)!=(calendar["basis"]=="unknown")):
            raise AnalysisError("invalid source calendar evidence")
        if calendar["name"] is not None:timezone_name(calendar["name"])
        if source=="gsc" and calendar["name"] not in {None,"America/Los_Angeles"}:
            raise AnalysisError("GSC calendar conflicts with API contract")
        source_today=timestamp(row["fetched_at"]).astimezone(ZoneInfo(calendar["name"])).date() if calendar["name"] else timestamp(row["fetched_at"]).date()
        if last>source_today:raise AnalysisError("observation request includes future source date")
        days=row["dates_observed"]
        if not isinstance(days,list) or len(days)>14 or len(set(days))!=len(days) or any(not first<=calendar_date(day)<=last for day in days):
            raise AnalysisError("invalid observed dates")
        maturity=row["maturity"]
        if source=="gsc":
            strict_keys(maturity,{"basis","data_state","first_incomplete_date"})
            if maturity["basis"]!="gsc_api_metadata" or maturity["data_state"] not in {"all","final"}:
                raise AnalysisError("invalid GSC maturity evidence")
            incomplete=maturity["first_incomplete_date"]
            if incomplete is not None and (maturity["data_state"]!="all" or not first<=calendar_date(incomplete)<=last):
                raise AnalysisError("invalid scoped GSC processing boundary")
        elif source=="ga4":
            strict_keys(maturity,{"basis","estimated_through","policy_version"})
            if maturity["basis"] not in {"unknown","configured_lag"}:
                raise AnalysisError("GA4 provider finality cannot be inferred")
            estimate=maturity["estimated_through"];policy=maturity["policy_version"]
            if maturity["basis"]=="unknown":
                if estimate is not None or policy is not None:raise AnalysisError("unknown maturity cannot have an estimate")
            elif not isinstance(policy,str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./-]{0,79}",policy) or estimate is None or calendar_date(estimate)>timestamp(row["fetched_at"]).date():
                raise AnalysisError("invalid maturity estimate")
        else:
            strict_keys(maturity,{"basis","max_record_date"})
            if maturity["basis"]!="stored_date_observation":raise AnalysisError("inquiry date is not a finality watermark")
            if maturity["max_record_date"] is not None:calendar_date(maturity["max_record_date"])
        quality=row["quality"]
        allowed={"ga4":{"sampled","thresholded","other_row_loss","truncation_reasons_supported","truncated","restricted"},
            "gsc":{"truncated"},"inquiry":{"truncated","unknown_attribution_count"}}[source]
        strict_keys(quality,set(),allowed)
        for key,value in quality.items():
            if key=="unknown_attribution_count":
                if value is not None:metric_number(value)
            elif value is not None and type(value) is not bool:raise AnalysisError("nullable quality flag required")
        if source=="ga4" and quality.get("truncation_reasons_supported") is not True and quality.get("truncated") is False:
            raise AnalysisError("unsupported truncation metadata cannot mean no truncation")
    return rows


def _matches(observation, record, source_ids):
    target=record["scope"];provided=observation["scope"]
    source=record["evidence"]["source"]
    return (observation["source"]==source and source_ids.get(source)==observation["source_id"]
        and all(provided[k]==target[k] for k in ("site","pages","countries","channels"))
        and provided["start"]<=target["start"]<=target["end"]<=provided["end"])


def project_records(records, *, as_of, observations=(), source_ids=None):
    rows=validate_observations(list(observations),as_of=as_of)
    ids=source_ids or {}
    result=[]
    for record in records:
        evidence,target=record["evidence"],record["scope"]
        source=evidence["source"]
        matches=[r for r in rows if _matches(r,record,ids)]
        # Overlapping proofs are not resolved by arbitrary ordering.
        obs=matches[0] if len(matches)==1 else None
        limitations=[]
        if len(matches)>1:limitations.append("ambiguous_overlapping_observations")
        elif rows and obs is None:limitations.append("no_matching_scope_registration_and_range_proof")
        if obs and evidence["date_boundary"]!="unverified" and obs["calendar"]["name"] is not None and evidence["date_boundary"]!=obs["calendar"]["name"]:
            limitations.append("source_calendar_observation_conflict")
            obs=None
        name=evidence["date_boundary"] if evidence["date_boundary"]!="unverified" else None
        calendar={"name":name,"basis":"legacy_captured" if name else "unknown"}
        coverage={"state":"legacy_reported" if evidence["coverage_start"] and evidence["coverage_end"] else "unknown",
            "reported_start":evidence["coverage_start"],"reported_end":evidence["coverage_end"],"dates_observed":[],"missing_rows_are_zero":False}
        maturity={"state":"unknown","observed_complete_through":None,"estimated_through":None,
            "legacy_reported_through":evidence["complete_through"],"legacy_cutoff_basis":evidence["cutoff_basis"],"basis":"unverified"}
        if source=="ga4" and evidence["cutoff_basis"]=="configured_lag":
            maturity.update(state="estimated",estimated_through=evidence["complete_through"],basis="legacy_configured_lag")
        quality={"state":"unknown","flags":{},"limitations":[]}
        if source=="ga4" and obs is None:
            quality["limitations"].append("sdk_truncation_metadata_not_captured")
        if obs:
            calendar=obs["calendar"]
            present=[day for day in obs["dates_observed"] if target["start"]<=day<=target["end"]]
            first,last=calendar_date(target["start"]),calendar_date(target["end"])
            expected={(first+timedelta(days=i)).isoformat() for i in range((last-first).days+1)}
            coverage.update(state="observed_dates" if set(present)==expected else "partial_date_observation",dates_observed=present)
            maturity.update(basis=obs["maturity"]["basis"],observed_at=obs["fetched_at"])
            flags=obs["quality"];quality["flags"]=flags
            if source=="gsc":
                incomplete=obs["maturity"]["first_incomplete_date"]
                maturity["requested_data_state"]=obs["maturity"]["data_state"]
                if incomplete:
                    maturity.update(state="provider_final_for_window" if target["end"]<incomplete else "provider_partial",
                        observed_complete_through=min((calendar_date(incomplete)-timedelta(days=1)).isoformat(),obs["scope"]["end"]))
                elif obs["maturity"]["data_state"]=="final":maturity["state"]="finalized_rows_without_watermark"
                quality.update(state="degraded" if flags.get("truncated") else "limited",limitations=["gsc_api_does_not_guarantee_all_search_rows"])
            elif source=="ga4":
                maturity.update(state="estimated" if obs["maturity"]["basis"]=="configured_lag" else "unknown",estimated_through=obs["maturity"]["estimated_through"])
                degraded=any(flags.get(k) is True for k in ("sampled","thresholded","other_row_loss","truncated","restricted"))
                known=set(flags)=={"sampled","thresholded","other_row_loss","truncation_reasons_supported","truncated","restricted"} and flags["truncation_reasons_supported"] is True and all(type(v) is bool for v in flags.values())
                quality["state"]="degraded" if degraded else "no_observed_flags" if known else "unknown"
                if flags.get("truncation_reasons_supported") is not True:quality["limitations"].append("sdk_truncation_metadata_unknown")
            else:
                maturity.update(state="mutable_as_of",max_record_date=obs["maturity"]["max_record_date"])
                unknown=flags.get("unknown_attribution_count")
                quality["state"]="degraded" if flags.get("truncated") or unknown is not None and unknown>0 else "no_observed_flags" if flags.get("truncated") is False and unknown==0 else "unknown"
        if source=="inquiry":
            maturity.update(state="mutable_as_of",observed_complete_through=None)
            limitations.append("stored_date_and_max_record_date_do_not_prove_calendar_or_finality")
        if source=="ga4":limitations.append("sampling_and_lag_do_not_prove_provider_finality")
        request="failed" if evidence["status"]=="error" else "unavailable" if evidence["status"]=="unavailable" else "succeeded"
        metrics_present=set(METRICS[source])<=evidence["metrics"].keys()
        complete=(request=="succeeded" and metrics_present and coverage["state"]=="observed_dates" and calendar["basis"] not in {"unknown","legacy_captured"}
            and quality["state"]=="no_observed_flags" and maturity["state"]=="provider_final_for_window")
        result.append({"source":source,"scope":target,"scope_digest":fingerprint(target),
            "provenance":{"source_id":ids.get(source),"observation_id":fingerprint(obs) if obs else None,"observation_fetched_at":obs["fetched_at"] if obs else None,
                "observation_precedes_request":timestamp(obs["fetched_at"])<timestamp(evidence["fetched_at"]) if obs else None},
            "request":{"state":request,"fetched_at":evidence["fetched_at"]},"coverage":coverage,"calendar":calendar,
            "maturity":maturity,"quality":quality,"metrics_present":metrics_present,"strict_complete":complete,"limitations":limitations})
    return {"contract":CONTRACT,"strict_complete":bool(result) and all(r["strict_complete"] for r in result),
        "status":"complete" if result and all(r["strict_complete"] for r in result) else "partial","source_windows":result,
        "batch_confirmation_or_causal_claim":False,"legacy_report_digest_unchanged":True}
