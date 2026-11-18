import copy
import json
from pathlib import Path

import pytest

from website_analytics.analysis.contracts import AnalysisError, BatchDraft, Query, Selection, page_path

EXAMPLES = Path(__file__).resolve().parents[1]/"examples/batch-analysis"


def draft_data():
    return json.loads((EXAMPLES/"draft.json").read_text())


@pytest.mark.parametrize("url", ["https://tarmart.com/i/x", "https://genemedi.net.evil.test/i/x", "https://name:secret@genemedi.net/i/x", "//genemedi.net/i/x", "/i/x?email=secret", "/i/x#fragment", "/i/%2e%2e/x", "/i/%3fsecret", "/i/%5cfoo", "/i/%00foo", "https://www.genemedi.net:8443/i/x", "file:///x", "i/x"])
def test_rejects_unsafe_or_foreign_page(url):
    with pytest.raises(AnalysisError):
        page_path(url)


def test_uses_existing_path_identity_without_leaking_query():
    assert page_path("https://www.genemedi.net/i/AAV/") == "/i/aav"
    value = draft_data()
    value["pages"] += ["https://www.genemedi.net/i/AAV-example/"]
    assert BatchDraft.parse(value).pages == ("/i/aav-example", "/i/unmapped-example")


@pytest.mark.parametrize("field,value", [("site", "tarmart"), ("contract", "batch-analysis/v2"), ("revision", True), ("revision", 2), ("sample", "true"), ("published_at", "2026-08-08T10:00:00"), ("published_at", "2026-08-08T10:00:00-08:00"), ("published_timezone", "Not/AZone"), ("pages", []), ("control_pages", ["/i/aav-example"]), ("control_rationale", None)])
def test_rejects_invalid_draft(field, value):
    data = draft_data()
    data[field] = value
    with pytest.raises(AnalysisError):
        BatchDraft.parse(data)


def test_rejects_unrecognized_request_fields_and_unsaved_revision():
    batch = BatchDraft.parse(draft_data())
    req = {"contract": "batch-analysis/v1", "site": "genemedi-net", "batch_id": batch.batch_id, "revision": 1}
    with pytest.raises(AnalysisError):
        Query.parse({**req, "sql": "arbitrary"}, batch)
    with pytest.raises(AnalysisError):
        Query.parse({**req, "revision": 2}, batch)
    with pytest.raises(AnalysisError):
        Query.parse({**req, "selection": {"pages": ["/outside-batch"]}}, batch)
    with pytest.raises(AnalysisError):
        Query.parse({**req, "window_days": True}, batch)


def test_default_periods_exclude_actual_launch_day_and_respect_timezone():
    batch = BatchDraft.parse(draft_data())
    req = {"contract": "batch-analysis/v1", "site": "genemedi-net", "batch_id": batch.batch_id, "revision": 1}
    query = Query.parse(req, batch)
    assert query.before.start.isoformat() == "2026-07-11"
    assert query.before.end.isoformat() == "2026-08-07"
    assert query.after.start.isoformat() == "2026-08-09"
    assert query.after.end.isoformat() == "2026-09-05"
    assert not query.selection.countries and not query.selection.channels
    explicit = {**req, "before": {"start": "2026-08-01", "end": "2026-08-07"}, "after": {"start": "2026-08-09", "end": "2026-08-15"}}
    assert Query.parse(explicit, batch).before.end == query.before.end
    for bad in ({**explicit, "window_days": 7}, {**explicit, "after": {"start": "2026-08-08", "end": "2026-08-14"}}, {**explicit, "after": {"start": "2026-08-09", "end": "2026-08-16"}}):
        with pytest.raises(AnalysisError):
            Query.parse(bad, batch)


def test_explicit_optional_segments_are_bounded_and_have_no_defaults():
    assert Selection.parse({}).document() == {"countries": [], "channels": [], "pages": []}
    assert Selection.parse({"countries": ["JP", "JP"], "channels": ["Organic Search"]}).countries == ("JP",)
    for invalid in ({"countries": ["Japan"]}, {"countries": "JP"}, {"channels": [1]}, {"channels": ["invented channel"]}, {"devices": []}):
        with pytest.raises(AnalysisError):
            Selection.parse(invalid)
