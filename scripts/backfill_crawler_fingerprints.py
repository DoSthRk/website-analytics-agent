"""Fetch the GA4 crawler fingerprint report for a date range (read-only GA4).

Fetches one calendar month per request, maps product landing pages to report
lines with the current rules, runs the crawler rule and prints the monthly
product landing sessions with their suspected crawler share for review. With
--apply the days are saved to the fingerprint store (existing days replaced).
Use it to seed the store, and again for every day after a URL-rule change.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from run_period_refresh import _load_env, fetch_fingerprints, ga4_adapter  # noqa: E402

from website_analytics.adapters.page_dimension import create_page_dimension_adapter  # noqa: E402
from website_analytics.config import load_sites, require_site  # noqa: E402
from website_analytics.crawler_traffic import (  # noqa: E402
    crawler_shares,
    load_crawler_rule,
    load_fingerprint_days,
    monthly_summary,
    save_fingerprint_days,
)
from website_analytics.page_classification import build_page_dimension, load_page_classification  # noqa: E402
from website_analytics.product_mapping import load_product_mapping  # noqa: E402


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default="genemedi-net")
    parser.add_argument("--site-config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True, help="KEY=VALUE file holding the read-only DB credential")
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--page-classification", type=Path, required=True)
    parser.add_argument("--crawler-store", type=Path, required=True)
    parser.add_argument("--crawler-rule", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.end < args.start:
        parser.error("--end is before --start")
    return args


def months(start: date, end: date) -> list[list[date]]:
    chunks: list[list[date]] = []
    day = start
    while day <= end:
        following = (day.replace(day=1) + timedelta(days=32)).replace(day=1)
        last = min(following - timedelta(days=1), end)
        chunks.append([day + timedelta(days=offset) for offset in range((last - day).days + 1)])
        day = last + timedelta(days=1)
    return chunks


def main() -> int:
    args = _arguments()
    _load_env(args.env_file)
    site = require_site(load_sites(args.site_config), args.site)
    if site.inquiry_source is None:
        raise SystemExit("registered read-only route source is required")
    mapping = load_product_mapping(args.mapping, site.site_key)
    if mapping is None:
        raise SystemExit("approved product mapping is required")
    rule = load_crawler_rule(args.crawler_rule, site.site_key)
    dimension = build_page_dimension(
        load_page_classification(args.page_classification, site.site_key),
        create_page_dimension_adapter(site.inquiry_source).query(),
    )
    adapter = ga4_adapter()
    fetched: dict[str, dict] = {}
    for chunk in months(args.start, args.end):
        documents = fetch_fingerprints(adapter, site, chunk, mapping, dimension)
        fetched |= documents
        print(json.dumps({"fetched": f"{chunk[0]}..{chunk[-1]}", "days": len(documents)}), file=sys.stderr, flush=True)
    documents = load_fingerprint_days(args.crawler_store) | fetched
    days = [date.fromisoformat(day) for day in sorted(fetched)]
    crawler = crawler_shares(documents, days, rule, mapping_version=mapping.version)
    summary = {
        "mode": "apply" if args.apply else "dry_run",
        "rule": rule.version,
        "mapping_version": mapping.version,
        "days": len(days),
        "months": monthly_summary(documents, crawler),
    }
    if args.apply:
        saved = save_fingerprint_days(args.crawler_store, fetched, datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None))
        summary["saved_days"] = len(saved)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
