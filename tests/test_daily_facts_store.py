import json
from datetime import date

import pytest

from website_analytics.daily_facts_store import last_day, load_days, merge_recompute_outputs, save_days


def _records(day, sessions=100, lines=("A", "B")):
    return {
        "overview_daily": [{"data_date": day, "ga4_sessions": sessions}],
        "product_funnel_daily": [{"data_date": day, "product_line_id": line} for line in lines],
    }


def test_save_and_load_round_trip_and_replace_a_day(tmp_path):
    first = _records("2026-11-19")
    second = _records("2026-11-20")
    both = {table: first[table] + second[table] for table in first}
    assert save_days(tmp_path, both) == ["2026-11-19", "2026-11-20"]
    save_days(tmp_path, _records("2026-11-20", sessions=7, lines=("A",)))

    stored = load_days(tmp_path)
    assert [row["ga4_sessions"] for row in stored["overview_daily"]] == [100, 7]
    assert len(stored["product_funnel_daily"]) == 3
    assert last_day(stored) == date(2026, 11, 20)
    assert not list(tmp_path.glob(".*.tmp"))


def test_save_rejects_a_day_without_both_record_sets(tmp_path):
    records = _records("2026-11-20")
    records["product_funnel_daily"] = []
    with pytest.raises(ValueError, match="missing a record set"):
        save_days(tmp_path, records)


def test_load_rejects_mislabeled_files(tmp_path):
    save_days(tmp_path, _records("2026-11-20"))
    (tmp_path / "2026-11-20.json").rename(tmp_path / "2026-11-21.json")
    with pytest.raises(ValueError, match="mislabeled"):
        load_days(tmp_path)


def test_merge_recompute_outputs_later_file_wins_per_day(tmp_path):
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    old.write_text(json.dumps({"mismatches": [], "records": {
        "overview_daily": _records("2026-11-19")["overview_daily"] + _records("2026-11-20")["overview_daily"],
        "product_funnel_daily": _records("2026-11-19")["product_funnel_daily"] + _records("2026-11-20")["product_funnel_daily"],
    }}))
    new.write_text(json.dumps({"mismatches": [], "records": _records("2026-11-20", sessions=5)}))

    merged = merge_recompute_outputs([old, new])
    assert [(row["data_date"], row["ga4_sessions"]) for row in merged["overview_daily"]] == [("2026-11-19", 100), ("2026-11-20", 5)]

    new.write_text(json.dumps({"mismatches": [{"data_date": "2026-11-20"}], "records": _records("2026-11-20")}))
    with pytest.raises(ValueError, match="mismatches"):
        merge_recompute_outputs([new])
