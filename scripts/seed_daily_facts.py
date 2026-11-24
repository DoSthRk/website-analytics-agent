"""Seed the daily-facts store from recompute_history.py outputs (later files win per day)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from website_analytics.daily_facts_store import load_days, merge_recompute_outputs, save_days  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recompute-json", type=Path, action="append", required=True)
    parser.add_argument("--store", type=Path, required=True)
    args = parser.parse_args()
    if args.store.exists() and any(args.store.glob("????-??-??.json")):
        raise SystemExit("store already has days; seed into an empty directory")
    days = save_days(args.store, merge_recompute_outputs(args.recompute_json))
    stored = load_days(args.store)
    print(json.dumps({"days": len(days), "first": days[0], "last": days[-1],
                      "overview_rows": len(stored["overview_daily"]), "funnel_rows": len(stored["product_funnel_daily"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
