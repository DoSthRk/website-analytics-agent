import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from website_analytics.analysis.batches import BatchStore
from website_analytics.analysis.contracts import AnalysisError, BatchDraft, Query
from website_analytics.analysis.query import analyze
from website_analytics.analysis.readers import FixtureReader
from website_analytics.page_classification import build_page_dimension, load_page_classification
from website_analytics.product_mapping import load_product_mapping

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT/"examples/batch-analysis"
AS_OF = datetime(2026, 8, 21, tzinfo=timezone.utc)


def setup_case(tmp_path, *, fixture=None, request=None):
    draft = BatchDraft.parse(json.loads((EXAMPLES/"draft.json").read_text()))
    batch = BatchStore(tmp_path/"batches.sqlite").confirm(draft, draft.digest, "synthetic-test", confirmed_at="2026-08-20T12:00:00Z")
    query = Query.parse(request or json.loads((EXAMPLES/"query.json").read_text()), draft)
    path = EXAMPLES/"evidence.json"
    if fixture is not None:
        path = tmp_path/"evidence.json"
        path.write_text(json.dumps(fixture))
    reader = FixtureReader(path)
    dimension = build_page_dimension(load_page_classification(ROOT/"config/page_classifications/genemedi-net.yaml", "genemedi-net"), reader.dimension_rows)
    mapping = load_product_mapping(ROOT/"config/product_mappings/genemedi-net.yaml", "genemedi-net")
    return batch, query, reader, dimension, mapping


def run_case(tmp_path, **kwargs):
    return analyze(*setup_case(tmp_path, **kwargs), as_of=AS_OF)


def fixture_data():
    return json.loads((EXAMPLES/"evidence.json").read_text())


def test_complete_synthetic_vertical_slice_uses_current_classification_and_source_metrics(tmp_path):
    out = run_case(tmp_path)
    assert out["sample"] is True and out["status"] == "ok"
    assert out["metric_priority"] == ["gsc", "ga4", "inquiry"]
    assert out["visitor_label"] == "ordinary_traffic_target_visitors_undefined"
    rows = out["cohorts"]["changed"]["comparison"]["metrics"]
    assert rows["ga4"]["totalUsers"]["current"] == 80  # source interval aggregate, never page/day sums
    assert rows["ga4"]["sessions"]["delta"] == 20
    assert rows["gsc"]["ctr"]["current"] == 0.05
    assert rows["gsc"]["ctr"]["delta"] == -0.05
    assert rows["inquiry"]["storedSubmissions"]["delta"] == 2
    assert out["cohorts"]["changed"]["before"]["inquiry"]["unattributed_submissions_sitewide"] == 3
    assert out["control_context"]["ga4"]["sessions"] == {"changed_delta": 20, "control_delta": 5}
    assert out["pages"][0]["page_class"] == "product_page"
    assert out["pages"][1]["page_class"] == "unknown_unmapped"
    assert "observational_before_after_not_causal" in out["limitations"]
    assert "causal_lift" not in out


@pytest.mark.parametrize("patch,reason", [({"truncated": True}, "source_truncated"), ({"coverage_start": "2026-08-02"}, "requested_dates_not_covered"), ({"complete_through": "2026-08-06"}, "period_not_finalized"), ({"cutoff_basis": "configured_lag"}, "finality_is_estimate_or_unknown"), ({"missing_dates": ["2026-08-03"]}, "source_dates_missing"), ({"status": "partial"}, "source_partial")])
def test_partial_evidence_keeps_observed_values_but_withholds_effect_delta(tmp_path, patch, reason):
    fixture = fixture_data()
    fixture["records"][0]["evidence"].update(patch)  # changed/before/ga4
    out = run_case(tmp_path, fixture=fixture)
    assert out["status"] == "partial"
    source = out["cohorts"]["changed"]["before"]["ga4"]
    assert reason in source["limitations"]
    assert source["metrics"]["sessions"] == 100
    metric = out["cohorts"]["changed"]["comparison"]["metrics"]["ga4"]["sessions"]
    assert metric["previous"] == 100 and metric["delta"] is None and not metric["available"]


def test_missing_source_is_not_zero_and_does_not_contaminate_other_sources(tmp_path):
    fixture = fixture_data()
    fixture["records"] = [r for r in fixture["records"] if not (r["source"] == "inquiry" and r["scope"]["start"] == "2026-08-01")]
    out = run_case(tmp_path, fixture=fixture)
    rows = out["cohorts"]["changed"]["comparison"]["metrics"]
    assert rows["inquiry"]["storedSubmissions"]["previous"] is None
    assert rows["inquiry"]["storedSubmissions"]["delta"] is None
    assert rows["ga4"]["sessions"]["delta"] == 20


def test_country_channel_page_selections_require_exact_evidence_scope(tmp_path):
    req = json.loads((EXAMPLES/"query.json").read_text())
    req["selection"] = {"countries": ["JP"], "channels": ["Organic Search"], "pages": ["/i/aav-example"]}
    out = run_case(tmp_path, request=req)
    assert out["status"] == "partial"  # never silently use unfiltered sample rows
    assert out["visitor_label"] == "filtered_ga4_visitors"
    assert len([x for x in out["pages"] if x["cohort"] == "changed"]) == 1
    assert out["cohorts"]["changed"]["before"]["ga4"]["metrics"] == {}
    assert "do not apply" in out["filter_application"]["gsc"]


def test_query_is_deterministic_and_does_not_rewrite_ledger(tmp_path):
    args = setup_case(tmp_path)
    ledger = (tmp_path/"batches.sqlite").read_bytes()
    first = analyze(*args, as_of=AS_OF)
    second = analyze(*args, as_of=datetime(2026, 8, 22, tzinfo=timezone.utc))
    assert first["result_digest"] == second["result_digest"]
    assert (tmp_path/"batches.sqlite").read_bytes() == ledger


def test_sample_cannot_be_used_as_real_batch_evidence(tmp_path):
    args = list(setup_case(tmp_path))
    from dataclasses import replace
    args[0] = replace(args[0], draft=replace(args[0].draft, sample=False))
    with pytest.raises(AnalysisError, match="cannot be mixed"):
        analyze(*args, as_of=AS_OF)


def test_rate_aggregation_uses_counts_not_average_of_rates_and_zero_baseline_is_explicit(tmp_path):
    fixture = fixture_data()
    for r in fixture["records"]:
        if r["source"] == "gsc":
            r["evidence"]["metrics"]["ctr"] = 0.9
        if r["source"] == "inquiry" and r["scope"]["start"] == "2026-08-01":
            r["evidence"]["metrics"] = {"storedSubmissions": 0, "quarantinedSubmissions": 0, "nonQuarantinedSubmissions": 0}
    out = run_case(tmp_path, fixture=fixture)
    metrics = out["cohorts"]["changed"]["comparison"]["metrics"]
    assert metrics["gsc"]["ctr"]["current"] == 0.05
    assert metrics["inquiry"]["storedSubmissions"]["delta"] == 7
    assert metrics["inquiry"]["storedSubmissions"]["relative_change"] is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True])
def test_invalid_metric_evidence_is_rejected(tmp_path, bad):
    fixture = fixture_data()
    fixture["records"][0]["evidence"]["metrics"]["sessions"] = bad
    with pytest.raises(AnalysisError):
        run_case(tmp_path, fixture=fixture)
