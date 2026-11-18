"""All inputs are synthetic stand-ins; never load real saved cases in unit tests."""
import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_analysis_continuous import draft_data as continuous_data, packet as weekly_packet
from test_analysis_preview import draft_data as preview_data, fake_packet as window_packet
from website_analytics.analysis.cases import CaseStore, make_case_record, query_case, render_case, MAX_CASE_BYTES
from website_analytics.analysis.continuous import ContinuousDraft
from website_analytics.analysis.contracts import AnalysisError
from website_analytics.analysis.preview import PreviewDraft


def source_runtime():
    return {"execution_model": "existing_server_runtime", "production_release": "synthetic-test-not-production",
            "public_source_sha256": {}, "local_developer_edits_deployed": False}


def inputs(kind="continuous_trend"):
    context = continuous_data()
    draft = context if kind == "continuous_trend" else preview_data()
    parsed = ContinuousDraft.parse(context) if kind == "continuous_trend" else PreviewDraft.parse(draft)
    packet = weekly_packet(parsed) if kind == "continuous_trend" else window_packet(parsed)
    packet["runtime"] = source_runtime()
    packet["query_metadata"] = {"ga4": [], "gsc": []}
    if kind == "approximate_window":
        for scenario in packet["scenarios"]:
            if scenario["offset_days"] != 0:
                for period in scenario["periods"].values():
                    for key in list(period):
                        if key not in {"start", "end", "ga4_submitted", "inquiry"}:
                            del period[key]
    periods = packet["weeks"] if kind == "continuous_trend" else [p for s in packet["scenarios"] for p in s["periods"].values()]
    iq_metrics = {"storedSubmissions": 0, "quarantinedSubmissions": 0, "nonQuarantinedSubmissions": 0}
    for period in periods:
        for value in period.values():
            if isinstance(value, dict) and "status" in value:
                value["fetched_at"] = "2026-10-28T09:00:00Z"
        period["inquiry"]["result"] = {"rows": [], "cohorts": {"submitted": copy.deepcopy(iq_metrics), "lowercase_variant": copy.deepcopy(iq_metrics)},
            "page_rows_truncated": False, "database_total": 4, "database_unattributed_count": 4,
            "foreign_origin_group_count": 0, "database_scope": "configured_database_not_independently_proven_dedicated",
            "date_boundary": "legacy_server_calendar_unverified", "days_with_database_rows": []}
    packet["gsc_page_days"]["fetched_at"] = "2026-10-28T09:00:00Z"
    path = parsed.paths[0]
    if kind == "continuous_trend":
        for period in periods:
            packet["gsc_page_days"]["result"]["rows"].append({"date": period["start"], "page": "https://www.genemedi.net"+path,
                "clicks": 0, "impressions": 5, "position": 10, "ctr": 0})
        packet["gsc_availability"] = {"status": "ok", "fetched_at": "2026-10-28T09:00:00Z", "result": {
            "requested_start": "2026-10-19", "requested_end": "2026-10-27", "first_incomplete_date": "2026-10-27",
            "dates_with_rows": [], "zero_filled": False, "source_scope": "submitted_and_separate_variant_paths_only"}}
        urls = {"observed_at": "2026-10-28T09:00:00Z", "actual_migration_time": "unknown", "observations": [], "third_party_301_proposal_is_not_completion_evidence": True}
    else:
        packet["ga4_page_days"]["fetched_at"] = "2026-10-28T09:00:00Z"
        packet["cms"] = {"status": "ok", "fetched_at": "2026-10-28T09:00:00Z", "result": {
            "route_rows": [], "classifications": [], "classification_version": "3", "product_mapping_version": "3", "current_dimension_not_historical": True}}
        urls = {"checked_at": "2026-10-28T09:00:00Z", "public_url_metadata": [], "raw_html_saved": False}
    def add_transport_caps(value):
        if isinstance(value, dict):
            if {"rows", "truncated"} <= value.keys():
                value["row_cap"] = 50000
            for child in value.values(): add_transport_caps(child)
        elif isinstance(value, list):
            for child in value: add_transport_caps(child)
    add_transport_caps(packet)
    return draft, packet, urls, context


def record(kind="continuous_trend"):
    draft, packet, urls, context = inputs(kind)
    return make_case_record("synthetic-case-test", kind, draft, packet, urls, context, superseded=["a"*64])


def request(operation, **kwargs):
    return {"contract": "batch-service/v1", "operation": operation, **kwargs}


def test_import_two_versions_offline_and_catalog_correction_visible(tmp_path, monkeypatch):
    import socket
    def forbidden(*a, **kw):
        raise AssertionError("offline import must never connect")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    store = CaseStore(tmp_path/"cases.sqlite")
    versions = store.put_many([record(), record("approximate_window")])
    catalog = query_case(store, request("list_cases"))
    assert len(catalog["cases"]) == 1 and len(catalog["cases"][0]["versions"]) == 2
    for evidence_id in versions:
        result = query_case(store, request("case_detail", case_id="synthetic-case-test", evidence_id=evidence_id))
        assert result["evidence_state"] == "provisional" and result["batch_confirmed"] is False and result["sample"] is False
        assert result["interpretation"]["october6_12"] == "already_after_reported_start_not_unoptimized_baseline"
        assert result["interpretation"]["october14_20"] == "completion_and_stability_unconfirmed"
        assert result["interpretation"]["both_windows_definitely_still_modifying"] is False
        assert result["interpretation"]["intervention"]["change"]["end"]["date"] is None
    old = query_case(store, request("case_detail", case_id="synthetic-case-test", evidence_id=versions[1]))
    assert old["report"]["single_event_interpretation_superseded"]


def test_read_repeat_concurrency_and_restart_preserve_evidence_bytes(tmp_path):
    store = CaseStore(tmp_path/"cases.sqlite")
    value = record()
    with ThreadPoolExecutor(max_workers=3) as pool:
        ids = list(pool.map(lambda _: store.put(value), range(3)))
    assert len(set(ids)) == 1
    before = store.path.read_bytes()
    assert store.put(value) == ids[0]
    restarted = CaseStore(store.path)
    assert restarted.versions("synthetic-case-test") == store.versions("synthetic-case-test")
    assert restarted.get("synthetic-case-test", ids[0]) == value
    assert store.path.read_bytes() == before


def test_exact_case_page_keys_and_nonadditive_users_contribution(tmp_path):
    store = CaseStore(tmp_path/"cases.sqlite")
    evidence_id = store.put(record())
    common = {"case_id": "synthetic-case-test", "evidence_id": evidence_id}
    detail = query_case(store, request("case_detail", **common))
    original, variant = detail["pages"]
    assert original["page_key"] != variant["page_key"]
    assert original["exact_path"] == "/i/Mixed-Example"
    assert variant["cohort"] == "separate_variant"
    contribution = query_case(store, request("page_contribution", **common, page_key=original["page_key"]))
    assert contribution["contribution"][0]["session_share_of_submitted_cohort"] == 1
    assert contribution["contribution"][0]["user_counts_non_additive_no_user_share"]
    other = query_case(store, request("page_contribution", **common, page_key=variant["page_key"]))
    assert all(row["session_share_of_submitted_cohort"] is None for row in other["contribution"])


@pytest.mark.parametrize("place", ["root", "row", "inquiry", "runtime", "url", "context"])
def test_unknown_personal_or_secret_fields_rejected_before_any_store_creation(tmp_path, place):
    draft, packet, urls, context = inputs()
    if place == "root": packet["client_secret"] = "do-not-persist"
    elif place == "row": packet["weeks"][0]["ga4_submitted"]["result"][0]["customer_email"] = "private@example.invalid"
    elif place == "inquiry": packet["weeks"][0]["inquiry"]["result"]["contacts"] = [{"name": "private-person"}]
    elif place == "runtime": packet["runtime"]["adc"] = "secret"
    elif place == "url": urls["observations"] = [{"requested_url": "https://www.genemedi.net/i/Mixed-Example?token=secret", "error_category": "HTTPError"}]
    else: context["change"]["user_statement"] = "contact private@example.invalid"
    store = CaseStore(tmp_path/"absent.sqlite")
    with pytest.raises(AnalysisError):
        store.put(make_case_record("synthetic-case-test", "continuous_trend", draft, packet, urls, context))
    assert not store.path.exists()


def test_failed_bundle_or_corrupt_existing_version_never_overwrites(tmp_path):
    store = CaseStore(tmp_path/"cases.sqlite")
    value = record()
    evidence_id = store.put(value)
    before = store.path.read_bytes()
    invalid = record("approximate_window")
    invalid["batch_confirmed"] = True
    with pytest.raises(AnalysisError):
        store.put_many([value, invalid])
    assert store.path.read_bytes() == before
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE case_evidence SET payload='{}' WHERE evidence_id=?", (evidence_id,))
    corrupted = store.path.read_bytes()
    with pytest.raises(AnalysisError): store.get("synthetic-case-test", evidence_id)
    with pytest.raises(AnalysisError): store.put(value)
    assert store.path.read_bytes() == corrupted


@pytest.mark.parametrize("extra", [{"path": "/private"}, {"sql": "SELECT secret"}, {"command": "ssh root"}, {"url": "https://evil.invalid"}, {"source": "fetch"}])
def test_service_contract_rejects_paths_commands_queries_and_source_fetches(tmp_path, extra):
    store = CaseStore(tmp_path/"cases.sqlite")
    store.put(record())
    with pytest.raises(AnalysisError):
        query_case(store, request("list_cases", **extra))


def test_unknown_case_page_wrong_evidence_kind_and_oversize_are_explicit(tmp_path):
    store = CaseStore(tmp_path/"cases.sqlite")
    evidence_id = store.put(record("approximate_window"))
    for req in (request("case_detail", case_id="unknown", evidence_id=evidence_id),
                request("page_contribution", case_id="synthetic-case-test", evidence_id=evidence_id, page_key="0"*64)):
        with pytest.raises(KeyError): query_case(store, req)
    with pytest.raises(AnalysisError):
        query_case(store, request("case_trend", case_id="synthetic-case-test", evidence_id=evidence_id))
    huge = record()
    huge["padding"] = "x"*MAX_CASE_BYTES
    with pytest.raises(AnalysisError): store.put(huge)
    absent = CaseStore(tmp_path/"not-created.sqlite")
    with pytest.raises(AnalysisError): absent.list_cases()
    assert not absent.path.exists()
