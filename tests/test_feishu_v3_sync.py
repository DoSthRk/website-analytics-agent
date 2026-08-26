import json
from pathlib import Path

import pytest

from website_analytics.feishu_v3_sync import (
    FeishuV3Target,
    load_feishu_v3_target,
    sync_v3_record_sets,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads(
    (PROJECT_ROOT / "config" / "feishu_dashboard" / "v3" / "data_contract.json").read_text(
        encoding="utf-8"
    )
)


class FakeClient:
    def __init__(self, tables):
        self.tables = tables
        self.created = []
        self.updated = []

    def list_records(self, table_id):
        return self.tables.get(table_id, [])

    def create_records(self, table_id, records):
        values = list(records)
        self.created.append((table_id, values))
        rows = self.tables.setdefault(table_id, [])
        rows.extend((f"created-{len(rows) + index}", dict(row)) for index, row in enumerate(values))
        return len(records)

    def update_record(self, table_id, record_id, fields):
        self.updated.append((table_id, record_id, dict(fields)))
        rows = self.tables[table_id]
        self.tables[table_id] = [
            (current_id, dict(fields) if current_id == record_id else row)
            for current_id, row in rows
        ]


def test_incremental_v3_sync_creates_updates_and_ignores_refresh_only_changes() -> None:
    document = _document()
    overview = _display_row("overview_daily", document["records"]["overview_daily"][0])
    product = _display_row("product_daily", document["records"]["product_daily"][0])
    information = _display_row(
        "information_daily", document["records"]["information_daily"][0]
    )
    overview["数据更新时间"] = "2026-08-01 01:00:00"
    product["官网访问次数"] = 999
    client = FakeClient(
        {
            "overview": [("rec-overview", overview)],
            "product": [("rec-product", product)],
            "information": [],
        }
    )
    target = FeishuV3Target(
        "base",
        "user",
        {
            "overview_daily": "overview",
            "product_daily": "product",
            "information_daily": "information",
        },
    )

    result = sync_v3_record_sets(client, target, CONTRACT, document)

    assert result["overview_daily"] == {
        "desired": 1,
        "existing": 1,
        "created": 0,
        "updated": 0,
        "unchanged": 1,
        "verified": 1,
    }
    assert result["product_daily"]["updated"] == 1
    assert result["information_daily"]["created"] == 1
    assert [row[0] for row in client.created] == ["information"]
    assert [(row[0], row[1]) for row in client.updated] == [
        ("product", "rec-product")
    ]


def test_incremental_v3_sync_validates_all_tables_before_writing() -> None:
    document = _document()
    overview = _display_row("overview_daily", document["records"]["overview_daily"][0])
    client = FakeClient(
        {
            "overview": [("one", overview), ("two", overview)],
            "product": [],
            "information": [],
        }
    )
    target = FeishuV3Target(
        "base",
        "user",
        {
            "overview_daily": "overview",
            "product_daily": "product",
            "information_daily": "information",
        },
    )

    with pytest.raises(ValueError, match="duplicate"):
        sync_v3_record_sets(client, target, CONTRACT, document)

    assert client.created == []
    assert client.updated == []


def test_load_v3_target_rejects_unexpected_fields(tmp_path: Path) -> None:
    path = tmp_path / "target.json"
    path.write_text(
        json.dumps(
            {
                "version": "3",
                "base_token": "base",
                "identity": "user",
                "tables": {
                    "overview_daily": "tbl-overview",
                    "product_daily": "tbl-product",
                    "information_daily": "tbl-information",
                },
                "secret": "not-allowed",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unexpected"):
        load_feishu_v3_target(path)


def _document():
    records = {}
    for table in CONTRACT["tables"]:
        row = {}
        for field in table["fields"]:
            key = field["key"]
            if field["type"] == "number":
                row[key] = 1
            elif field["type"] == "datetime":
                row[key] = (
                    "2026-08-22" if key == "data_date" else "2026-08-24T07:03:35Z"
                )
            elif field["type"] == "select":
                row[key] = "complete" if key == "data_status" else f"value-{key}"
            else:
                row[key] = f"value-{key}"
        records[table["logical_name"]] = [row]
    return {
        "schema_version": "3",
        "reconciliation": {
            "status": "passed",
            "record_counts": {name: len(rows) for name, rows in records.items()},
        },
        "records": records,
    }


def _display_row(logical_name, row):
    from website_analytics.feishu_v3 import build_feishu_rows

    table = next(
        table for table in CONTRACT["tables"] if table["logical_name"] == logical_name
    )
    payload = build_feishu_rows(table, [row])
    return dict(zip(payload["fields"], payload["rows"][0], strict=True))
