"""Preview or create Feishu dashboard v2 containers and API-safe chart blocks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-token", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--result", type=Path)
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


def _dashboard_id(result: dict[str, Any]) -> str:
    data = result.get("data", {})
    candidates = [
        data.get("dashboard_id") if isinstance(data, dict) else None,
        data.get("id") if isinstance(data, dict) else None,
        data.get("dashboard", {}).get("dashboard_id") if isinstance(data, dict) and isinstance(data.get("dashboard"), dict) else None,
        data.get("dashboard", {}).get("id") if isinstance(data, dict) and isinstance(data.get("dashboard"), dict) else None,
    ]
    value = next((item for item in candidates if isinstance(item, str) and item), None)
    if value is None:
        raise RuntimeError("dashboard create response did not contain a dashboard ID")
    return value


def _block_id(result: dict[str, Any]) -> str:
    data = result.get("data", {})
    candidates = [
        data.get("block_id") if isinstance(data, dict) else None,
        data.get("id") if isinstance(data, dict) else None,
        data.get("block", {}).get("block_id") if isinstance(data, dict) and isinstance(data.get("block"), dict) else None,
        data.get("block", {}).get("id") if isinstance(data, dict) and isinstance(data.get("block"), dict) else None,
    ]
    value = next((item for item in candidates if isinstance(item, str) and item), None)
    if value is None:
        raise RuntimeError("block create response did not contain a block ID")
    return value


def main() -> int:
    args = _arguments()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    dashboards = manifest.get("dashboards") if isinstance(manifest, dict) else None
    if not isinstance(dashboards, list) or not dashboards:
        raise ValueError("manifest must contain dashboards")
    runtime = _runtime()
    common = ["--as", "user", "--base-token", args.base_token]
    created: list[dict[str, Any]] = []

    for index, dashboard in enumerate(dashboards):
        if not isinstance(dashboard, dict) or not isinstance(dashboard.get("blocks"), list):
            raise ValueError("invalid dashboard manifest entry")
        dashboard_command = [
            *runtime, "base", "+dashboard-create", *common, "--name", str(dashboard["name"]),
        ]
        if not args.apply:
            dashboard_command.append("--dry-run")
        dashboard_result = _run(dashboard_command, dry_run=not args.apply)
        dashboard_id = _dashboard_id(dashboard_result) if args.apply else f"blk_dry_run_{index}"
        result_entry = {"name": dashboard["name"], "dashboard_id": dashboard_id, "blocks": []}
        if args.apply:
            time.sleep(1.25)

        for block in dashboard["blocks"]:
            if block.get("managed_via") == "feishu_ui":
                result_entry["blocks"].append(
                    {
                        "name": block["name"],
                        "type": block["type"],
                        "block_id": None,
                        "managed_via": "feishu_ui",
                    }
                )
                continue
            block_command = [
                *runtime, "base", "+dashboard-block-create", *common,
                "--dashboard-id", dashboard_id,
                "--name", str(block["name"]),
                "--type", str(block["type"]),
                "--data-config", json.dumps(block["data_config"], ensure_ascii=False, separators=(",", ":")),
            ]
            if not args.apply:
                block_command.append("--dry-run")
            block_result = _run(block_command, dry_run=not args.apply)
            block_id = _block_id(block_result) if args.apply else "cht_dry_run"
            result_entry["blocks"].append(
                {"name": block["name"], "type": block["type"], "block_id": block_id}
            )
            if args.apply:
                time.sleep(1.25)

        arrange_command = [
            *runtime, "base", "+dashboard-arrange", *common, "--dashboard-id", dashboard_id,
        ]
        if not args.apply:
            arrange_command.append("--dry-run")
        _run(arrange_command, dry_run=not args.apply)
        created.append(result_entry)
        if args.apply:
            time.sleep(1.25)

    if args.apply and args.result is not None:
        args.result.parent.mkdir(parents=True, exist_ok=True)
        args.result.write_text(
            json.dumps({"dashboards": created}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "status": "ok",
                "mode": "apply" if args.apply else "dry-run",
                "dashboards": len(created),
                "blocks": sum(len(item["blocks"]) for item in created),
                "ui_managed_blocks": sum(
                    1
                    for item in created
                    for block in item["blocks"]
                    if block.get("managed_via") == "feishu_ui"
                ),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
