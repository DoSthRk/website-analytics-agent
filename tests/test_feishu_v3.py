from website_analytics.feishu_v3 import (
    build_feishu_rows,
    build_table_fields,
    validate_backfill,
    validate_contract,
)


def _contract():
    return {
        "version": "3",
        "tables": [
            {
                "logical_name": logical_name,
                "display_name": display_name,
                "stable_key": stable_name,
                "fields": [
                    {"key": stable_key, "name": stable_name, "type": "text"},
                    {"key": "data_date", "name": "数据日期", "type": "datetime"},
                    {"key": "data_status", "name": "数据状态", "type": "select"},
                    {"key": "metric", "name": "指标", "type": "number"},
                    {"key": "refreshed_at", "name": "数据更新时间", "type": "datetime"},
                ],
            }
            for logical_name, display_name, stable_key, stable_name in (
                ("overview_daily", "全站每日数据", "daily_key", "每日键"),
                ("product_daily", "产品每日数据", "product_daily_key", "产品每日键"),
                ("information_daily", "信息页每日数据", "information_daily_key", "信息每日键"),
            )
        ],
    }


def _backfill():
    records = {}
    key_fields = {
        "overview_daily": "daily_key",
        "product_daily": "product_daily_key",
        "information_daily": "information_daily_key",
    }
    for table, key in key_fields.items():
        records[table] = [
            {
                key: f"genemedi-net|2026-08-21|{table}",
                "data_date": "2026-08-21",
                "data_status": "complete",
                "metric": 12,
                "refreshed_at": "2026-08-24T08:59:58+00:00",
            }
        ]
    return {
        "schema_version": "3",
        "records": records,
        "reconciliation": {
            "status": "passed",
            "record_counts": {table: 1 for table in records},
        },
    }


def test_v3_feishu_schema_and_rows_use_operator_friendly_types() -> None:
    tables = validate_contract(_contract())
    records = validate_backfill(_backfill(), tables)
    table = tables["overview_daily"]

    fields = build_table_fields(table, records["overview_daily"])
    payload = build_feishu_rows(table, records["overview_daily"])

    assert fields[0] == {"name": "每日键", "type": "text"}
    assert fields[1]["style"]["format"] == "yyyy-MM-dd"
    assert fields[2]["options"] == [
        {"name": "完整", "hue": "Green", "lightness": "Light"}
    ]
    assert fields[3]["style"]["thousands_separator"] is True
    assert payload["fields"] == ["每日键", "数据日期", "数据状态", "指标", "数据更新时间"]
    assert payload["rows"] == [
        [
            "genemedi-net|2026-08-21|overview_daily",
            "2026-08-21 00:00:00",
            "完整",
            12,
            "2026-08-24 08:59:58",
        ]
    ]


def test_v3_backfill_rejects_duplicate_stable_keys() -> None:
    tables = validate_contract(_contract())
    document = _backfill()
    document["records"]["overview_daily"].append(
        dict(document["records"]["overview_daily"][0])
    )
    document["reconciliation"]["record_counts"]["overview_daily"] = 2

    try:
        validate_backfill(document, tables)
    except ValueError as error:
        assert "duplicate V3 stable key" in str(error)
    else:
        raise AssertionError("duplicate stable key was accepted")
