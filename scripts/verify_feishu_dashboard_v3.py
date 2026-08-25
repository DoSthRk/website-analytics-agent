"""Read V3 Feishu rows back and reconcile them with the approved backfill."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from website_analytics.feishu_records import is_retryable_lark_failure, lark_cli_environment
from website_analytics.feishu_v3 import (
    TABLE_KEYS,
    additive_totals,
    build_feishu_rows,
    load_json_object,
    validate_backfill,
    validate_contract,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--backfill", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--protected-table", action="append", default=[])
    return parser.parse_args()


def _runtime() -> list[str]:
    wrapper = shutil.which("lark-cli")
    node = shutil.which("node")
    if wrapper is None or node is None:
        raise RuntimeError("lark-cli is unavailable")
    script = Path(wrapper).parent / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js"
    if not script.is_file():
        raise RuntimeError("lark-cli runtime script is unavailable")
    return [node, str(script)]


def _run(command: list[str]) -> dict[str, Any]:
    environment = lark_cli_environment()
    for attempt in range(6):
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            check=False,
        )
        raw = completed.stdout or completed.stderr
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as error:
            if attempt < 5 and is_retryable_lark_failure(code=None, message=None, raw=raw):
                time.sleep(min(3 * (attempt + 1), 15))
                continue
            raise RuntimeError(raw.strip()) from error
        error_data = result.get("error") if isinstance(result, dict) else None
        code = error_data.get("code") if isinstance(error_data, dict) else None
        message = error_data.get("message") if isinstance(error_data, dict) else None
        failed = completed.returncode != 0 or result.get("ok") is not True
        if failed and attempt < 5 and is_retryable_lark_failure(code=code, message=message, raw=raw):
            time.sleep(min(3 * (attempt + 1), 15))
            continue
        if failed:
            raise RuntimeError(json.dumps(result, ensure_ascii=False))
        return result
    raise RuntimeError("lark-cli retry limit exceeded")


def _target(path: Path) -> tuple[str, str, dict[str, str]]:
    target = load_json_object(path)
    tables = target.get("tables")
    if str(target.get("version")) != "3" or not isinstance(tables, dict):
        raise ValueError("invalid V3 sync target")
    if set(tables) != set(TABLE_KEYS):
        raise ValueError("V3 sync target tables do not match the contract")
    return str(target["base_token"]), str(target["identity"]), {
        str(key): str(value) for key, value in tables.items()
    }


def _read_table(
    runtime: list[str],
    common: list[str],
    table_id: str,
    field_names: list[str],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    offset = 0
    while True:
        command = [
            *runtime,
            "base",
            "+record-list",
            *common,
            "--table-id",
            table_id,
        ]
        for name in field_names:
            command.extend(("--field-id", name))
        command.extend(("--offset", str(offset), "--limit", "200"))
        result = _run(command)
        data = result.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("record list response did not contain data")
        actual_fields = data.get("fields")
        values = data.get("data")
        if actual_fields != field_names or not isinstance(values, list):
            raise RuntimeError("record list projection differs from the requested fields")
        for row in values:
            if not isinstance(row, list) or len(row) != len(field_names):
                raise RuntimeError("record list contains a malformed row")
            output.append(dict(zip(field_names, row, strict=True)))
        if not data.get("has_more"):
            break
        offset += len(values)
    return output


def _normal(value: Any, field_type: str) -> Any:
    if field_type == "select":
        if value is None:
            return None
        if isinstance(value, list) and len(value) == 1:
            return value[0]
        raise AssertionError(f"unexpected single-select value: {value!r}")
    if field_type == "datetime":
        if not isinstance(value, str):
            raise AssertionError(f"unexpected datetime value: {value!r}")
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    return value


def _protected(value: str) -> tuple[str, int]:
    table_id, separator, count = value.partition("=")
    if not separator or not table_id.startswith("tbl"):
        raise ValueError("--protected-table must use tbl_id=record_count")
    return table_id, int(count)


def main() -> int:
    args = _arguments()
    contract = load_json_object(args.contract)
    tables = validate_contract(contract)
    backfill = load_json_object(args.backfill)
    expected_records = validate_backfill(backfill, tables)
    base_token, identity, target_tables = _target(args.target)
    runtime = _runtime()
    common = ["--as", identity, "--base-token", base_token, "--format", "json"]
    readback_by_table: dict[str, list[dict[str, Any]]] = {}

    for logical_name in TABLE_KEYS:
        table = tables[logical_name]
        definitions = list(table["fields"])
        field_names = [str(field["name"]) for field in definitions]
        field_types = {str(field["name"]): str(field["type"]) for field in definitions}
        readback = _read_table(runtime, common, target_tables[logical_name], field_names)
        expected_payload = build_feishu_rows(table, expected_records[logical_name])
        expected = [dict(zip(field_names, row, strict=True)) for row in expected_payload["rows"]]
        stable_name = field_names[0]
        expected_by_key = {str(row[stable_name]): row for row in expected}
        actual_by_key: dict[str, dict[str, Any]] = {}
        for row in readback:
            key = str(row[stable_name])
            if key in actual_by_key:
                raise AssertionError(f"duplicate Feishu stable key: {logical_name} {key}")
            actual_by_key[key] = row
        if set(actual_by_key) != set(expected_by_key):
            raise AssertionError(f"Feishu stable keys differ from backfill: {logical_name}")
        for key, expected_row in expected_by_key.items():
            actual_row = actual_by_key[key]
            for name in field_names:
                if _normal(actual_row[name], field_types[name]) != expected_row[name]:
                    raise AssertionError(
                        f"Feishu cell differs from backfill: {logical_name} {key} {name}"
                    )
        key_by_contract = {str(field["name"]): str(field["key"]) for field in definitions}
        readback_by_table[logical_name] = [
            {key_by_contract[name]: _normal(row[name], field_types[name]) for name in field_names}
            for row in readback
        ]

    expected_totals = backfill["reconciliation"]["additive_totals"]
    actual_totals = additive_totals(readback_by_table)
    if actual_totals != expected_totals:
        raise AssertionError(("additive totals differ", expected_totals, actual_totals))

    table_list = _run([*runtime, "base", "+table-list", *common])
    actual_counts = {
        str(item["id"]): int(item["records_count"])
        for item in table_list.get("data", {}).get("tables", [])
        if isinstance(item, dict) and item.get("id") and item.get("records_count") is not None
    }
    for table_id, count in map(_protected, args.protected_table):
        if actual_counts.get(table_id) != count:
            raise AssertionError(f"protected V2 table count changed: {table_id}")

    print(
        json.dumps(
            {
                "status": "ok",
                "record_counts": {
                    logical_name: len(rows) for logical_name, rows in readback_by_table.items()
                },
                "additive_totals": actual_totals,
                "protected_tables": len(args.protected_table),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
