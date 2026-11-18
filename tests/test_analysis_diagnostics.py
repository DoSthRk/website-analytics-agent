from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from test_analysis_readers import PERIOD, PAGES, site
from website_analytics.analysis.contracts import AnalysisError, Selection
from website_analytics.analysis.diagnostics import diagnostic_notes
from website_analytics.analysis.readers import AdapterReader


class HTTPFailure(Exception):
    def __init__(self, status):
        super().__init__("https://secret.invalid/?token=private customer@example.test")
        self.resp = SimpleNamespace(status=status)

    @property
    def content(self):
        raise AssertionError("must not read response bodies")

    def __str__(self):
        raise AssertionError("must not stringify exception")


@pytest.mark.parametrize("status,category", [(400, "request_rejected"), (401, "access_denied"), (403, "access_denied"), (429, "rate_limited"), (503, "transient_http")])
def test_http_diagnostics_are_fixed_codes_without_body_or_message(status, category):
    error = HTTPFailure(status)
    assert diagnostic_notes(error) == ("error_category_" + category, "http_status_" + str(status))


def test_diagnostics_preserve_known_contract_reason_and_only_allowlisted_oauth_code():
    assert diagnostic_notes(AnalysisError("GSC interval aggregate must not contain multiple rows")) == ("error_category_contract_validation", "error_reason_gsc_multiple_aggregate_rows")
    assert diagnostic_notes(RuntimeError({"error": "invalid_grant", "error_description": "private-token"})) == ("error_category_authorization_failed", "oauth_invalid_grant")
    assert diagnostic_notes(RuntimeError({"error": "https://secret.invalid/?code=private"})) == ("error_category_unknown",)


def test_bounded_cause_chain_timeout_and_unknown_status_do_not_leak():
    outer = RuntimeError("private DSN")
    outer.__cause__ = HTTPFailure(403)
    outer.__cause__.__cause__ = outer
    assert diagnostic_notes(outer) == ("error_category_access_denied", "http_status_403")
    assert diagnostic_notes(TimeoutError("private URL")) == ("error_category_timeout",)
    assert diagnostic_notes(HTTPFailure("403 secret")) == ("error_category_unknown",)
    assert diagnostic_notes(HTTPFailure(True)) == ("error_category_unknown",)


def test_reader_captures_safe_diagnostics_and_error_stays_ineligible():
    def fail(*args, **kwargs):
        raise HTTPFailure(403)
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    reader = AdapterReader(site(), gsc=SimpleNamespace(query_exact_result=fail), exact_paths=True, clock=lambda: now)
    evidence = reader.read("gsc", PERIOD, PAGES, Selection())
    assert evidence.notes == ("source_request_failed", "error_category_access_denied", "http_status_403")
    assessed = evidence.assess(PERIOD, evidence.scope_digest, sample=False, as_of=now)
    assert not assessed["comparison_eligible"] and evidence.metrics == {}


def test_unexpected_gsc_grain_is_safe_reason_code():
    reader = AdapterReader(site(), gsc=SimpleNamespace(query_exact_result=lambda *a, **kw: SimpleNamespace(rows=({}, {}), truncated=False)), exact_paths=True)
    evidence = reader.read("gsc", PERIOD, PAGES, Selection())
    assert evidence.status == "error"
    assert "error_reason_gsc_multiple_aggregate_rows" in evidence.notes


def test_google_status_and_database_denials_keep_only_allowlisted_codes():
    error=RuntimeError("private source text");error.code=403
    assert diagnostic_notes(error)==("error_category_access_denied","http_status_403")
    assert diagnostic_notes(RuntimeError(1045,"mysql://private"))==("error_category_access_denied","sql_status_1045")
