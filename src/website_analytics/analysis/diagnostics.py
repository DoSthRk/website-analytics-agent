"""Bounded public diagnostics; never stringify exceptions or inspect bodies."""
from __future__ import annotations

import errno

_REASONS = {
    "GSC interval aggregate must not contain multiple rows": "gsc_multiple_aggregate_rows",
    "GA4 interval aggregate must not contain multiple rows": "ga4_multiple_aggregate_rows",
    "invalid source time metadata": "invalid_source_time_metadata",
    "GSC response field 'rows' must be a list": "gsc_invalid_rows",
    "GSC response rows must be mappings": "gsc_invalid_rows",
    "GSC metric values must be numeric": "gsc_invalid_metrics",
}
_OAUTH = {"invalid_grant", "invalid_client", "access_denied", "temporarily_unavailable"}


def diagnostic_notes(error: Exception) -> tuple[str, ...]:
    """Read only structured status, allowlisted codes and bounded cause chains.

    No message, URL, headers, content, request or response body is returned.
    Categories are diagnostics, not proof of the underlying failure cause.
    """
    chain = []
    seen = set()
    current = error
    while isinstance(current, Exception) and id(current) not in seen and len(chain) < 4:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__
    category = "unknown"
    status = None
    reason = None
    oauth = None
    sql_status = None
    for item in chain:
        response = getattr(item, "resp", None)
        candidate = getattr(response, "status", None)
        if candidate is None:
            candidate = getattr(getattr(item, "response", None), "status_code", None)
        if candidate is None:
            candidate = getattr(item,"status_code",None) or getattr(item,"code",None)
        if type(candidate) is int and 400 <= candidate <= 599:
            status = candidate
        for arg in item.args:
            if type(arg) is int and arg in {1044,1045,1142}:sql_status=arg
            if isinstance(arg, str) and arg in _REASONS:
                reason = _REASONS[arg]
            if isinstance(arg, dict) and isinstance(arg.get("error"), str) and arg["error"] in _OAUTH:
                oauth = arg["error"]
        if isinstance(item, TimeoutError) or (type(item).__module__.startswith(("requests.", "urllib3.")) and type(item).__name__ in {"Timeout", "ReadTimeout", "ConnectTimeout", "ReadTimeoutError", "ConnectTimeoutError"}):
            category = "timeout"
        elif isinstance(item, OSError) and item.errno in {errno.ECONNRESET, errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH}:
            category = "transport_error"
        elif type(item).__name__ == "AnalysisError" and type(item).__module__ == "website_analytics.analysis.contracts":
            category = "contract_validation"
        elif isinstance(item, (ValueError, TypeError, KeyError)):
            category = "invalid_response_or_contract"
    if oauth in {"invalid_grant", "invalid_client", "access_denied"}:
        category = "authorization_failed"
    if sql_status is not None:category="access_denied"
    if status is not None:
        category = "access_denied" if status in {401, 403} else "rate_limited" if status == 429 else "transient_http" if status >= 500 else "request_rejected"
    notes = ["error_category_" + category]
    if status is not None:
        notes.append("http_status_" + str(status))
    if oauth is not None:
        notes.append("oauth_" + oauth)
    if sql_status is not None:notes.append("sql_status_"+str(sql_status))
    if reason is not None:
        notes.append("error_reason_" + reason)
    return tuple(notes)
