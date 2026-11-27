import json
from pathlib import Path
from datetime import date, datetime, timedelta

import pytest

from website_analytics.feishu_period_facts import (
    TABLE_KEYS,
    apply_period_plans,
    contract_tables,
    feishu_rows,
    load_period_target,
    period_contract,
    plan_period_sync,
    table_field_definitions,
    validate_records,
    PeriodTarget,
)
from website_analytics.period_facts import WEEK, MONTH, build_period_facts
from website_analytics.product_mapping import ProductMapping, ReportLine

MAPPING = ProductMapping(
    site_key="genemedi-net",
    version="4",
    report_lines=(ReportLine("A", "Alpha", "X", "P"), ReportLine("B", "Beta", "X", "P"), ReportLine("C", "Gamma", "Y")),
    rules=(),
)
STAMP = datetime(2026, 11, 23, 12, 0, 0)
TARGET = PeriodTarget("base", "bot", {"product_period": "tblP", "site_period": "tblS", "product_change": "tblC"})


def _records(weeks=10):
    days = [date(2025, 7, 14) + timedelta(days=offset) for offset in range(weeks * 7)]
    funnel = [
        {
            "data_date": day.isoformat(),
            "product_line_id": line,
            "mapping_version": "4",
            "gsc_impressions": 100,
            "gsc_clicks": 10,
            "gsc_position_impression_sum": 500,
            "ga4_product_landing_sessions_all_channels": 20,
            "ga4_product_landing_engaged_sessions_all_channels": 10,
            "db_accepted_inquiries": 1,
        }
        for day in days
        for line in ("A", "B", "C")
    ]
    overview = [
        {
            "data_date": day.isoformat(),
            "page_classification_version": "4",
            "ga4_sessions": 100,
            "product_page_sessions": 60,
            "information_page_sessions": 30,
            "other_page_sessions": 10,
            "gsc_clicks": 30,
            "gsc_impressions": 300,
            "accepted_inquiries": 3,
        }
        for day in days
    ]
    build = build_period_facts(
        site="genemedi-net", mapping=MAPPING, funnel_daily=funnel, overview_daily=overview,
        last_final=days[-1] - timedelta(days=1), grains=(WEEK, MONTH), crawler={day: {"A": 0.1} for day in days},
    )
    return build.records


class FakeFeishu:
    """Stores cells as written and reads them back the way Feishu projects them."""

    def __init__(self):
        self.tables = {table_id: {} for table_id in TARGET.tables.values()}
        self.next_id = 0
        self.writes = 0

    def list_records(self, table_id):
        return [(record_id, {name: self._project(value) for name, value in row.items() if value is not None and value is not False})
                for record_id, row in self.tables[table_id].items()]

    @staticmethod
    def _project(value):
        if isinstance(value, str) and len(value) == 19 and value[4] == "-" and value[13] == ":":
            return value.replace(" ", "T") + ".000+08:00"
        return value

    def create_records(self, table_id, records):
        self.writes += 1
        for row in records:
            self.next_id += 1
            self.tables[table_id][f"rec{self.next_id}"] = dict(row)
        return len(records)

    def update_records(self, table_id, updates, *, datetime_fields=()):
        self.writes += 1
        for record_id, row in updates:
            self.tables[table_id][record_id] = dict(row)
        return len(updates)


def test_contract_matches_every_record_key():
    records = _records()
    tables = contract_tables(period_contract())
    validate_records(records, tables)
    for name in TABLE_KEYS:
        contract_keys = {field["key"] for field in tables[name]["fields"]} - {"refreshed_at"}
        assert contract_keys == set(records[name][0])


def test_validate_records_rejects_missing_fields():
    records = _records()
    del records["site_period"][0]["clicks"]
    with pytest.raises(ValueError, match="missing \\['clicks'\\]"):
        validate_records(records, contract_tables(period_contract()))


def test_rows_translate_dates_checkboxes_and_empty_text():
    records = _records()
    table = contract_tables(period_contract())["product_period"]
    row = next(r for r in records["product_period"] if r["scope_id"] == "ALL_PRODUCTS")
    cells = feishu_rows(table, [row], STAMP)[0]

    assert cells["周期开始"] == "2025-07-14 00:00:00"
    assert cells["数据更新时间"] == "2026-11-23 12:00:00"
    assert cells["一级分类"] is None  # empty text-like cell
    assert cells["基准排除"] is False
    assert cells["范围名称"] == "全部产品"


def test_field_definitions_use_percent_for_ratios_and_changes():
    records = _records()
    table = contract_tables(period_contract())["product_period"]
    definitions = {item["name"]: item for item in table_field_definitions(table, records["product_period"])}

    assert definitions["搜索点击率"]["style"]["percentage"] is True
    assert definitions["变化·Google 自然搜索点击"]["style"]["percentage"] is True
    assert definitions["基准·Google 自然搜索点击"]["style"]["precision"] == 1
    assert definitions["Google 自然搜索点击"]["style"]["precision"] == 0
    assert [option["name"] for option in definitions["周期状态"]["options"]] == ["完整", "进行中", "有缺日"]
    assert {"前8周均值", "去年同月"} <= {option["name"] for option in definitions["对照方式"]["options"]}


def test_first_sync_creates_then_reruns_unchanged_then_updates_one_row():
    records = _records()
    contract = period_contract()
    client = FakeFeishu()

    first = apply_period_plans(client, plan_period_sync(client, TARGET, contract, records, refreshed_at=STAMP))
    assert first["product_period"]["create"] == len(records["product_period"])
    assert first["product_change"]["verified"] == len(records["product_change"])

    again = plan_period_sync(client, TARGET, contract, records, refreshed_at=STAMP + timedelta(days=1))
    assert all(plan.counts()["unchanged"] == plan.counts()["desired"] for plan in again)

    records["site_period"][0]["clicks"] = 999
    plans = plan_period_sync(client, TARGET, contract, records, refreshed_at=STAMP)
    counts = {plan.logical_name: plan.counts() for plan in plans}
    assert counts["site_period"]["update"] == 1
    assert counts["product_period"]["update"] == 0
    writes = client.writes
    result = apply_period_plans(client, plans)
    assert client.writes == writes + 1
    assert result["site_period"]["verified"] == len(records["site_period"])


def test_readback_mismatch_fails():
    records = _records()
    client = FakeFeishu()
    plans = plan_period_sync(client, TARGET, period_contract(), records, refreshed_at=STAMP, tables=("site_period",))
    original = client.create_records

    def lossy(table_id, rows):
        rows = [dict(row, **{"Google 自然搜索点击": 0}) for row in rows]
        return original(table_id, rows)

    client.create_records = lossy
    with pytest.raises(AssertionError, match="readback differs"):
        apply_period_plans(client, plans)


def test_duplicate_live_keys_stop_the_plan():
    records = _records()
    client = FakeFeishu()
    client.tables["tblS"] = {"r1": {"全站周期键": "k"}, "r2": {"全站周期键": "k"}}
    with pytest.raises(ValueError, match="duplicate key"):
        plan_period_sync(client, TARGET, period_contract(), records, refreshed_at=STAMP, tables=("site_period",))


def test_load_period_target(tmp_path):
    path = tmp_path / "target.json"
    path.write_text(json.dumps({"version": "1", "base_token": "b", "identity": "bot", "tables": dict(TARGET.tables)}), encoding="utf-8")
    assert load_period_target(path).tables["site_period"] == "tblS"
    path.write_text(json.dumps({"version": "1", "base_token": "b", "identity": "bot", "tables": {"site_period": "tblS"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="tables differ"):
        load_period_target(path)


def test_refresh_period_tables_from_store_is_idempotent(tmp_path):
    from website_analytics.daily_facts_store import save_days
    from website_analytics.period_facts_job import refresh_period_tables

    days = [date(2025, 7, 14) + timedelta(days=offset) for offset in range(70)]
    records = {
        "overview_daily": [
            {"data_date": d.isoformat(), "page_classification_version": "4", "ga4_sessions": 100, "product_page_sessions": 60,
             "information_page_sessions": 30, "other_page_sessions": 10, "gsc_clicks": 30, "gsc_impressions": 300, "accepted_inquiries": 3}
            for d in days
        ],
        "product_funnel_daily": [
            {"data_date": d.isoformat(), "product_line_id": line, "mapping_version": "4", "gsc_impressions": 100, "gsc_clicks": 10,
             "gsc_position_impression_sum": 500, "ga4_product_landing_sessions_all_channels": 20,
             "ga4_product_landing_engaged_sessions_all_channels": 10, "db_accepted_inquiries": 1}
            for d in days for line in ("A", "B", "C")
        ],
    }
    store = tmp_path / "store"
    save_days(store, records)
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"version": "1", "base_token": "b", "identity": "bot", "tables": dict(TARGET.tables)}), encoding="utf-8")
    exclusions = tmp_path / "exclusions.yaml"
    exclusions.write_text("site: genemedi-net\nexclusions: []\n", encoding="utf-8")
    crawler_store = tmp_path / "fingerprints"
    crawler_store.mkdir()
    for d in days:
        (crawler_store / f"{d.isoformat()}.json").write_text(json.dumps({
            "data_date": d.isoformat(), "mapping_version": "4",
            "fingerprints": [["Direct", "SG", "SG", "1280x1200", "Chrome", "Windows", 40, 0]],
            "product": [["A", "Direct", "SG", "SG", "1280x1200", "Chrome", "Windows", 5, 0],
                        ["A", "Organic Search", "DE", "Berlin", "1920x1080", "Chrome", "Windows", 15, 9]]}), encoding="utf-8")
    client = FakeFeishu()
    kwargs = dict(site="genemedi-net", mapping=MAPPING, store=store, exclusions=exclusions, target=target,
                  client_factory=lambda _: client, crawler_store=crawler_store,
                  crawler_rule=Path("config/crawler_rules/genemedi-net.yaml"))

    seen = {}
    first = refresh_period_tables(apply=True, after_write=lambda rows: seen.setdefault("rows", rows) and {"status": "sent"}, **kwargs)
    assert first["last_final"] == "2025-09-21"
    assert first["write_result"]["site_period"]["verified"] > 0
    assert first["alert"] == {"status": "sent"} and seen["rows"]["product_change"]
    week = next(row for row in seen["rows"]["product_period"]
                if row["scope_id"] == "A" and row["grain"] == "周" and row["period"] == "2025-W30")
    assert week["landing_sessions"] == 140 and week["clean_landing_sessions"] == 105
    second = refresh_period_tables(apply=False, after_write=lambda rows: 1 / 0, **kwargs)
    assert all(counts["create"] == counts["update"] == 0 for counts in second["plan"].values())
    assert "alert" not in second  # a dry run never alerts

    def broken(rows):
        raise RuntimeError("bot is not in the group")

    third = refresh_period_tables(apply=True, after_write=broken, **kwargs)
    assert third["alert"] == {"status": "failed", "error": "bot is not in the group"}
