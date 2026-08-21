"""Preview or apply the approved Feishu dashboard v2 field schema."""

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
    parser.add_argument("--updates", type=Path, required=True)
    parser.add_argument("--creates", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--start-update", type=int, default=0)
    parser.add_argument("--start-create", type=int, default=0)
    parser.add_argument("--skip-updates", action="store_true")
    parser.add_argument("--skip-creates", action="store_true")
    return parser.parse_args()


def _load_array(path: Path) -> list[dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, list) or not all(isinstance(item, dict) for item in document):
        raise ValueError(f"configuration must contain an array of objects: {path}")
    return document


def _run(command: list[str], *, dry_run: bool) -> None:
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
            message = completed.stderr.strip() or completed.stdout.strip() or "lark-cli failed"
            raise RuntimeError(message) from error
        error_code = result.get("error", {}).get("code") if isinstance(result, dict) else None
        if completed.returncode != 0 and error_code == 800004135 and attempt < 5:
            time.sleep(3)
            continue
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "lark-cli failed"
            raise RuntimeError(message)
        break
    else:
        raise RuntimeError("lark-cli retry limit reached")
    if not isinstance(result, dict):
        raise RuntimeError(completed.stdout.strip())
    if dry_run and not isinstance(result.get("api"), list):
        raise RuntimeError(completed.stdout.strip())
    if not dry_run and result.get("ok") is not True:
        raise RuntimeError(completed.stdout.strip())


def main() -> int:
    args = _arguments()
    lark_wrapper = shutil.which("lark-cli")
    if lark_wrapper is None:
        raise RuntimeError("lark-cli is unavailable")
    if os.name != "nt":
        command_prefix = [lark_wrapper]
    else:
        node = shutil.which("node")
        if node is None:
            raise RuntimeError("node is unavailable")
        run_script = Path(lark_wrapper).parent / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js"
        if not run_script.is_file():
            raise RuntimeError("lark-cli runtime script is unavailable")
        command_prefix = [node, str(run_script)]
    updates = _load_array(args.updates)
    creates = _load_array(args.creates)
    common = ["--as", "user", "--base-token", args.base_token]
    mode = "apply" if args.apply else "dry-run"

    selected_updates = [] if args.skip_updates else updates[args.start_update:]
    selected_creates = [] if args.skip_creates else creates[args.start_create:]

    for item in selected_updates:
        command = [
            *command_prefix,
            "base",
            "+field-update",
            *common,
            "--table-id",
            str(item["table_id"]),
            "--field-id",
            str(item["field_id"]),
            "--json",
            json.dumps(item["definition"], ensure_ascii=False, separators=(",", ":")),
        ]
        command.append("--yes" if args.apply else "--dry-run")
        _run(command, dry_run=not args.apply)
        if args.apply:
            time.sleep(1.25)

    for item in selected_creates:
        command = [
            *command_prefix,
            "base",
            "+field-create",
            *common,
            "--table-id",
            str(item["table_id"]),
            "--json",
            json.dumps(item["definition"], ensure_ascii=False, separators=(",", ":")),
        ]
        if not args.apply:
            command.append("--dry-run")
        _run(command, dry_run=not args.apply)
        if args.apply:
            time.sleep(1.25)

    print(json.dumps({"status": "ok", "mode": mode, "field_updates": len(selected_updates), "field_creates": len(selected_creates)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
