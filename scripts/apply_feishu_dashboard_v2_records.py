"""Preview or apply weekly aggregate records for the Feishu dashboard v2."""

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
    parser.add_argument("--overview-table", required=True)
    parser.add_argument("--product-table", required=True)
    parser.add_argument("--overview-record-id", required=True)
    parser.add_argument("--product-record", action="append", required=True)
    parser.add_argument("--payload-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


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
    product_ids: dict[str, str] = {}
    for value in args.product_record:
        product, separator, record_id = value.partition("=")
        if not separator or not product or not record_id:
            raise ValueError("--product-record must use Product=record_id")
        product_ids[product] = record_id

    overview_history = _read(args.payload_dir / "overview-history-create.json")
    product_history = _read(args.payload_dir / "product-history-create.json")
    overview_patch = _read(args.payload_dir / "overview-current-patch.json")
    product_patches = _read(args.payload_dir / "product-current-patches.json")
    if set(product_ids) != set(product_patches):
        raise ValueError("product record IDs do not match current product patches")

    runtime = _runtime()
    common = ["--as", "user", "--base-token", args.base_token]
    operations: list[list[str]] = [
        [
            *runtime, "base", "+record-batch-update", *common,
            "--table-id", args.overview_table,
            "--json", _json({"record_id_list": [args.overview_record_id], "patch": overview_patch}),
        ],
    ]
    for product in sorted(product_ids):
        operations.append(
            [
                *runtime, "base", "+record-batch-update", *common,
                "--table-id", args.product_table,
                "--json", _json({"record_id_list": [product_ids[product]], "patch": product_patches[product]}),
            ]
        )
    operations.extend(
        [
            [
                *runtime, "base", "+record-batch-create", *common,
                "--table-id", args.overview_table, "--json", _json(overview_history),
            ],
            [
                *runtime, "base", "+record-batch-create", *common,
                "--table-id", args.product_table, "--json", _json(product_history),
            ],
        ]
    )

    created = 0
    updated = 0
    for command in operations:
        if not args.apply:
            command.append("--dry-run")
        result = _run(command, dry_run=not args.apply)
        if args.apply:
            data = result.get("data", {})
            record_ids = data.get("record_id_list", []) if isinstance(data, dict) else []
            if "+record-batch-create" in command:
                created += len(record_ids)
            else:
                updated += len(record_ids) or 1
            time.sleep(1.25)

    print(
        json.dumps(
            {
                "status": "ok",
                "mode": "apply" if args.apply else "dry-run",
                "operations": len(operations),
                "updated": updated,
                "created": created,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
