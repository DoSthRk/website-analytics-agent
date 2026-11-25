"""Preview or create the 产品诊断 dashboard on the period tables.

Every chart field is checked against the period-table contract first. Without
--apply only lark-cli dry runs are made. An existing dashboard of the same
name is refused unless --reuse-existing, which keeps blocks whose name and
type already match so an interrupted apply can resume or new blocks can be
added; --update-block rewrites a kept block from the manifest, and a block
found under one of its previous_names is renamed. Dashboard filters are
not available through the API; the manifest lists them for setup in the UI.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from website_analytics.feishu_period_facts import load_period_target, period_contract  # noqa: E402
from website_analytics.feishu_tables import lark_runtime, run_lark  # noqa: E402



def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True, help="period sync_target.json (base token, identity)")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reuse-existing", action="store_true")
    parser.add_argument("--update-block", action="append", default=[], metavar="NAME",
                        help="With --reuse-existing, overwrite this existing block's data_config from the manifest")
    args = parser.parse_args()
    if args.update_block and not args.reuse_existing:
        parser.error("--update-block requires --reuse-existing")
    return args


def validate_manifest(manifest: dict[str, Any], contract: dict[str, Any]) -> None:
    """Fail on any table, field, rollup or select value the period tables do not have."""
    fields: dict[str, dict[str, dict[str, Any]]] = {}
    for table in contract["tables"]:
        fields[table["display_name"]] = {field["name"]: field for field in table["fields"]}
    for dashboard in manifest["dashboards"]:
        names = [block["name"] for block in dashboard["blocks"]]
        names += [old for block in dashboard["blocks"] for old in block.get("previous_names", [])]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate block names in {dashboard['name']}")
        layout = dashboard.get("layout")
        if layout is not None:
            placed = [name for row in layout["rows"] for name, _ in row]
            if sorted(placed) != sorted(block["name"] for block in dashboard["blocks"]):
                raise ValueError(f"layout of {dashboard['name']} must place every block exactly once")
            if any(sum(width for _, width in row) != layout["columns"] for row in layout["rows"]):
                raise ValueError(f"every layout row of {dashboard['name']} must fill {layout['columns']} columns")
        for block in dashboard["blocks"]:
            config = block["data_config"]
            if block["type"] == "text":
                if set(config) != {"text"} or not config["text"].strip():
                    raise ValueError(f"text block {block['name']} must only hold text")
                continue
            table = fields.get(config.get("table_name"))
            if table is None:
                raise ValueError(f"{block['name']}: unknown table {config.get('table_name')}")
            if ("series" in config) == bool(config.get("count_all")):
                raise ValueError(f"{block['name']}: use exactly one of series and count_all")
            for series in config.get("series", []):
                field = table.get(series["field_name"])
                if field is None or field["type"] != "number" or series["rollup"] not in {"SUM", "MAX", "MIN", "AVERAGE"}:
                    raise ValueError(f"{block['name']}: invalid series {series}")
            if len(config.get("group_by", [])) > 2:
                raise ValueError(f"{block['name']}: at most two group_by fields")
            for group in config.get("group_by", []):
                if group["field_name"] not in table or group.get("mode") != "integrated":
                    raise ValueError(f"{block['name']}: invalid group_by {group}")
            for condition in config.get("filter", {}).get("conditions", []):
                field = table.get(condition["field_name"])
                if field is None:
                    raise ValueError(f"{block['name']}: unknown filter field {condition['field_name']}")
                options = [option["name"] if isinstance(option, dict) else option for option in field.get("options", [])]
                if field["type"] == "select" and options and condition["value"] not in options:
                    raise ValueError(f"{block['name']}: {condition['value']!r} is not an option of {field['name']}")
                if field["type"] == "checkbox" and not isinstance(condition["value"], bool):
                    raise ValueError(f"{block['name']}: checkbox filters take true/false")
        for ui_filter in dashboard.get("ui_filters", []):
            for table_name in ui_filter["tables"]:
                if ui_filter["field"] not in fields.get(table_name, {}):
                    raise ValueError(f"UI filter field {ui_filter['field']} is missing from {table_name}")


def _data(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data")
    return data if isinstance(data, dict) else {}


def _find_id(result: dict[str, Any], kind: str, prefix: str) -> str:
    data = _data(result)
    nested = data.get(kind) if isinstance(data.get(kind), dict) else {}
    for value in (data.get(f"{kind}_id"), data.get("id"), nested.get(f"{kind}_id"), nested.get("id")):
        if isinstance(value, str) and value.startswith(prefix):
            return value
    raise RuntimeError(f"{kind} create response did not contain an ID")


def main() -> int:
    args = _arguments()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    validate_manifest(manifest, period_contract())
    target = load_period_target(args.target)
    common = ["--base-token", target.base_token, "--as", target.identity]
    runtime = lark_runtime()

    listed = run_lark([*runtime, "base", "+dashboard-list", *common])
    items = _data(listed).get("items") or _data(listed).get("dashboards") or []
    existing = {str(item["name"]): str(item["dashboard_id"]) for item in items if isinstance(item, dict) and item.get("dashboard_id")}

    manifest_names = {block["name"] for dashboard in manifest["dashboards"] for block in dashboard["blocks"]}
    unknown = sorted(set(args.update_block) - manifest_names)
    if unknown:
        raise SystemExit(f"--update-block names not in the manifest: {unknown}")

    report: list[dict[str, Any]] = []
    for index, dashboard in enumerate(manifest["dashboards"]):
        name = dashboard["name"]
        reused_blocks: dict[str, dict[str, Any]] = {}
        if name in existing:
            if not args.reuse_existing:
                raise SystemExit(f"dashboard {name!r} already exists; pass --reuse-existing to resume it")
            dashboard_id = existing[name]
            blocks = run_lark([*runtime, "base", "+dashboard-block-list", *common, "--dashboard-id", dashboard_id])
            for item in _data(blocks).get("blocks") or _data(blocks).get("items") or []:
                block_name = item.get("block_name") or item.get("name")
                if block_name in reused_blocks:
                    raise SystemExit(f"duplicate live block name {block_name!r}")
                reused_blocks[str(block_name)] = item
        elif args.apply:
            dashboard_id = _find_id(run_lark([*runtime, "base", "+dashboard-create", *common, "--name", name]), "dashboard", "blk")
            time.sleep(1.25)
        else:
            run_lark([*runtime, "base", "+dashboard-create", *common, "--name", name, "--dry-run"], dry_run=True)
            dashboard_id = f"blk_dry_run_{index}"

        entry: dict[str, Any] = {"name": name, "dashboard_id": dashboard_id, "blocks": []}
        for block in dashboard["blocks"]:
            live = reused_blocks.get(block["name"])
            renamed_from = None
            if live is None:
                for old in block.get("previous_names", []):
                    if old in reused_blocks:
                        live, renamed_from = reused_blocks[old], old
                        break
            if live is not None:
                if (live.get("block_type") or live.get("type")) != block["type"]:
                    raise SystemExit(f"live block {block['name']!r} has another type")
                updated = block["name"] in args.update_block
                if updated or renamed_from:
                    update = [
                        *runtime, "base", "+dashboard-block-update", *common, "--dashboard-id", dashboard_id,
                        "--block-id", str(live.get("block_id")),
                    ]
                    if renamed_from:
                        update += ["--name", block["name"]]
                    if updated:
                        update += ["--data-config",
                                   json.dumps(block["data_config"], ensure_ascii=False, separators=(",", ":"))]
                    if args.apply:
                        run_lark(update)
                        time.sleep(1.25)
                    else:
                        run_lark([*update, "--dry-run"], dry_run=True)
                entry["blocks"].append({"name": block["name"], "block_id": live.get("block_id"), "reused": True,
                                        "updated": updated, "renamed_from": renamed_from})
                continue
            command = [
                *runtime, "base", "+dashboard-block-create", *common, "--dashboard-id", dashboard_id,
                "--name", block["name"], "--type", block["type"],
                "--data-config", json.dumps(block["data_config"], ensure_ascii=False, separators=(",", ":")),
            ]
            if args.apply:
                block_id = _find_id(run_lark(command), "block", "cht")
                time.sleep(1.25)
            else:
                run_lark([*command, "--dry-run"], dry_run=True)
                block_id = None
            entry["blocks"].append({"name": block["name"], "block_id": block_id})
        # Auto-arrange is only for a fresh dashboard: it would scramble a layout someone set by hand.
        if args.apply and not reused_blocks:
            run_lark([*runtime, "base", "+dashboard-arrange", *common, "--dashboard-id", dashboard_id])
        report.append(entry)

    summary = {
        "mode": "apply" if args.apply else "dry_run",
        "dashboards": [
            {"name": item["name"], "dashboard_id": item["dashboard_id"], "blocks": len(item["blocks"]),
             "reused": sum(1 for block in item["blocks"] if block.get("reused")),
             "updated": [block["name"] for block in item["blocks"] if block.get("updated")],
             "renamed": sum(1 for block in item["blocks"] if block.get("renamed_from"))}
            for item in report
        ],
        "ui_filters_to_set": [ui["field"] for dashboard in manifest["dashboards"] for ui in dashboard.get("ui_filters", [])],
    }
    if args.apply and args.result is not None:
        args.result.write_text(json.dumps({"dashboards": report}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
