"""Validated Feishu V3 table definitions and daily-record payloads."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any


TABLE_KEYS = ("overview_daily", "product_daily", "information_daily")
STATUS_LABELS = {"complete": "完整", "preliminary": "初步", "partial": "部分"}
_OPTION_COLORS = (
    "Blue",
    "Green",
    "Purple",
    "Orange",
    "Carmine",
    "Red",
    "Turquoise",
    "Wathet",
    "Yellow",
    "Gray",
)


def load_json_object(path: Path) -> dict[str, Any]:
    """Load a JSON object with a stable, operator-facing error."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"JSON file is unreadable or invalid: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return value


def validate_contract(contract: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Validate the V3 table contract and return entries by logical name."""

    if str(contract.get("version")) != "3":
        raise ValueError("Feishu data contract must use version 3")
    raw_tables = contract.get("tables")
    if not isinstance(raw_tables, list):
        raise ValueError("Feishu data contract must contain tables")
    tables: dict[str, Mapping[str, Any]] = {}
    for raw_table in raw_tables:
        if not isinstance(raw_table, Mapping):
            raise ValueError("Feishu data contract contains an invalid table")
        logical_name = _required_text(raw_table, "logical_name")
        if logical_name in tables:
            raise ValueError(f"duplicate logical table: {logical_name}")
        _required_text(raw_table, "display_name")
        stable_key = _required_text(raw_table, "stable_key")
        fields = raw_table.get("fields")
        if not isinstance(fields, list) or not fields:
            raise ValueError(f"table {logical_name} must define fields")
        names: set[str] = set()
        keys: set[str] = set()
        for field in fields:
            if not isinstance(field, Mapping):
                raise ValueError(f"table {logical_name} contains an invalid field")
            key = _required_text(field, "key")
            name = _required_text(field, "name")
            field_type = _required_text(field, "type")
            if field_type not in {"text", "number", "select", "datetime"}:
                raise ValueError(f"unsupported V3 field type: {field_type}")
            if key in keys or name in names:
                raise ValueError(f"table {logical_name} has duplicate field keys or names")
            keys.add(key)
            names.add(name)
        if stable_key != str(fields[0]["name"]):
            raise ValueError(f"table {logical_name} stable key must be its first field")
        tables[logical_name] = raw_table
    if tuple(tables) != TABLE_KEYS:
        raise ValueError(f"V3 contract tables must be ordered as {TABLE_KEYS}")
    return tables


def validate_backfill(
    document: Mapping[str, Any],
    tables: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Validate an audited V3 backfill before it can become a write payload."""

    if str(document.get("schema_version")) != "3":
        raise ValueError("V3 backfill must use schema version 3")
    reconciliation = document.get("reconciliation")
    if not isinstance(reconciliation, Mapping) or reconciliation.get("status") != "passed":
        raise ValueError("V3 backfill reconciliation has not passed")
    raw_records = document.get("records")
    if not isinstance(raw_records, Mapping) or set(raw_records) != set(TABLE_KEYS):
        raise ValueError("V3 backfill record sets do not match the contract")
    expected_counts = reconciliation.get("record_counts")
    if not isinstance(expected_counts, Mapping):
        raise ValueError("V3 backfill does not contain reconciled record counts")

    result: dict[str, list[dict[str, Any]]] = {}
    for logical_name in TABLE_KEYS:
        records = raw_records.get(logical_name)
        if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
            raise ValueError(f"V3 record set is invalid: {logical_name}")
        expected = expected_counts.get(logical_name)
        if expected != len(records):
            raise ValueError(f"V3 record count mismatch: {logical_name}")
        table = tables[logical_name]
        fields = table["fields"]
        expected_keys = {str(field["key"]) for field in fields}
        stable_field = str(fields[0]["key"])
        stable_values: set[str] = set()
        for row in records:
            if set(row) != expected_keys:
                missing = sorted(expected_keys - set(row))
                extra = sorted(set(row) - expected_keys)
                raise ValueError(
                    f"V3 record fields mismatch in {logical_name}: missing={missing}, extra={extra}"
                )
            stable_value = _record_text(row.get(stable_field), stable_field)
            if stable_value in stable_values:
                raise ValueError(f"duplicate V3 stable key in {logical_name}: {stable_value}")
            stable_values.add(stable_value)
        result[logical_name] = records
    return result


def build_table_fields(
    table: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Translate contract fields into lark-cli table-create definitions."""

    definitions: list[dict[str, Any]] = []
    for field in table["fields"]:
        name = str(field["name"])
        field_type = str(field["type"])
        definition: dict[str, Any] = {"name": name, "type": field_type}
        if field_type == "number":
            definition["style"] = {
                "type": "plain",
                "precision": 0,
                "percentage": False,
                "thousands_separator": True,
            }
        elif field_type == "datetime":
            definition["style"] = {
                "format": "yyyy-MM-dd" if field["key"] == "data_date" else "yyyy-MM-dd HH:mm"
            }
        elif field_type == "select":
            values = sorted(
                {
                    _display_value(str(field["key"]), value)
                    for row in rows
                    if (value := row.get(str(field["key"]))) not in (None, "")
                }
            )
            definition["multiple"] = False
            definition["options"] = [
                {
                    "name": value,
                    "hue": _option_hue(value, index),
                    "lightness": "Light",
                }
                for index, value in enumerate(values)
            ]
        definitions.append(definition)
    return definitions


def build_feishu_rows(
    table: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the column-oriented payload required by record-batch-create."""

    fields = list(table["fields"])
    names = [str(field["name"]) for field in fields]
    output_rows: list[list[Any]] = []
    for row in rows:
        output_rows.append(
            [_cell_value(field, row[str(field["key"])]) for field in fields]
        )
    return {"fields": names, "rows": output_rows}


def feishu_record_to_key(value: Any, field_name: str) -> str:
    """Read a projected Feishu text cell as a stable key."""

    if isinstance(value, str):
        return _record_text(value, field_name)
    if isinstance(value, list) and len(value) == 1:
        item = value[0]
        if isinstance(item, str):
            return _record_text(item, field_name)
        if isinstance(item, Mapping):
            for key in ("text", "name", "value"):
                if isinstance(item.get(key), str):
                    return _record_text(item[key], field_name)
    if isinstance(value, Mapping):
        for key in ("text", "name", "value"):
            if isinstance(value.get(key), str):
                return _record_text(value[key], field_name)
    raise ValueError(f"Feishu stable-key cell has an unexpected shape: {field_name}")


def additive_totals(
    rows_by_table: Mapping[str, Sequence[Mapping[str, Any]]]
) -> dict[str, int]:
    """Calculate the cross-table aggregates needed for read-back verification."""

    overview = rows_by_table["overview_daily"]
    return {
        name: sum(_integer(row.get(name), name) for row in overview)
        for name in (
            "ga4_sessions",
            "ga4_key_events",
            "gsc_clicks",
            "gsc_impressions",
            "stored_submissions",
            "accepted_inquiries",
            "product_page_sessions",
            "information_page_sessions",
            "other_page_sessions",
            "classified_page_sessions",
        )
    }


def _cell_value(field: Mapping[str, Any], value: Any) -> Any:
    key = str(field["key"])
    field_type = str(field["type"])
    if field_type == "number":
        return _integer(value, key)
    if field_type == "datetime":
        if key == "data_date":
            try:
                return datetime.strptime(str(value), "%Y-%m-%d").strftime("%Y-%m-%d 00:00:00")
            except ValueError as error:
                raise ValueError(f"invalid data_date: {value}") from error
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"invalid datetime value for {key}: {value}") from error
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    if value is None:
        return None
    text = str(value)
    if field_type == "select":
        return _display_value(key, text) if text else None
    return text


def _display_value(key: str, value: Any) -> str:
    text = str(value)
    return STATUS_LABELS.get(text, text) if key == "data_status" else text


def _option_hue(value: str, index: int) -> str:
    if value == "完整":
        return "Green"
    if value in {"部分", "初步"}:
        return "Orange"
    return _OPTION_COLORS[index % len(_OPTION_COLORS)]


def _required_text(value: Mapping[str, Any], key: str) -> str:
    return _record_text(value.get(key), key)


def _record_text(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be non-empty text")
    return value.strip()


def _integer(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be numeric")
    if int(value) != value:
        raise ValueError(f"{key} must be an integer")
    return int(value)
