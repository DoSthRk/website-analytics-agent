"""Correct overview records to accepted GA4 interval totals."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


OVERVIEW_PATCHES: dict[str, dict[str, Any]] = {
    "recvsJpe8iNDji": {
        "官网访问次数": 5816,
        "官网访客数": 5251,
        "表单提交事件（GA4）": 9,
        "访问较上周": None,
        "表单事件较上周": None,
        "运营摘要": "首个可用周期，暂无上周对比；询盘数据缺失。",
    },
    "recvsJpe8iKvuj": {
        "官网访问次数": 5681,
        "官网访客数": 5139,
        "表单提交事件（GA4）": 12,
        "访问较上周": -135,
        "表单事件较上周": 3,
        "运营摘要": (
            "官网访问较上周减少 135次；Google自然搜索点击较上周减少 218次；"
            "GA4表单提交事件较上周增加 3次；询盘数据缺失，无法环比。"
        ),
    },
    "recvsJpe8iDUOL": {
        "官网访问次数": 6320,
        "官网访客数": 5589,
        "表单提交事件（GA4）": 4,
        "访问较上周": 639,
        "表单事件较上周": -8,
        "运营摘要": (
            "官网访问较上周增加 639次；Google自然搜索点击较上周增加 231次；"
            "GA4表单提交事件较上周减少 8次；本周期官网入库询盘 5 条，"
            "上周期询盘数据缺失。"
        ),
    },
    "recvsJiBqqDBx3": {
        "官网访问次数": 5847,
        "官网访客数": 5398,
        "表单提交事件（GA4）": 7,
        "访问较上周": -473,
        "表单事件较上周": 3,
        "运营摘要": (
            "官网访问较上周减少 473次；Google自然搜索点击较上周增加 31次；"
            "GA4表单提交事件较上周增加 3次；官网入库询盘较上周增加 1条。"
        ),
    },
}

def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-token", required=True)
    parser.add_argument("--overview-table", required=True)
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


def _run(command: list[str], *, dry_run: bool) -> dict[str, Any]:
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
        break
    if not isinstance(result, dict):
        raise RuntimeError("lark-cli returned an invalid result")
    if dry_run and not isinstance(result.get("api"), list):
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    if not dry_run and result.get("ok") is not True:
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    return result


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def main() -> int:
    args = _arguments()
    runtime = _runtime()
    common = ["--as", "user", "--base-token", args.base_token]
    operations: list[list[str]] = []
    for record_id, patch in OVERVIEW_PATCHES.items():
        operations.append(
            [
                *runtime,
                "base",
                "+record-batch-update",
                *common,
                "--table-id",
                args.overview_table,
                "--json",
                _json({"record_id_list": [record_id], "patch": patch}),
            ]
        )
    for command in operations:
        if not args.apply:
            command.append("--dry-run")
        _run(command, dry_run=not args.apply)
        if args.apply:
            time.sleep(1.25)

    print(
        json.dumps(
            {
                "status": "ok",
                "mode": "apply" if args.apply else "dry-run",
                "overview_records": len(OVERVIEW_PATCHES),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
