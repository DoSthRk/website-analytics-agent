"""Read every page-product cell back from Feishu and compare it to the approved artifact."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from website_analytics.feishu_records import (
    is_retryable_lark_failure,
    lark_cli_environment,
)
from website_analytics.feishu_v3 import build_feishu_rows, load_json_object
from website_analytics.page_product_dimension import (
    partition_records,
    validate_contract,
    validate_document,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
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
    for attempt in range(8):
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
            if attempt < 7 and is_retryable_lark_failure(code=None, message=None, raw=raw):
                time.sleep(min(2 * (attempt + 1), 12))
                continue
            raise RuntimeError("lark-cli returned invalid JSON") from error
        error_data = result.get("error") if isinstance(result, dict) else None
        code = error_data.get("code") if isinstance(error_data, dict) else None
        message = error_data.get("message") if isinstance(error_data, dict) else None
        failed = completed.returncode != 0 or result.get("ok") is not True
        if failed and attempt < 7 and is_retryable_lark_failure(
            code=code,
            message=message,
            raw=raw,
        ):
            time.sleep(min(2 * (attempt + 1), 12))
            continue
        if failed:
            raise RuntimeError(f"lark-cli failed (code={code}, message={message or 'unknown'})")
        return result
    raise RuntimeError("lark-cli retry limit exceeded")


def _target(path: Path, partition_keys: set[str]) -> tuple[str, str, dict[str, str]]:
    target = load_json_object(path)
    tables = target.get("tables")
    if str(target.get("version")) != "1" or not isinstance(tables, dict):
        raise ValueError("invalid page-product mapping target")
    if set(tables) != partition_keys:
        raise ValueError("page-product target partitions differ from the contract")
    base_token = target.get("base_token")
    identity = target.get("identity")
    if not isinstance(base_token, str) or not base_token:
        raise ValueError("page-product target is missing base_token")
    if identity not in {"user", "bot"}:
        raise ValueError("page-product target identity is invalid")
    return base_token, str(identity), {str(key): str(value) for key, value in tables.items()}


def _read_table(
    runtime: list[str],
    common: list[str],
    table_id: str,
    field_names: list[str],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    offset = 0
    while True:
        command = [*runtime, "base", "+record-list", *common, "--table-id", table_id]
        for name in field_names:
            command.extend(("--field-id", name))
        command.extend(("--offset", str(offset), "--limit", "200"))
        result = _run(command)
        data = result.get("data")
        if not isinstance(data, Mapping):
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
        if not values:
            raise RuntimeError("record list pagination made no progress")
        offset += len(values)
    return output


def normalize_cell(value: Any, field_type: str) -> Any:
    """Normalize the shapes returned by lark-cli to the submitted cell values."""

    if field_type == "select":
        if value is None or value == []:
            return None
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
            return value[0]
        if isinstance(value, str):
            return value
        raise AssertionError("unexpected single-select value")
    if field_type == "datetime":
        if not isinstance(value, str):
            raise AssertionError("unexpected datetime value")
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    if field_type in {"text", "url"} and value is None:
        return ""
    return value


def main() -> int:
    args = _arguments()
    contract = validate_contract(load_json_object(args.contract))
    records = validate_document(load_json_object(args.data), contract)
    records_by_partition = partition_records(contract, records)
    partition_keys = {str(partition["key"]) for partition in contract["partitions"]}
    base_token, identity, target_tables = _target(args.target, partition_keys)
    runtime = _runtime()
    common = ["--as", identity, "--base-token", base_token, "--format", "json"]
    definitions = list(contract["fields"])
    field_names = [str(field["name"]) for field in definitions]
    field_types = {str(field["name"]): str(field["type"]) for field in definitions}
    stable_name = field_names[0]
    results: dict[str, Any] = {}
    all_actual_keys: set[str] = set()
    actual_page_classes: Counter[str] = Counter()
    actual_categories_l1: Counter[str] = Counter()

    for partition in contract["partitions"]:
        key = str(partition["key"])
        table_id = target_tables[key]
        readback = _read_table(runtime, common, table_id, field_names)
        expected_payload = build_feishu_rows(contract, records_by_partition[key])
        expected = [dict(zip(field_names, row, strict=True)) for row in expected_payload["rows"]]
        expected_by_key = {str(row[stable_name]): row for row in expected}
        actual_by_key: dict[str, dict[str, Any]] = {}
        for row in readback:
            stable_value = normalize_cell(row[stable_name], field_types[stable_name])
            if not isinstance(stable_value, str) or not stable_value:
                raise AssertionError(f"invalid Feishu stable key in partition: {key}")
            if stable_value in actual_by_key or stable_value in all_actual_keys:
                raise AssertionError(f"duplicate Feishu stable key in partition: {key}")
            actual_by_key[stable_value] = row
            all_actual_keys.add(stable_value)
        if set(actual_by_key) != set(expected_by_key):
            raise AssertionError(f"Feishu stable keys differ from approved artifact: {key}")
        for stable_value, expected_row in expected_by_key.items():
            actual_row = actual_by_key[stable_value]
            for name in field_names:
                if normalize_cell(actual_row[name], field_types[name]) != expected_row[name]:
                    raise AssertionError(
                        f"Feishu cell differs from approved artifact: {key} {stable_value} {name}"
                    )
            page_class_name = next(
                str(field["name"]) for field in definitions if field["key"] == "page_class"
            )
            category_l1_name = next(
                str(field["name"]) for field in definitions if field["key"] == "category_l1"
            )
            actual_page_classes[str(normalize_cell(actual_row[page_class_name], "select"))] += 1
            category_l1 = normalize_cell(actual_row[category_l1_name], "select")
            if category_l1:
                actual_categories_l1[str(category_l1)] += 1
        results[key] = {"table_id": table_id, "record_count": len(readback), "status": "passed"}

    if len(all_actual_keys) != len(records):
        raise AssertionError("Feishu URL mapping total differs from the approved artifact")
    expected_page_classes = Counter(str(row["page_class"]) for row in records)
    expected_categories_l1 = Counter(str(row["category_l1"]) for row in records if row["category_l1"])
    if actual_page_classes != expected_page_classes:
        raise AssertionError("Feishu page-class totals differ from the approved artifact")
    if actual_categories_l1 != expected_categories_l1:
        raise AssertionError("Feishu product-category totals differ from the approved artifact")

    print(
        json.dumps(
            {
                "status": "ok",
                "verified_records": len(all_actual_keys),
                "partitions": results,
                "page_classes": dict(sorted(actual_page_classes.items())),
                "product_categories_l1": dict(sorted(actual_categories_l1.items())),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
