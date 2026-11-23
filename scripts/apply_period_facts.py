"""Write period facts (from build_period_facts.py) to the Feishu period tables.

Without --apply it only reads the live tables and prints how many rows would
be created, updated or left unchanged. With --apply it writes those rows and
reads every desired row back. Rows are never deleted.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from website_analytics.feishu_period_facts import (  # noqa: E402
    TABLE_KEYS,
    apply_period_plans,
    load_period_target,
    period_contract,
    plan_period_sync,
)
from website_analytics.feishu_records import LarkCLIRecordClient  # noqa: E402


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period-json", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--tables", default=",".join(TABLE_KEYS))
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    tables = tuple(name.strip() for name in args.tables.split(",") if name.strip())
    if not tables or not set(tables) <= set(TABLE_KEYS):
        raise SystemExit(f"--tables must be a subset of {','.join(TABLE_KEYS)}")
    document = json.loads(args.period_json.read_text(encoding="utf-8"))
    target = load_period_target(args.target)
    client = LarkCLIRecordClient(target.record_client_target())
    refreshed_at = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None, microsecond=0)
    plans = plan_period_sync(client, target, period_contract(), document["records"], refreshed_at=refreshed_at, tables=tables)
    summary = {"mode": "apply" if args.apply else "dry_run", "period_summary": document.get("summary", {}).get("periods")}
    if args.apply:
        summary["result"] = apply_period_plans(client, plans)
    else:
        summary["plan"] = {plan.logical_name: plan.counts() for plan in plans}
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
