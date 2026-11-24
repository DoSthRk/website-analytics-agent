"""Build weekly/monthly period facts from recomputed daily facts, without writes.

Reads the daily-facts store, or one or more recompute_history.py outputs (a
later file replaces earlier files day by day), aggregates them with ``website_analytics.period_facts``, and
writes the period records, bot-like candidate periods and a summary to JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from website_analytics.daily_facts_store import load_days, merge_recompute_outputs  # noqa: E402
from website_analytics.period_facts import build_period_facts, load_exclusions  # noqa: E402
from website_analytics.product_mapping import load_product_mapping  # noqa: E402


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default="genemedi-net")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--recompute-json", type=Path, action="append")
    source.add_argument("--daily-facts-dir", type=Path)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--exclusions", type=Path, required=True)
    parser.add_argument("--last-final", type=date.fromisoformat)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    mapping = load_product_mapping(args.mapping, args.site)
    if mapping is None:
        raise SystemExit("product mapping is required")
    daily = load_days(args.daily_facts_dir) if args.daily_facts_dir else merge_recompute_outputs(args.recompute_json)
    days = sorted({str(row["data_date"])[:10] for row in daily["overview_daily"]})
    last_final = args.last_final or date.fromisoformat(days[-1])
    build = build_period_facts(
        site=args.site,
        mapping=mapping,
        funnel_daily=daily["product_funnel_daily"],
        overview_daily=daily["overview_daily"],
        last_final=last_final,
        exclusions=load_exclusions(args.exclusions, args.site),
    )
    summary = dict(build.summary)
    summary["days"] = len(days)
    one_mode = (row for row in build.records["site_period"] if row["comparison_mode"] in ("前8周均值", "前3月均值"))
    summary["statuses"] = dict(Counter(f"{row['grain']}:{row['period_status']}" for row in one_mode))
    args.output.write_text(
        json.dumps({"summary": summary, "candidates": build.candidates, "records": build.records}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
