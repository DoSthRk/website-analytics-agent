"""Migrate existing weekly Feishu rows to the extensible period-key model."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from website_analytics.periods import AnalyticsPeriod, dashboard_windows, period_key


PRODUCT_IDS = {
    "GMP": "GMP",
    "SOLIDEX": "SOLIDEX",
    "AAV Processing": "AAV_PROCESSING",
}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-token", required=True)
    parser.add_argument("--overview-table", required=True)
    parser.add_argument("--product-table", required=True)
    parser.add_argument("--anchor", required=True)
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
    environment = dict(os.environ)
    environment["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    environment["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
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
        try:
            result = json.loads(completed.stdout or completed.stderr)
        except json.JSONDecodeError as error:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip()) from error
        code = result.get("error", {}).get("code") if isinstance(result, dict) else None
        if completed.returncode != 0 and code == 800004135 and attempt < 5:
            time.sleep(3)
            continue
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
        if dry_run and not isinstance(result.get("api"), list):
            raise RuntimeError(json.dumps(result, ensure_ascii=False))
        if not dry_run and result.get("ok") is not True:
            raise RuntimeError(json.dumps(result, ensure_ascii=False))
        return result
    raise RuntimeError("lark-cli retry limit exceeded")


def _records(result: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    data = result.get("data", {})
    fields = data.get("fields")
    rows = data.get("data")
    record_ids = data.get("record_id_list")
    if not isinstance(fields, list) or not isinstance(rows, list) or not isinstance(record_ids, list):
        raise RuntimeError("unexpected record-list response")
    return [
        (str(record_id), dict(zip(fields, row, strict=True)))
        for record_id, row in zip(record_ids, rows, strict=True)
    ]


def _date(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError("period date is missing")
    return date.fromisoformat(value[:10])


def _date_cell(value: date) -> str:
    return f"{value.isoformat()} 00:00:00"


def _period(fields: dict[str, Any]) -> AnalyticsPeriod:
    return AnalyticsPeriod(
        kind="week",
        start=_date(fields.get("周期开始")),
        end=_date(fields.get("周期结束")),
    )


def _common_patch(
    fields: dict[str, Any],
    period: AnalyticsPeriod,
    anchor: date,
    *,
    inquiry_complete: bool,
) -> dict[str, Any]:
    return {
        "周期键": period_key("genemedi-net", period),
        "统计粒度": "周",
        "滚动天数": None,
        "GA4数据截至": _date_cell(period.end),
        "GSC数据截至": _date_cell(period.end),
        "询盘数据截至": _date_cell(period.end) if inquiry_complete else None,
        "是否最终值": inquiry_complete,
        "看板窗口": list(dashboard_windows(period, anchor)),
        "同步批次": "time-model-migration-2026-08-19",
    }


def main() -> int:
    args = _arguments()
    anchor = date.fromisoformat(args.anchor)
    runtime = _runtime()
    common = ["--as", "user", "--base-token", args.base_token, "--format", "json"]
    overview = _records(
        _run(
            [
                *runtime,
                "base",
                "+record-list",
                *common,
                "--table-id",
                args.overview_table,
                "--page-size",
                "200",
            ]
        )
    )
    product = _records(
        _run(
            [
                *runtime,
                "base",
                "+record-list",
                *common,
                "--table-id",
                args.product_table,
                "--page-size",
                "200",
            ]
        )
    )
    operations: list[tuple[str, str, dict[str, Any]]] = []
    for record_id, fields in overview:
        period = _period(fields)
        inquiry_complete = fields.get("数据状态") == ["complete"] or fields.get("数据状态") == "complete"
        patch = _common_patch(fields, period, anchor, inquiry_complete=inquiry_complete)
        patch["周期"] = period.label
        operations.append((args.overview_table, record_id, patch))
    for record_id, fields in product:
        period = _period(fields)
        product_name = fields.get("产品大类")
        if isinstance(product_name, list) and len(product_name) == 1:
            product_name = product_name[0]
        if product_name not in PRODUCT_IDS:
            raise ValueError("existing product row has an unsupported product category")
        completeness = fields.get("数据完整性")
        inquiry_complete = completeness == ["完整"] or completeness == "完整"
        patch = _common_patch(fields, period, anchor, inquiry_complete=inquiry_complete)
        patch["产品周期键"] = f"{patch['周期键']}|{PRODUCT_IDS[str(product_name)]}"
        patch["周期标签"] = period.label
        operations.append((args.product_table, record_id, patch))

    for table_id, record_id, patch in operations:
        command = [
            *runtime,
            "base",
            "+record-upsert",
            *common,
            "--table-id",
            table_id,
            "--record-id",
            record_id,
            "--json",
            json.dumps(patch, ensure_ascii=False, separators=(",", ":")),
        ]
        if not args.apply:
            command.append("--dry-run")
        _run(command, dry_run=not args.apply)
        if args.apply:
            time.sleep(1.1)

    print(
        json.dumps(
            {
                "status": "ok",
                "mode": "apply" if args.apply else "dry-run",
                "overview_records": len(overview),
                "product_records": len(product),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
