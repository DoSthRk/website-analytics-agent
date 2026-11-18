"""Validate and translate product-funnel facts for Feishu Base."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from website_analytics.product_funnel_backfill import (
    ADDITIVE_METRICS,
    TABLE_KEYS,
)


STATUS_LABELS = {"observation_complete": "观察完成"}
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
_SUPPORTED_TYPES = {"text", "number", "select", "datetime", "checkbox"}


def load_json_object(path: Path) -> dict[str, Any]:
    """Load one local JSON object with a stable error."""
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"JSON file is unreadable or invalid: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return value


def validate_contract(
    contract: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    """Validate the publishable part of the product-funnel contract."""
    if str(contract.get("version")) != "1":
        raise ValueError("product funnel contract must use version 1")
    raw_tables = contract.get("tables")
    if not isinstance(raw_tables, list):
        raise ValueError("product funnel contract must contain tables")
    all_tables: dict[str, Mapping[str, Any]] = {}
    for table in raw_tables:
        if not isinstance(table, Mapping):
            raise ValueError("product funnel contract contains an invalid table")
        logical_name = _text(table.get("logical_name"), "logical_name")
        if logical_name in all_tables:
            raise ValueError(f"duplicate logical table: {logical_name}")
        _text(table.get("display_name"), "display_name")
        stable_name = _text(table.get("stable_key"), "stable_key")
        fields = table.get("fields")
        if not isinstance(fields, list) or not fields:
            raise ValueError(f"table {logical_name} must define fields")
        names: set[str] = set()
        keys: set[str] = set()
        for field in fields:
            if not isinstance(field, Mapping):
                raise ValueError(f"table {logical_name} contains an invalid field")
            key = _text(field.get("key"), "field key")
            name = _text(field.get("name"), "field name")
            field_type = _text(field.get("type"), "field type")
            if field_type not in _SUPPORTED_TYPES:
                raise ValueError(f"unsupported product funnel field type: {field_type}")
            if key in keys or name in names:
                raise ValueError(f"table {logical_name} has duplicate fields")
            keys.add(key)
            names.add(name)
        if stable_name != str(fields[0]["name"]):
            raise ValueError(f"table {logical_name} stable key must be first")
        all_tables[logical_name] = table
    if not set(TABLE_KEYS) <= set(all_tables):
        raise ValueError("product funnel contract is missing publishable tables")
    return {key: all_tables[key] for key in TABLE_KEYS}


def validate_backfill(
    document: Mapping[str, Any],
    tables: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Validate a reconciled product-funnel backfill before publication."""
    if str(document.get("schema_version")) != "1":
        raise ValueError("product funnel backfill must use schema version 1")
    if document.get("write_enabled") is not False:
        raise ValueError("product funnel backfill must remain write-disabled")
    reconciliation = document.get("reconciliation")
    if not isinstance(reconciliation, Mapping) or reconciliation.get("status") != "passed":
        raise ValueError("product funnel backfill reconciliation has not passed")
    raw_records = document.get("records")
    if not isinstance(raw_records, Mapping) or set(raw_records) != set(TABLE_KEYS):
        raise ValueError("product funnel record sets do not match publishable tables")
    expected_counts = reconciliation.get("record_counts")
    if not isinstance(expected_counts, Mapping):
        raise ValueError("product funnel backfill record counts are unavailable")
    result: dict[str, list[dict[str, Any]]] = {}
    for logical_name in TABLE_KEYS:
        records = raw_records.get(logical_name)
        if not isinstance(records, list) or not all(
            isinstance(row, dict) for row in records
        ):
            raise ValueError(f"product funnel records are invalid: {logical_name}")
        if expected_counts.get(logical_name) != len(records):
            raise ValueError(f"product funnel record count differs: {logical_name}")
        fields = list(tables[logical_name]["fields"])
        expected_keys = {str(field["key"]) for field in fields}
        stable_key = str(fields[0]["key"])
        stable_values: set[str] = set()
        for row in records:
            if set(row) != expected_keys:
                raise ValueError(f"product funnel fields differ: {logical_name}")
            value = _text(row.get(stable_key), stable_key)
            if value in stable_values:
                raise ValueError(f"duplicate product funnel key: {value}")
            stable_values.add(value)
            _validate_row_types(row, fields)
        result[logical_name] = records
    if additive_totals(result) != reconciliation.get("additive_totals"):
        raise ValueError("product funnel additive totals differ from reconciliation")
    return result


def build_table_fields(
    table: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Translate physical contract fields into lark-cli definitions."""
    definitions: list[dict[str, Any]] = []
    for field in table["fields"]:
        key = str(field["key"])
        name = str(field["name"])
        field_type = str(field["type"])
        definition: dict[str, Any] = {"name": name, "type": field_type}
        if field_type == "number":
            definition["style"] = _number_style(key)
        elif field_type == "datetime":
            definition["style"] = {
                "format": "yyyy-MM-dd" if key == "data_date" else "yyyy-MM-dd HH:mm"
            }
        elif field_type == "select":
            values = sorted(
                {
                    _select_value(key, value)
                    for row in rows
                    if (value := row.get(key)) not in (None, "")
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
    """Build the column-oriented record-batch-create body."""
    fields = list(table["fields"])
    return {
        "fields": [str(field["name"]) for field in fields],
        "rows": [
            [_cell_value(field, row[str(field["key"])]) for field in fields]
            for row in rows
        ],
    }


def additive_totals(
    rows_by_table: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, float | int]:
    """Recalculate wide-table totals for write and read-back verification."""
    rows = rows_by_table["product_funnel_daily"]
    totals: dict[str, float | int] = {}
    for metric in ADDITIVE_METRICS:
        total = sum(_number(row.get(metric), metric) for row in rows)
        totals[metric] = int(total) if total.is_integer() else total
    return totals


def normalize_readback(value: Any, field: Mapping[str, Any]) -> Any:
    """Normalize one Feishu cell into the local serialized representation."""
    key = str(field["key"])
    field_type = str(field["type"])
    if value is None:
        return None
    if field_type == "select":
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        if not isinstance(value, str):
            raise ValueError(f"unexpected Feishu select value: {key}")
        return value
    if field_type == "datetime":
        if not isinstance(value, str):
            raise ValueError(f"unexpected Feishu datetime value: {key}")
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    if field_type == "checkbox":
        if not isinstance(value, bool):
            raise ValueError(f"unexpected Feishu checkbox value: {key}")
        return value
    return value


def expected_readback_value(field: Mapping[str, Any], value: Any) -> Any:
    """Return the local value expected after a Feishu cell round trip."""
    if value == "" and str(field["type"]) in {"text", "select"}:
        # Feishu stores an empty text-like cell as an absent cell and returns null.
        return None
    return _cell_value(field, value)


def readback_values_equal(expected: Any, actual: Any) -> bool:
    """Compare API-round-tripped values without rejecting harmless float noise."""
    numeric = (int, float)
    if (
        isinstance(expected, numeric)
        and not isinstance(expected, bool)
        and isinstance(actual, numeric)
        and not isinstance(actual, bool)
    ):
        return math.isclose(
            float(expected), float(actual), rel_tol=1e-12, abs_tol=1e-12
        )
    return expected == actual


def _validate_row_types(
    row: Mapping[str, Any], fields: Sequence[Mapping[str, Any]]
) -> None:
    for field in fields:
        key = str(field["key"])
        required = field.get("required") is True
        value = row.get(key)
        if value is None:
            if required:
                raise ValueError(f"required product funnel field is null: {key}")
            continue
        field_type = str(field["type"])
        if field_type == "number":
            _number(value, key)
        elif field_type == "checkbox" and not isinstance(value, bool):
            raise ValueError(f"product funnel checkbox is invalid: {key}")
        elif field_type in {"text", "select", "datetime"} and not isinstance(
            value, str
        ):
            raise ValueError(f"product funnel text-like field is invalid: {key}")


def _cell_value(field: Mapping[str, Any], value: Any) -> Any:
    key = str(field["key"])
    field_type = str(field["type"])
    if value is None:
        return None
    if field_type == "number":
        number = _number(value, key)
        return int(number) if number.is_integer() else number
    if field_type == "datetime":
        if key == "data_date":
            return datetime.strptime(str(value), "%Y-%m-%d").strftime(
                "%Y-%m-%d 00:00:00"
            )
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    if field_type == "checkbox":
        if not isinstance(value, bool):
            raise ValueError(f"checkbox value is invalid: {key}")
        return value
    if field_type == "select":
        return _select_value(key, value)
    return str(value)


def _number_style(key: str) -> dict[str, Any]:
    if key == "rate_from_previous":
        return {
            "type": "plain",
            "precision": 2,
            "percentage": True,
            "thousands_separator": False,
        }
    return {
        "type": "plain",
        "precision": 2 if key == "gsc_position_impression_sum" else 0,
        "percentage": False,
        "thousands_separator": True,
    }


def _select_value(key: str, value: Any) -> str:
    text = str(value)
    return STATUS_LABELS.get(text, text) if key == "data_status" else text


def _option_hue(value: str, index: int) -> str:
    if value == "观察完成":
        return "Green"
    return _OPTION_COLORS[index % len(_OPTION_COLORS)]


def _number(value: object, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{key} must be a finite non-negative number")
    return number


def _text(value: object, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be non-empty text")
    return value.strip()
