"""Create, populate, and verify the standalone Feishu page-product dimension."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from website_analytics.feishu_records import (
    is_retryable_lark_failure,
    lark_cli_environment,
)
from website_analytics.feishu_v3 import (
    build_feishu_rows,
    build_table_fields,
    feishu_record_to_key,
    load_json_object,
)
from website_analytics.page_product_dimension import (
    partition_records,
    validate_contract,
    validate_document,
)


MAX_JSON_ARGUMENT_CHARS = 24_000
MAX_BATCH_ROWS = 100
MAX_TABLE_ROWS = 20_000


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--daily-target", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
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


def _run(command: list[str], *, dry_run: bool = False) -> dict[str, Any]:
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
        failed = completed.returncode != 0 or (not dry_run and result.get("ok") is not True)
        if failed and attempt < 7 and is_retryable_lark_failure(
            code=code,
            message=message,
            raw=raw,
        ):
            time.sleep(min(2 * (attempt + 1), 12))
            continue
        if failed or (dry_run and not isinstance(result.get("api"), list)):
            raise RuntimeError(f"lark-cli failed (code={code}, message={message or 'unknown'})")
        return result
    raise RuntimeError("lark-cli retry limit exceeded")


def _daily_target(path: Path) -> tuple[str, str]:
    target = load_json_object(path)
    base_token = target.get("base_token")
    identity = target.get("identity")
    if not isinstance(base_token, str) or not base_token:
        raise ValueError("daily target is missing base_token")
    if identity not in {"user", "bot"}:
        raise ValueError("daily target identity is invalid")
    return base_token, str(identity)


def _table_id(result: Mapping[str, Any]) -> str:
    data = result.get("data")
    if not isinstance(data, Mapping):
        raise RuntimeError("table create response did not contain data")
    candidates = (
        data.get("table_id"),
        data.get("id"),
        data.get("table", {}).get("table_id") if isinstance(data.get("table"), Mapping) else None,
        data.get("table", {}).get("id") if isinstance(data.get("table"), Mapping) else None,
    )
    value = next((item for item in candidates if isinstance(item, str) and item.startswith("tbl")), None)
    if value is None:
        raise RuntimeError("table create response did not contain a table ID")
    return value


def _field_types(result: Mapping[str, Any]) -> dict[str, str]:
    data = result.get("data")
    if not isinstance(data, Mapping):
        raise RuntimeError("field list response did not contain data")
    fields = data.get("fields") or data.get("items") or data.get("data")
    if not isinstance(fields, list):
        raise RuntimeError("field list response did not contain fields")
    output = {
        str(item["name"]): str(item["type"])
        for item in fields
        if isinstance(item, Mapping) and item.get("name") and item.get("type")
    }
    if len(output) != len(fields):
        raise RuntimeError("field list contains missing or duplicate field names")
    return output


def payload_batches(
    table: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> Iterable[dict[str, Any]]:
    """Keep every serialized Windows command safely below CreateProcess limits."""

    fields = [str(field["name"]) for field in table["fields"]]
    rows: list[list[Any]] = []
    current_size = len(json.dumps({"fields": fields, "rows": []}, ensure_ascii=False))
    for record in records:
        projected = build_feishu_rows(table, [record])["rows"][0]
        projected_size = len(json.dumps(projected, ensure_ascii=False, separators=(",", ":"))) + 1
        if rows and (len(rows) >= MAX_BATCH_ROWS or current_size + projected_size > MAX_JSON_ARGUMENT_CHARS):
            yield {"fields": fields, "rows": rows}
            rows = []
            current_size = len(json.dumps({"fields": fields, "rows": []}, ensure_ascii=False))
        rows.append(projected)
        current_size += projected_size
    if rows:
        yield {"fields": fields, "rows": rows}


def _existing(
    runtime: list[str],
    common: list[str],
    table_id: str,
    stable_name: str,
    hash_name: str,
) -> dict[str, tuple[str, str]]:
    output: dict[str, tuple[str, str]] = {}
    offset = 0
    while True:
        result = _run(
            [
                *runtime,
                "base",
                "+record-list",
                *common,
                "--table-id",
                table_id,
                "--field-id",
                stable_name,
                "--field-id",
                hash_name,
                "--offset",
                str(offset),
                "--limit",
                "200",
            ]
        )
        data = result.get("data")
        if not isinstance(data, Mapping):
            raise RuntimeError("record list response did not contain data")
        fields = data.get("fields")
        rows = data.get("data")
        record_ids = data.get("record_id_list")
        if fields != [stable_name, hash_name] or not isinstance(rows, list) or not isinstance(record_ids, list):
            raise RuntimeError("record list projection differs from the requested fields")
        if len(rows) != len(record_ids):
            raise RuntimeError("record list response has mismatched record IDs")
        for record_id, row in zip(record_ids, rows, strict=True):
            if not isinstance(record_id, str) or not isinstance(row, list) or len(row) != 2:
                raise RuntimeError("record list contains an invalid row")
            key = feishu_record_to_key(row[0], stable_name)
            row_hash = feishu_record_to_key(row[1], hash_name)
            if key in output:
                raise ValueError(f"duplicate Feishu URL key: {key}")
            output[key] = (record_id, row_hash)
        if not data.get("has_more"):
            break
        if not rows:
            raise RuntimeError("record list pagination made no progress")
        offset += len(rows)
    return output


def _sync_partition(
    *,
    runtime: list[str],
    common: list[str],
    contract: Mapping[str, Any],
    partition: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    existing_tables: Mapping[str, str],
    apply: bool,
) -> dict[str, Any]:
    partition_key = str(partition["key"])
    display_name = str(partition["display_name"])
    if len(records) > MAX_TABLE_ROWS:
        raise ValueError(f"page-product partition exceeds Feishu row limit: {partition_key}")
    definitions = build_table_fields(contract, records)
    expected_types = {str(item["name"]): str(item["type"]) for item in definitions}
    table_id = existing_tables.get(display_name)
    created_table = False
    if table_id is None:
        command = [
            *runtime,
            "base",
            "+table-create",
            *common,
            "--name",
            display_name,
            "--fields",
            json.dumps(definitions, ensure_ascii=False, separators=(",", ":")),
        ]
        if not apply:
            command.append("--dry-run")
        result = _run(command, dry_run=not apply)
        if not apply:
            return {
                "partition": partition_key,
                "table": display_name,
                "table_id": None,
                "created_table": True,
                "approved_records": len(records),
                "existing": 0,
                "missing": len(records),
                "changed": 0,
                "extra_preserved": 0,
                "create_operations": sum(1 for _ in payload_batches(contract, records)),
            }
        table_id = _table_id(result)
        created_table = True
        time.sleep(1.0)
    if table_id is None:
        raise AssertionError("page-product table ID was not resolved")
    actual_types = _field_types(
        _run([*runtime, "base", "+field-list", *common, "--table-id", table_id, "--limit", "200"])
    )
    if actual_types != expected_types:
        raise ValueError(f"existing page-product table schema differs: {display_name}")

    stable_key = str(contract["fields"][0]["key"])
    stable_name = str(contract["fields"][0]["name"])
    hash_name = next(
        str(field["name"]) for field in contract["fields"] if field["key"] == "row_hash"
    )
    existing = _existing(runtime, common, table_id, stable_name, hash_name)
    desired_by_key = {str(record[stable_key]): record for record in records}
    missing = [record for key, record in desired_by_key.items() if key not in existing]
    changed = [
        (existing[key][0], record)
        for key, record in desired_by_key.items()
        if key in existing and existing[key][1] != record["row_hash"]
    ]
    extra = sorted(set(existing) - set(desired_by_key))
    if not apply:
        return {
            "partition": partition_key,
            "table": display_name,
            "table_id": table_id,
            "created_table": False,
            "approved_records": len(records),
            "existing": len(existing),
            "missing": len(missing),
            "changed": len(changed),
            "extra_preserved": len(extra),
            "create_operations": sum(1 for _ in payload_batches(contract, missing)),
        }

    created_records = 0
    operations = 0
    for payload in payload_batches(contract, missing):
        result = _run(
            [
                *runtime,
                "base",
                "+record-batch-create",
                *common,
                "--table-id",
                table_id,
                "--json",
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
            ]
        )
        data = result.get("data")
        record_ids = data.get("record_id_list") if isinstance(data, Mapping) else None
        if not isinstance(record_ids, list) or len(record_ids) != len(payload["rows"]):
            raise RuntimeError("Feishu did not confirm every created page-product record")
        created_records += len(record_ids)
        operations += 1
        time.sleep(0.35)

    field_names = [str(field["name"]) for field in contract["fields"]]
    for record_id, record in changed:
        row = build_feishu_rows(contract, [record])["rows"][0]
        patch = dict(zip(field_names, row, strict=True))
        _run(
            [
                *runtime,
                "base",
                "+record-upsert",
                *common,
                "--table-id",
                table_id,
                "--record-id",
                record_id,
                "--json",
                json.dumps(patch, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
            ]
        )
        time.sleep(0.35)

    readback = _existing(runtime, common, table_id, stable_name, hash_name)
    for key, record in desired_by_key.items():
        actual = readback.get(key)
        if actual is None or actual[1] != record["row_hash"]:
            raise AssertionError(f"Feishu readback differs for URL key: {key}")
    return {
        "partition": partition_key,
        "table": display_name,
        "table_id": table_id,
        "created_table": created_table,
        "approved_records": len(records),
        "created_records": created_records,
        "updated_records": len(changed),
        "extra_preserved": len(extra),
        "write_operations": operations + len(changed),
        "readback_records": len(readback),
        "verified_records": len(desired_by_key),
    }


def main() -> int:
    args = _arguments()
    contract = validate_contract(load_json_object(args.contract))
    records = validate_document(load_json_object(args.data), contract)
    records_by_partition = partition_records(contract, records)
    base_token, identity = _daily_target(args.daily_target)
    runtime = _runtime()
    common = ["--as", identity, "--base-token", base_token, "--format", "json"]
    table_list = _run([*runtime, "base", "+table-list", *common])
    existing_tables = {
        str(item["name"]): str(item["id"])
        for item in table_list.get("data", {}).get("tables", [])
        if isinstance(item, Mapping) and item.get("name") and item.get("id")
    }
    summaries = [
        _sync_partition(
            runtime=runtime,
            common=common,
            contract=contract,
            partition=partition,
            records=records_by_partition[str(partition["key"])],
            existing_tables=existing_tables,
            apply=args.apply,
        )
        for partition in contract["partitions"]
    ]
    if args.apply:
        target = {
            "version": "1",
            "base_token": base_token,
            "identity": identity,
            "tables": {
                str(summary["partition"]): str(summary["table_id"])
                for summary in summaries
            },
        }
        args.target.parent.mkdir(parents=True, exist_ok=True)
        args.target.write_text(
            json.dumps(target, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "status": "ok",
                "mode": "apply" if args.apply else "dry-run",
                "approved_records": len(records),
                "partitions": summaries,
                "target_written": str(args.target) if args.apply else None,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
