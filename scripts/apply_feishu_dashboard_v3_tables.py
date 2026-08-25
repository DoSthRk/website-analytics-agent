"""Create or reuse the three V3 daily Feishu tables without touching V2."""

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
    build_table_fields,
    load_json_object,
    validate_backfill,
    validate_contract,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-token", required=True)
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


def _table_id(result: dict[str, Any]) -> str:
    data = result.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("table create response did not contain data")
    candidates = (
        data.get("table_id"),
        data.get("id"),
        data.get("table", {}).get("table_id") if isinstance(data.get("table"), dict) else None,
        data.get("table", {}).get("id") if isinstance(data.get("table"), dict) else None,
    )
    value = next((item for item in candidates if isinstance(item, str) and item.startswith("tbl")), None)
    if value is None:
        raise RuntimeError("table create response did not contain a table ID")
    return value


def _field_types(result: dict[str, Any]) -> dict[str, str]:
    data = result.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("field list response did not contain data")
    fields = data.get("fields") or data.get("items") or data.get("data")
    if not isinstance(fields, list):
        raise RuntimeError("field list response did not contain fields")
    output = {
        str(item["name"]): str(item["type"])
        for item in fields
        if isinstance(item, dict) and item.get("name") and item.get("type")
    }
    if len(output) != len(fields):
        raise RuntimeError("field list contains missing or duplicate field names")
    return output


def main() -> int:
    args = _arguments()
    contract = load_json_object(args.contract)
    tables = validate_contract(contract)
    backfill = load_json_object(args.backfill)
    rows_by_table = validate_backfill(backfill, tables)
    runtime = _runtime()
    common = ["--as", "user", "--base-token", args.base_token, "--format", "json"]
    table_list = _run([*runtime, "base", "+table-list", *common])
    existing = {
        str(item["name"]): str(item["id"])
        for item in table_list.get("data", {}).get("tables", [])
        if isinstance(item, dict) and item.get("name") and item.get("id")
    }
    resolved: dict[str, str] = {}
    created: list[str] = []
    reused: list[str] = []

    for index, logical_name in enumerate(TABLE_KEYS):
        table = tables[logical_name]
        display_name = str(table["display_name"])
        definitions = build_table_fields(table, rows_by_table[logical_name])
        expected_types = {str(item["name"]): str(item["type"]) for item in definitions}
        table_id = existing.get(display_name)
        if table_id:
            if not args.apply:
                raise ValueError(
                    f"dry-run refuses an existing V3 table; run with --apply to validate and reuse: {display_name}"
                )
            actual_types = _field_types(
                _run([*runtime, "base", "+field-list", *common, "--table-id", table_id, "--limit", "200"])
            )
            if actual_types != expected_types:
                raise ValueError(f"existing V3 table schema differs from contract: {display_name}")
            reused.append(logical_name)
        else:
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
            if not args.apply:
                command.append("--dry-run")
            result = _run(command, dry_run=not args.apply)
            table_id = _table_id(result) if args.apply else f"tbl_dry_run_{index}"
            created.append(logical_name)
            if args.apply:
                time.sleep(1.25)
        resolved[logical_name] = table_id

    target = {
        "version": "3",
        "base_token": args.base_token,
        "identity": "user",
        "tables": resolved,
    }
    if args.apply:
        args.target.parent.mkdir(parents=True, exist_ok=True)
        args.target.write_text(json.dumps(target, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "ok",
                "mode": "apply" if args.apply else "dry-run",
                "created": created,
                "reused": reused,
                "target_written": bool(args.apply),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
