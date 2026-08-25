"""Create missing V3 daily records in restart-safe batches."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from website_analytics.feishu_records import is_retryable_lark_failure, lark_cli_environment
from website_analytics.feishu_v3 import (
    TABLE_KEYS,
    build_feishu_rows,
    feishu_record_to_key,
    load_json_object,
    validate_backfill,
    validate_contract,
)


# Keep the serialized --json argument below the Windows CreateProcess limit.
# The Feishu API permits 200, but these wide daily rows can exceed 32 KiB.
WRITE_BATCH_SIZE = 100


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--backfill", type=Path, required=True)
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
        failed = completed.returncode != 0 or (not dry_run and result.get("ok") is not True)
        if failed and attempt < 5 and is_retryable_lark_failure(code=code, message=message, raw=raw):
            time.sleep(min(3 * (attempt + 1), 15))
            continue
        if failed or (dry_run and not isinstance(result.get("api"), list)):
            raise RuntimeError(json.dumps(result, ensure_ascii=False))
        return result
    raise RuntimeError("lark-cli retry limit exceeded")


def _target(path: Path) -> tuple[str, str, dict[str, str]]:
    target = load_json_object(path)
    if str(target.get("version")) != "3" or target.get("identity") not in {"user", "bot"}:
        raise ValueError("invalid V3 sync target")
    tables = target.get("tables")
    if not isinstance(tables, dict) or set(tables) != set(TABLE_KEYS):
        raise ValueError("V3 sync target tables do not match the contract")
    if not all(isinstance(value, str) and value.startswith("tbl") for value in tables.values()):
        raise ValueError("V3 sync target contains an invalid table ID")
    base_token = target.get("base_token")
    if not isinstance(base_token, str) or not base_token:
        raise ValueError("V3 sync target is missing base_token")
    return base_token, str(target["identity"]), tables


def _projected_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    data = result.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("record list response did not contain data")
    fields = data.get("fields")
    records = data.get("data")
    if not isinstance(fields, list) or not isinstance(records, list):
        raise RuntimeError("record list response did not contain records")
    output: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, list) or len(record) != len(fields):
            raise RuntimeError("record list response contains a malformed projected row")
        output.append(dict(zip((str(field) for field in fields), record, strict=True)))
    return output


def _existing_keys(
    runtime: list[str],
    common: list[str],
    table_id: str,
    stable_name: str,
) -> set[str]:
    output: set[str] = set()
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
                "--offset",
                str(offset),
                "--limit",
                "200",
            ]
        )
        rows = _projected_rows(result)
        for fields in rows:
            if stable_name not in fields:
                raise RuntimeError(f"record is missing stable key: {stable_name}")
            key = feishu_record_to_key(fields[stable_name], stable_name)
            if key in output:
                raise ValueError(f"Feishu table contains duplicate stable key: {key}")
            output.add(key)
        if not result.get("data", {}).get("has_more"):
            break
        offset += len(rows)
    return output


def main() -> int:
    args = _arguments()
    contract = load_json_object(args.contract)
    tables = validate_contract(contract)
    records = validate_backfill(load_json_object(args.backfill), tables)
    base_token, identity, target_tables = _target(args.target)
    runtime = _runtime()
    common = ["--as", identity, "--base-token", base_token, "--format", "json"]
    result_summary: dict[str, dict[str, int]] = {}

    for logical_name in TABLE_KEYS:
        table = tables[logical_name]
        stable_key = str(table["fields"][0]["key"])
        stable_name = str(table["fields"][0]["name"])
        desired = records[logical_name]
        existing = _existing_keys(runtime, common, target_tables[logical_name], stable_name)
        desired_keys = {str(row[stable_key]) for row in desired}
        extra = existing - desired_keys
        if extra:
            raise ValueError(
                f"Feishu V3 table contains {len(extra)} keys outside the approved backfill: {logical_name}"
            )
        missing = [row for row in desired if str(row[stable_key]) not in existing]
        created = 0
        operations = 0
        for start in range(0, len(missing), WRITE_BATCH_SIZE):
            payload = build_feishu_rows(table, missing[start : start + WRITE_BATCH_SIZE])
            command = [
                *runtime,
                "base",
                "+record-batch-create",
                *common,
                "--table-id",
                target_tables[logical_name],
                "--json",
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
            ]
            if not args.apply:
                command.append("--dry-run")
            result = _run(command, dry_run=not args.apply)
            if args.apply:
                data = result.get("data")
                ids = data.get("record_id_list") if isinstance(data, dict) else None
                created += len(ids) if isinstance(ids, list) else len(payload["rows"])
                time.sleep(1.25)
            operations += 1
        result_summary[logical_name] = {
            "approved": len(desired),
            "existing": len(existing),
            "missing": len(missing),
            "created": created,
            "operations": operations,
        }

    print(
        json.dumps(
            {
                "status": "ok",
                "mode": "apply" if args.apply else "dry-run",
                "tables": result_summary,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
