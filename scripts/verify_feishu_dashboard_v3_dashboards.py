"""Verify V3 dashboard containers and filtered chart results against local facts."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Any

from website_analytics.feishu_records import is_retryable_lark_failure, lark_cli_environment
from website_analytics.feishu_v3 import load_json_object, validate_backfill, validate_contract


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-token", required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--backfill", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--filter-start", type=date.fromisoformat, required=True)
    parser.add_argument("--filter-end", type=date.fromisoformat, required=True)
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


def _dashboard_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    items = result.get("data", {}).get("items")
    if not isinstance(items, list):
        raise AssertionError("dashboard list did not return items")
    return [item for item in items if isinstance(item, dict)]


def _block_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    data = result.get("data", {})
    items = data.get("blocks") or data.get("items") or data.get("data")
    if not isinstance(items, list):
        raise AssertionError("dashboard block list did not return blocks")
    return [item for item in items if isinstance(item, dict)]


def _actual_series(
    result: dict[str, Any],
    metric_name: str,
    group_names: list[str],
) -> dict[Any, int]:
    data = result.get("data")
    if not isinstance(data, dict):
        raise AssertionError("chart result did not contain data")
    measures = data.get("measures")
    rows = data.get("main_data")
    dimensions = data.get("dimensions") or []
    if not isinstance(measures, list) or len(measures) != 1 or not isinstance(rows, list):
        raise AssertionError("chart result has an unexpected measure shape")
    measure = measures[0]
    if measure.get("field_name") != metric_name:
        raise AssertionError(f"chart metric differs: {metric_name}")
    if [item.get("field_name") for item in dimensions] != group_names:
        raise AssertionError(f"chart dimensions differ: {group_names}")
    measure_alias = measure.get("alias")
    aliases = [item.get("alias") for item in dimensions]
    output: dict[Any, int] = {}
    for row in rows:
        values = [row[alias]["value"] for alias in aliases]
        values = [str(value)[:10] if name == "数据日期" else value for name, value in zip(group_names, values, strict=True)]
        key: Any
        if not values:
            key = None
        elif len(values) == 1:
            key = values[0]
        else:
            key = tuple(values)
        if key in output:
            raise AssertionError(f"chart returned a duplicate group: {key}")
        output[key] = int(row[measure_alias]["value"])
    return output


def _expected_series(
    rows: list[dict[str, Any]],
    metric_key: str,
    group_keys: list[str],
    start: date,
    end: date,
) -> dict[Any, int]:
    output: dict[Any, int] = {}
    for row in rows:
        row_date = date.fromisoformat(str(row["data_date"]))
        if not start <= row_date <= end or row.get("data_status") != "complete":
            continue
        values = [row[key] for key in group_keys]
        key: Any
        if not values:
            key = None
        elif len(values) == 1:
            key = values[0]
        else:
            key = tuple(values)
        output[key] = output.get(key, 0) + int(row[metric_key])
    return output


def main() -> int:
    args = _arguments()
    if args.filter_end < args.filter_start:
        raise ValueError("filter end must not precede filter start")
    contract = load_json_object(args.contract)
    tables = validate_contract(contract)
    rows_by_table = validate_backfill(load_json_object(args.backfill), tables)
    manifest = load_json_object(args.manifest)
    result = load_json_object(args.result)
    dashboards = manifest.get("dashboards")
    created = result.get("dashboards")
    if not isinstance(dashboards, list) or not isinstance(created, list):
        raise ValueError("dashboard manifest or result is invalid")
    created_by_name = {str(item["name"]): item for item in created}
    if {str(item["name"]) for item in dashboards} != set(created_by_name):
        raise AssertionError("dashboard result names differ from manifest")

    runtime = _runtime()
    common = ["--as", "user", "--base-token", args.base_token, "--format", "json"]
    listed = _run([*runtime, "base", "+dashboard-list", *common])
    live_dashboards = {str(item["name"]): str(item["dashboard_id"]) for item in _dashboard_items(listed)}
    verified_charts = 0

    for dashboard in dashboards:
        name = str(dashboard["name"])
        result_dashboard = created_by_name[name]
        dashboard_id = str(result_dashboard["dashboard_id"])
        if live_dashboards.get(name) != dashboard_id:
            raise AssertionError(f"live dashboard ID differs: {name}")
        live_blocks = _block_items(
            _run(
                [
                    *runtime,
                    "base",
                    "+dashboard-block-list",
                    *common,
                    "--dashboard-id",
                    dashboard_id,
                ]
            )
        )
        live_by_name = {str(item["name"]): item for item in live_blocks}
        manifest_blocks = dashboard.get("blocks")
        if not isinstance(manifest_blocks, list):
            raise ValueError(f"dashboard has invalid blocks: {name}")
        if set(live_by_name) != {str(item["name"]) for item in manifest_blocks}:
            raise AssertionError(f"live dashboard blocks differ: {name}")
        result_blocks = {str(item["name"]): item for item in result_dashboard["blocks"]}

        for block in manifest_blocks:
            block_name = str(block["name"])
            block_type = str(block["type"])
            if live_by_name[block_name].get("type") != block_type:
                raise AssertionError(f"live block type differs: {name} / {block_name}")
            if block_type == "text":
                continue
            block_id = str(result_blocks[block_name]["block_id"])
            data_config = block["data_config"]
            table_name = str(data_config["table_name"])
            logical_name = next(
                logical
                for logical, table in tables.items()
                if table["display_name"] == table_name
            )
            fields = {str(field["name"]): str(field["key"]) for field in tables[logical_name]["fields"]}
            metric_name = str(data_config["series"][0]["field_name"])
            group_names = [str(item["field_name"]) for item in data_config.get("group_by", [])]
            expected = _expected_series(
                rows_by_table[logical_name],
                fields[metric_name],
                [fields[group] for group in group_names],
                args.filter_start,
                args.filter_end,
            )
            actual = _actual_series(
                _run(
                    [
                        *runtime,
                        "base",
                        "+dashboard-block-get-data",
                        *common,
                        "--block-id",
                        block_id,
                    ]
                ),
                metric_name,
                group_names,
            )
            time.sleep(1.25)
            if actual != expected:
                raise AssertionError(f"chart values differ: {name} / {block_name}")
            verified_charts += 1

    print(
        json.dumps(
            {
                "status": "ok",
                "dashboards": len(dashboards),
                "blocks": sum(len(item["blocks"]) for item in dashboards),
                "verified_charts": verified_charts,
                "filter_range": {
                    "start": args.filter_start.isoformat(),
                    "end": args.filter_end.isoformat(),
                },
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
