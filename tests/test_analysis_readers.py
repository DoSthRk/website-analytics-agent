import json
import re
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from website_analytics.adapters.ga4 import GA4Adapter
from website_analytics.adapters.gsc import GSCAdapter
from website_analytics.adapters.inquiry import InquiryQueryResult
from website_analytics.adapters.scope import page_regex
from website_analytics.analysis.contracts import AnalysisError, Selection, fingerprint, scope_document
from website_analytics.analysis.query import SourceEvidence
from website_analytics.analysis.readers import AdapterReader, FixtureReader, read_json
from website_analytics.models import DateRange, SiteConfig

PERIOD = DateRange(date(2026, 8, 1), date(2026, 8, 7))
NOW = datetime(2026, 8, 21, tzinfo=timezone.utc)
PAGES = ("/i/aav-example",)


def site():
    return SiteConfig("genemedi-net", "GeneMedi", ("genemedi.net", "www.genemedi.net"), "America/Los_Angeles", "123", "https://www.genemedi.net/")


class GAClient:
    def __init__(self):
        self.requests = []

    def run_report(self, request):
        self.requests.append(request)
        return SimpleNamespace(row_count=1, rows=[SimpleNamespace(dimension_values=[], metric_values=[SimpleNamespace(value=str(n)) for n in (100, 70, 65, 50, .5, 200, 4)])])


class GSCService:
    def __init__(self):
        self.requests = []

    def searchanalytics(self):
        return self

    def query(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(execute=lambda: {"rows": [{"keys": [], "clicks": 10, "impressions": 100, "ctr": .1, "position": 8}]})


def test_ga4_scoped_interval_request_keeps_hostnames_and_pushes_segments_to_source():
    client = GAClient()
    result = GA4Adapter(client).scoped_aggregate(site(), PERIOD, PAGES, countries=("JP",), channels=("Organic Search",))
    request = client.requests[0]
    assert list(request.dimensions) == []
    assert result[0]["totalUsers"] == 70
    filters = list(request.dimension_filter.and_group.expressions)
    assert [e.filter.string_filter.value for e in filters[0].or_group.expressions] == list(site().domains)
    assert filters[1].filter.field_name == "landingPagePlusQueryString"
    pattern = filters[1].filter.string_filter.value
    assert re.fullmatch(pattern, "/i/aav-example?utm_source=test")
    assert not re.fullmatch(pattern, "/i/aav-example-other")
    assert filters[2].filter.field_name == "countryId"
    assert list(filters[2].filter.in_list_filter.values) == ["JP"]
    assert filters[3].filter.field_name == "sessionDefaultChannelGroup"


def test_gsc_scoped_request_uses_existing_pagination_and_final_data_with_exact_website_boundary():
    service = GSCService()
    result = GSCAdapter(service).query_result(site(), PERIOD, (), page_paths=PAGES)
    assert result.rows[0]["clicks"] == 10 and not result.truncated
    body = service.requests[0]["body"]
    assert body["dimensions"] == [] and body["dataState"] == "final" and body["type"] == "web"
    pattern = body["dimensionFilterGroups"][0]["filters"][0]["expression"]
    assert re.fullmatch(pattern, "https://www.genemedi.net/i/aav-example/?x=1")
    assert not re.fullmatch(pattern, "https://tarmart.com/i/aav-example")
    assert not re.fullmatch(pattern, "https://www.genemedi.net.evil.test/i/aav-example")
    assert service.requests[0]["siteUrl"] == site().gsc_property_url


@pytest.mark.parametrize("paths", [[], ["//evil.test/x"], ["/x?secret=1"], ["/i/../x"], ["/x"]*101, ["/"+"x"*4096]])
def test_filter_validation_rejects_before_any_request(paths):
    client = GAClient()
    with pytest.raises(ValueError):
        GA4Adapter(client).scoped_aggregate(site(), PERIOD, paths)
    assert not client.requests


def test_injected_adapter_reader_requires_independent_finality_and_coverage_evidence():
    reader = AdapterReader(site(), ga4=GA4Adapter(GAClient()), clock=lambda: NOW)
    evidence = reader.read("ga4", PERIOD, PAGES, Selection())
    digest = fingerprint(scope_document(PERIOD, PAGES, Selection()))
    assessed = evidence.assess(PERIOD, digest, sample=False, as_of=NOW)
    assert assessed["status"] == "partial"
    assert assessed["metrics"]["totalUsers"] == 70
    assert not assessed["comparison_eligible"]
    assert "finality_is_estimate_or_unknown" in assessed["limitations"]


def test_injected_existing_adapters_can_supply_verified_aggregate_without_summing_uv():
    stamp = {("ga4", "2026-08-01", "2026-08-07"): {"date_boundary": "America/Los_Angeles", "coverage_start": "2026-08-01", "coverage_end": "2026-08-07", "complete_through": "2026-08-19", "cutoff_basis": "observed"}}
    reader = AdapterReader(site(), ga4=GA4Adapter(GAClient()), window_evidence=stamp, clock=lambda: NOW)
    evidence = reader.read("ga4", PERIOD, PAGES, Selection())
    assert evidence.assess(PERIOD, evidence.scope_digest, sample=False, as_of=NOW)["comparison_eligible"]
    assert evidence.metrics["totalUsers"] == 70


def inquiry_result(*, truncated=False, foreign=False):
    def row(url, n):
        return {"sourceUrl": url, "storedSubmissions": n, "quarantinedSubmissions": 0, "nonQuarantinedSubmissions": n}
    return InquiryQueryResult(daily=(), pages=(row("https://www.genemedi.net/i/aav-example", 3), row("https://www.genemedi.net/outside-batch", 4), row("[UNATTRIBUTED]", 1)) + ((row("https://tarmart.com/foreign", 1),) if foreign else ()), totals={"storedSubmissions": 10, "quarantinedSubmissions": 0, "nonQuarantinedSubmissions": 10}, page_rows_truncated=truncated)


def test_inquiry_unknown_is_sitewide_and_never_assigned_to_batch_or_visitor_segment():
    adapter = SimpleNamespace(query=lambda period: inquiry_result())
    reader = AdapterReader(site(), inquiry=adapter, clock=lambda: NOW)
    evidence = reader.read("inquiry", PERIOD, PAGES, Selection(countries=("JP",)))
    assert evidence.metrics["storedSubmissions"] == 3
    assert evidence.unattributed_submissions == 3  # 10 - selected 3 - other valid pages 4
    assert "unattributed_is_sitewide_not_batch_count" in evidence.notes
    reader = AdapterReader(site(), inquiry=SimpleNamespace(query=lambda period: inquiry_result(truncated=True)), clock=lambda: NOW)
    assert reader.read("inquiry", PERIOD, PAGES, Selection()).unattributed_submissions is None


def test_foreign_inquiry_fails_instead_of_silently_filtering_or_merging():
    reader = AdapterReader(site(), inquiry=SimpleNamespace(query=lambda period: inquiry_result(foreign=True)), clock=lambda: NOW)
    evidence = reader.read("inquiry", PERIOD, PAGES, Selection())
    assert evidence.status == "error" and evidence.metrics == {}
    assert "tarmart" not in str(evidence)


def test_source_errors_are_sanitized_and_other_sites_rejected():
    def bad(*args, **kwargs):
        raise RuntimeError("mysql://password@secret-host/?token=hidden")
    reader = AdapterReader(site(), ga4=SimpleNamespace(scoped_aggregate=bad), clock=lambda: NOW)
    evidence = reader.read("ga4", PERIOD, PAGES, Selection())
    assert evidence.status == "error" and "password" not in str(evidence)
    with pytest.raises(AnalysisError):
        AdapterReader(replace(site(), domains=("tarmart.com",)))


def test_reader_rejects_duplicate_json_keys_and_duplicate_or_non_sample_evidence(tmp_path):
    path = tmp_path/"input.json"
    path.write_text('{"sample": true, "sample": false}')
    with pytest.raises(AnalysisError):
        read_json(path)
    original = json.loads((Path(__file__).resolve().parents[1]/"examples/batch-analysis/evidence.json").read_text())
    original["records"].append(original["records"][0])
    path.write_text(json.dumps(original))
    with pytest.raises(AnalysisError, match="duplicate fixture"):
        FixtureReader(path)
    original["sample"] = False
    path.write_text(json.dumps(original))
    with pytest.raises(AnalysisError):
        FixtureReader(path)


def test_evidence_scope_cannot_be_relabelled_or_include_secret_notes():
    digest = fingerprint(scope_document(PERIOD, PAGES, Selection()))
    evidence = SourceEvidence("ga4", "wrong", "complete", {}, NOW.isoformat(), "test")
    with pytest.raises(AnalysisError):
        evidence.assess(PERIOD, digest, sample=False, as_of=NOW)
    with pytest.raises(AnalysisError):
        replace(evidence, scope_digest=digest, notes=("secret https://example.test",)).assess(PERIOD, digest, sample=False, as_of=NOW)
