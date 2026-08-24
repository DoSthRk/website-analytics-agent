"""Generate local V3 dashboard prototype records from approved CLI caches."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from website_analytics.dashboard_sync import load_period_details
from website_analytics.dashboard_v3 import build_v3_dry_run
from website_analytics.information_mapping import load_information_mapping
from website_analytics.periods import AnalyticsPeriod
from website_analytics.product_mapping import load_product_mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--previous-start", type=date.fromisoformat, required=True)
    parser.add_argument("--previous-end", type=date.fromisoformat, required=True)
    parser.add_argument("--product-evidence", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=PROJECT_ROOT / "cache")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--refreshed-at", type=_datetime, required=True)
    return parser.parse_args()


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


def _product_paths(path: Path) -> set[str]:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("product evidence is unavailable") from error
    if not isinstance(value, list):
        raise ValueError("product evidence must contain a list")
    paths: set[str] = set()
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("product evidence rows must be objects")
        if row.get("pageClass") != "product_page":
            continue
        candidate = row.get("canonicalPath")
        if not isinstance(candidate, str) or not candidate:
            raise ValueError("product evidence path is invalid")
        paths.add(candidate)
    return paths


def main() -> int:
    args = _arguments()
    product_mapping = load_product_mapping(
        PROJECT_ROOT / "config" / "product_mappings" / f"{args.site}.yaml",
        args.site,
    )
    information_mapping = load_information_mapping(
        PROJECT_ROOT / "config" / "information_mappings" / f"{args.site}.yaml",
        args.site,
    )
    if product_mapping is None or information_mapping is None:
        raise ValueError("V3 prototype mappings are unavailable")
    current = AnalyticsPeriod("week", args.start, args.end)
    previous = AnalyticsPeriod("week", args.previous_start, args.previous_end)
    payload = build_v3_dry_run(
        site=args.site,
        current_start=args.start,
        current_end=args.end,
        previous_start=args.previous_start,
        previous_end=args.previous_end,
        current_details=load_period_details(args.cache_dir, args.site, current),
        previous_details=load_period_details(args.cache_dir, args.site, previous),
        product_mapping=product_mapping,
        information_mapping=information_mapping,
        product_paths=_product_paths(args.product_evidence),
        refreshed_at=args.refreshed_at,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "mode": "dry_run",
                "write_enabled": False,
                "output": str(args.output),
                "overview_records": len(payload["records"]["overview_periods"]),
                "product_records": len(payload["records"]["product_periods"]),
                "information_records": len(
                    payload["records"]["information_periods"]
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
