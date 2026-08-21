"""Read, preview, and apply rolling-window filters to Feishu trend charts."""

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
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def _runtime() -> list[str]:
    wrapper = shutil.which("lark-cli")
    if wrapper is None:
        raise RuntimeError("lark-cli is unavailable")
    if os.name != "nt":
        return [wrapper]
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("node is unavailable")
    script = Path(wrapper).parent / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js"
    if not script.is_file():
        raise RuntimeError("lark-cli runtime script is unavailable")
    return [node, str(script)]


def _run(command: list[str], *, dry_run: bool = False) -> dict[str, Any]:
    environment = dict(os.environ)
    environment["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    environment["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
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
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    if dry_run and not isinstance(result.get("api"), list):
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    if not dry_run and result.get("ok") is not True:
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    return result


def main() -> int:
    args = _arguments()
    items = json.loads(args.config.read_text(encoding="utf-8"))
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError("filter config must contain an array")
    runtime = _runtime()
    common = ["--as", "user", "--base-token", args.base_token, "--format", "json"]

    for item in items:
        data_config = item.get("data_config")
        if data_config is None:
            data_config = {"filter": item["filter"]}
        if not isinstance(data_config, dict):
            raise ValueError("dashboard block data_config must contain an object")
        _run(
            [
                *runtime,
                "base",
                "+dashboard-block-get",
                *common,
                "--dashboard-id",
                str(item["dashboard_id"]),
                "--block-id",
                str(item["block_id"]),
            ]
        )
        command = [
            *runtime,
            "base",
            "+dashboard-block-update",
            *common,
            "--dashboard-id",
            str(item["dashboard_id"]),
            "--block-id",
            str(item["block_id"]),
            "--data-config",
            json.dumps(data_config, ensure_ascii=False, separators=(",", ":")),
        ]
        if item.get("name") is not None:
            command.extend(["--name", str(item["name"])])
        if not args.apply:
            command.append("--dry-run")
        _run(command, dry_run=not args.apply)
        if args.apply:
            time.sleep(1.1)
    print(
        json.dumps(
            {"status": "ok", "mode": "apply" if args.apply else "dry-run", "blocks": len(items)}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
