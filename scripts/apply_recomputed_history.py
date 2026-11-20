"""Write recomputed daily facts (from recompute_history.py) to Feishu in day batches.

Without --apply it only validates and prints what would be written. With
--apply it uses the same idempotent sync functions as the daily job: rows are
matched by stable key, only changed rows are updated, and every batch is read
back. Days after --last-final are refused, because V3 holds finalized days only.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from website_analytics.feishu_product_funnel import (  # noqa: E402
    additive_totals as funnel_additive_totals,
    validate_backfill as validate_funnel_backfill,
    validate_contract as validate_funnel_contract,
)
from website_analytics.feishu_product_funnel_sync import (  # noqa: E402
    load_product_funnel_target,
    sync_product_funnel_record_sets,
)
from website_analytics.feishu_records import LarkCLIRecordClient  # noqa: E402
from website_analytics.feishu_v3 import additive_totals, load_json_object, validate_backfill, validate_contract  # noqa: E402
from website_analytics.feishu_v3_sync import load_feishu_v3_target, sync_v3_record_sets  # noqa: E402
from website_analytics.history_recompute import FUNNEL_TABLES, V3_TABLES  # noqa: E402


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recompute-json", type=Path, required=True)
    parser.add_argument("--days", required=True, help="Comma-separated YYYY-MM-DD list, or 'all'")
    parser.add_argument("--last-final", type=date.fromisoformat, required=True)
    parser.add_argument("--batch-days", type=int, default=7)
    parser.add_argument("--v3-contract", type=Path, required=True)
    parser.add_argument("--v3-target", type=Path, required=True)
    parser.add_argument("--funnel-contract", type=Path, required=True)
    parser.add_argument("--funnel-target", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def _documents(result: dict, days: list[str]) -> tuple[dict, dict]:
    chosen = set(days)
    v3 = {name: [r for r in result["records"][name] if r["data_date"][:10] in chosen] for name in V3_TABLES}
    funnel = {name: [r for r in result["records"][name] if r["data_date"][:10] in chosen] for name in FUNNEL_TABLES}
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    span = {"start": min(days), "end": max(days), "inclusive": True, "days": len(days)}
    common = {
        "site": result["site"],
        "date_range": span,
        "generated_at": now,
        "rules": result["rules"]["current"],
    }
    v3_document = {
        "schema_version": "3",
        "mode": "history_recompute",
        "write_enabled": True,
        **common,
        "records": v3,
        "reconciliation": {
            "status": "passed",
            "complete_days": len(days),
            "record_counts": {name: len(rows) for name, rows in v3.items()},
            "unique_stable_keys": True,
            "additive_totals": additive_totals(v3),
        },
    }
    funnel_document = {
        "schema_version": "1",
        "mode": "history_recompute",
        # Same as the daily job: the funnel contract requires this flag to stay
        # False; the sync function still performs the reviewed upsert.
        "write_enabled": False,
        **common,
        "records": funnel,
        "reconciliation": {
            "status": "passed",
            "complete_days": len(days),
            "record_counts": {name: len(rows) for name, rows in funnel.items()},
            "unique_stable_keys": True,
            "additive_totals": funnel_additive_totals(funnel),
        },
    }
    return v3_document, funnel_document


def main() -> int:
    args = _arguments()
    result = json.loads(args.recompute_json.read_text(encoding="utf-8"))
    if result.get("mismatches"):
        raise SystemExit("recompute has reconciliation mismatches; refusing to write")
    completed = set(result["completed_days"])
    wanted = sorted(completed) if args.days == "all" else sorted(d.strip() for d in args.days.split(","))
    missing = [d for d in wanted if d not in completed]
    if missing:
        raise SystemExit(f"days were not recomputed: {missing}")
    wanted = [d for d in wanted if date.fromisoformat(d) <= args.last_final]
    if not wanted:
        raise SystemExit("no finalized days selected")

    v3_contract = load_json_object(args.v3_contract)
    validate_contract(v3_contract)
    funnel_contract = load_json_object(args.funnel_contract)
    validate_funnel_contract(funnel_contract)
    v3_target = load_feishu_v3_target(args.v3_target)
    funnel_target = load_product_funnel_target(args.funnel_target)

    batches = [wanted[i:i + args.batch_days] for i in range(0, len(wanted), args.batch_days)]
    for batch in batches:
        v3_document, funnel_document = _documents(result, batch)
        validate_backfill(v3_document, validate_contract(v3_contract))
        validate_funnel_backfill(funnel_document, validate_funnel_contract(funnel_contract))
        summary = {
            "days": [batch[0], batch[-1], len(batch)],
            "v3_rows": v3_document["reconciliation"]["record_counts"],
            "funnel_rows": funnel_document["reconciliation"]["record_counts"],
        }
        if args.apply:
            summary["v3_write"] = sync_v3_record_sets(
                LarkCLIRecordClient(v3_target.record_client_target()), v3_target, v3_contract, v3_document
            )
            summary["funnel_write"] = sync_product_funnel_record_sets(
                LarkCLIRecordClient(funnel_target.record_client_target()), funnel_target, funnel_contract, funnel_document
            )
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    print(json.dumps({"mode": "apply" if args.apply else "dry_run", "days": len(wanted), "batches": len(batches)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
