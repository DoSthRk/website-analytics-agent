"""lark-cli helpers for creating and inspecting Feishu Base tables."""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

from website_analytics.feishu_records import _runtime, is_retryable_lark_failure, lark_cli_environment


def lark_runtime() -> list[str]:
    """Same resolution as the record client: the wrapper itself off Windows."""
    return _runtime()


def run_lark(command: list[str], *, dry_run: bool = False) -> dict[str, Any]:
    """Run one lark-cli command, retrying only failures lark marks as transient."""
    environment = lark_cli_environment()
    for attempt in range(6):
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=environment, check=False
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


def table_ids_by_name(result: dict[str, Any]) -> dict[str, str]:
    return {
        str(item["name"]): str(item["id"])
        for item in result.get("data", {}).get("tables", [])
        if isinstance(item, dict) and item.get("name") and item.get("id")
    }


def created_table_id(result: dict[str, Any]) -> str:
    data = result.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("table create response did not contain data")
    table = data.get("table") if isinstance(data.get("table"), dict) else {}
    for value in (data.get("table_id"), data.get("id"), table.get("table_id"), table.get("id")):
        if isinstance(value, str) and value.startswith("tbl"):
            return value
    raise RuntimeError("table create response did not contain a table ID")


def field_types(result: dict[str, Any]) -> dict[str, str]:
    data = result.get("data")
    fields = (data.get("fields") or data.get("items") or data.get("data")) if isinstance(data, dict) else None
    if not isinstance(fields, list):
        raise RuntimeError("field list response did not contain fields")
    output = {str(item["name"]): str(item["type"]) for item in fields if isinstance(item, dict) and item.get("name") and item.get("type")}
    if len(output) != len(fields):
        raise RuntimeError("field list contains missing or duplicate field names")
    return output
