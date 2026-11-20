"""Dry-run: recompute cached daily facts with new URL rules and compare with old.

Reads only the local cache, audit manifests and the read-only route table.
Writes one local JSON file; never calls Google APIs or Feishu.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from website_analytics.adapters.page_dimension import create_page_dimension_adapter  # noqa: E402
from website_analytics.config import load_sites, require_site  # noqa: E402
from website_analytics.history_recompute import RuleSet, recompute_days  # noqa: E402
from website_analytics.information_mapping import load_information_mapping  # noqa: E402
from website_analytics.page_classification import build_page_dimension, load_page_classification  # noqa: E402
from website_analytics.product_mapping import load_product_mapping  # noqa: E402


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default="genemedi-net")
    parser.add_argument("--site-config", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--page-classification", type=Path, required=True)
    parser.add_argument("--information-mapping", type=Path, required=True)
    parser.add_argument("--baseline-mapping", type=Path, required=True)
    parser.add_argument("--baseline-page-classification", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--env-file", type=Path, help="KEY=VALUE file holding the read-only DB credential")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _load_env(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    args = _arguments()
    if args.end < args.start or (args.end - args.start).days > 400:
        raise SystemExit("date range must be ordered and at most 400 days")
    if args.env_file:
        _load_env(args.env_file)
    site = require_site(load_sites(args.site_config), args.site)
    if site.inquiry_source is None:
        raise SystemExit("registered read-only route source is required")
    route_rows = create_page_dimension_adapter(site.inquiry_source).query()
    information = load_information_mapping(args.information_mapping, site.site_key)

    def rules(mapping_path: Path, classification_path: Path) -> RuleSet:
        mapping = load_product_mapping(mapping_path, site.site_key)
        if mapping is None or information is None:
            raise SystemExit("approved product and information mappings are required")
        dimension = build_page_dimension(load_page_classification(classification_path, site.site_key), route_rows)
        return RuleSet(mapping, dimension, information)

    days = [args.start + timedelta(days=offset) for offset in range((args.end - args.start).days + 1)]
    result = recompute_days(
        site=site.site_key,
        days=days,
        cache_dir=args.cache_dir,
        audit_dir=args.audit_dir,
        current=rules(args.mapping, args.page_classification),
        baseline=rules(args.baseline_mapping, args.baseline_page_classification),
    )
    args.output.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    summary = {key: value for key, value in result.items() if key != "records"}
    summary["completed_days"] = len(result["completed_days"])
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
