"""Create (or validate and reuse) the three Feishu period tables.

Dry-run by default: lists the Base's tables and runs +table-create with
--dry-run. With --apply it creates missing tables, checks the field types of
tables that already exist, and writes the period sync target. With
--add-missing-fields, contract fields an existing table lacks are created
(fields are only ever added; any other difference still stops the run).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from website_analytics.feishu_period_facts import (  # noqa: E402
    CONTRACT_VERSION,
    TABLE_KEYS,
    contract_tables,
    period_contract,
    table_field_definitions,
)
from website_analytics.feishu_product_funnel import load_json_object  # noqa: E402
from website_analytics.feishu_tables import (  # noqa: E402
    created_table_id,
    field_types,
    lark_runtime,
    run_lark,
    table_ids_by_name,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-from", type=Path, required=True, help="An existing sync target in the same Base (base token source)")
    parser.add_argument("--period-json", type=Path, required=True, help="build_period_facts.py output (select options)")
    parser.add_argument("--target", type=Path, required=True, help="Where to write the period sync target")
    parser.add_argument("--identity", choices=("user", "bot"), default="bot")
    parser.add_argument("--sync-identity", choices=("user", "bot"), default="bot")
    parser.add_argument("--add-missing-fields", action="store_true")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    base_token = str(load_json_object(args.base_from)["base_token"])
    records = json.loads(args.period_json.read_text(encoding="utf-8"))["records"]
    tables = contract_tables(period_contract())
    runtime = lark_runtime()
    common = ["--as", args.identity, "--base-token", base_token, "--format", "json"]
    existing = table_ids_by_name(run_lark([*runtime, "base", "+table-list", *common]))

    resolved: dict[str, str] = {}
    report = []
    for index, logical_name in enumerate(TABLE_KEYS):
        table = tables[logical_name]
        name = str(table["display_name"])
        definitions = table_field_definitions(table, records[logical_name])
        expected = {str(item["name"]): str(item["type"]) for item in definitions}
        table_id = existing.get(name)
        if table_id:
            actual = field_types(run_lark([*runtime, "base", "+field-list", *common, "--table-id", table_id, "--limit", "200"]))
            missing = [item for item in definitions if str(item["name"]) not in actual]
            extra = sorted(set(actual) - set(expected))
            retyped = sorted(key for key in set(actual) & set(expected) if actual[key] != expected[key])
            if extra or retyped or (missing and not args.add_missing_fields):
                raise SystemExit(f"existing table {name} differs from the contract: missing "
                                 f"{[item['name'] for item in missing]}, extra {extra}, other type {retyped}")
            for item in missing:
                command = [*runtime, "base", "+field-create", *common, "--table-id", table_id,
                           "--json", json.dumps(item, ensure_ascii=False, separators=(",", ":"))]
                if not args.apply:
                    command.append("--dry-run")
                run_lark(command, dry_run=not args.apply)
                if args.apply:
                    time.sleep(1.25)
            report.append({"table": name, "action": "reuse", "fields": len(definitions),
                           "added_fields": [item["name"] for item in missing]})
        else:
            command = [*runtime, "base", "+table-create", *common, "--name", name,
                       "--fields", json.dumps(definitions, ensure_ascii=False, separators=(",", ":"))]
            if not args.apply:
                command.append("--dry-run")
            result = run_lark(command, dry_run=not args.apply)
            table_id = created_table_id(result) if args.apply else f"tbl_dry_run_{index}"
            report.append({"table": name, "action": "create" if args.apply else "would_create", "fields": len(definitions)})
            if args.apply:
                time.sleep(1.25)
        resolved[logical_name] = table_id

    if args.apply:
        target = {"version": CONTRACT_VERSION, "base_token": base_token, "identity": args.sync_identity, "tables": resolved}
        args.target.parent.mkdir(parents=True, exist_ok=True)
        args.target.write_text(json.dumps(target, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"mode": "apply" if args.apply else "dry_run", "tables": report, "target_written": args.apply}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
