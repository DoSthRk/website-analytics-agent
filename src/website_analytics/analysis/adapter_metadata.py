"""Actual adapter -> scope-bound observation bridge; never creates clients.

Only the worker calls this during an explicit source-reading phase. HTTP replay
does not import this module or trigger a request. Failures retain the aggregate
and expose bounded diagnostics instead of inventing metadata or finality.
"""
from __future__ import annotations

from website_analytics.analysis.completeness import registration_ids, validate_observations
from website_analytics.analysis.contracts import AnalysisError, fingerprint, scope_document
from website_analytics.analysis.diagnostics import diagnostic_notes

_REASONS={"metadata adapter unsupported":"metadata_adapter_unsupported",
    "metadata request scope mismatch":"metadata_request_scope_mismatch",
    "metadata segments unsupported":"metadata_segments_unsupported",
    "missing inquiry metadata":"metadata_not_captured"}


def capture_observation(reader,source,period,pages,selection,evidence,*,metadata_period=None):
    target=scope_document(period,pages,selection)
    def diagnostic(notes):return {"source":source,"scope_digest":fingerprint(target),"notes":list(notes)}
    if evidence.status in {"error","unavailable"}:
        return None,diagnostic(("metadata_skipped_after_source_error",))
    try:
        if evidence.source!=source or evidence.scope_digest!=fingerprint(target):raise AnalysisError("metadata request scope mismatch")
        if reader.exact_paths is not True:raise AnalysisError("metadata adapter unsupported")
        adapter=reader.adapters[source]
        source_id=registration_ids(reader.site)[source]
        scope=target;calendar={"name":None,"basis":"unknown"};days=[]
        if source=="ga4":
            expected={**target,"property":reader.site.ga4_property_id,"domains":sorted(reader.site.domains)}
            if getattr(adapter,"last_exact_scope",None)!=expected:raise AnalysisError("metadata request scope mismatch")
            meta=adapter.last_response_metadata
            if meta.get("timezone") is not None:calendar={"name":meta["timezone"],"basis":"api_response"}
            estimated=evidence.complete_through if evidence.cutoff_basis=="configured_lag" else None
            maturity={"basis":"configured_lag" if estimated is not None else "unknown","estimated_through":estimated,
                "policy_version":"legacy-captured-lag/v1" if estimated is not None else None}
            quality={"sampled":meta.get("sampled"),"thresholded":meta.get("thresholded"),"other_row_loss":meta.get("other_row_loss"),
                "truncation_reasons_supported":meta.get("truncation_reasons_supported"),
                "truncated":True if meta.get("provider_truncated") is True else meta.get("provider_truncated") if meta.get("truncation_reasons_supported") is True else None,
                "restricted":meta.get("restricted")}
        elif source=="gsc":
            if selection.countries or selection.channels:raise AnalysisError("metadata segments unsupported")
            if getattr(adapter,"exact_metadata_contract",None)!="genemedi-exact-metadata/v1" or not callable(getattr(adapter,"availability_exact_result",None)):
                raise AnalysisError("metadata adapter unsupported")
            window=metadata_period or period
            if not window.start<=period.start<=period.end<=window.end:raise AnalysisError("metadata request scope mismatch")
            result=adapter.availability_exact_result(reader.site,window,page_paths=pages)
            scope=scope_document(window,pages,selection)
            expected={**scope,"property":reader.site.gsc_property_url,"domains":sorted(reader.site.domains)}
            if result.request_scope!=expected:raise AnalysisError("metadata request scope mismatch")
            calendar={"name":"America/Los_Angeles","basis":"api_contract"};days=list(result.dates_observed)
            maturity={"basis":"gsc_api_metadata","data_state":"all","first_incomplete_date":result.first_incomplete_date}
            quality={"truncated":result.truncated}
        else:
            meta=getattr(reader,"last_inquiry_metadata",None)
            if not meta or meta["scope_digest"]!=fingerprint(target):raise AnalysisError("missing inquiry metadata")
            # Database-wide dates are not page-cohort date coverage or a known
            # submission calendar. No new SQL or customer row access occurs.
            maturity={"basis":"stored_date_observation","max_record_date":meta["max_record_date"]}
            quality={"truncated":meta["truncated"],"unknown_attribution_count":meta["unknown_attribution_count"]}
        row={"contract":"source-observation/v1","source":source,"source_id":source_id,"scope":scope,
            "fetched_at":reader.clock().isoformat(),"calendar":calendar,"dates_observed":days,"maturity":maturity,"quality":quality}
        validate_observations([row],as_of=reader.clock())
        return row,None
    except Exception as error:
        reason=_REASONS.get(error.args[0]) if error.args and type(error.args[0]) is str else None
        return None,diagnostic(((reason,) if reason else ())+diagnostic_notes(error))
