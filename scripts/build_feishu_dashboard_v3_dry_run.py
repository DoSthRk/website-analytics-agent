"""Build one local V3 daily payload from an approved CLI fetch."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

from website_analytics.adapters.page_dimension import create_page_dimension_adapter
from website_analytics.config import load_sites, require_site
from website_analytics.dashboard_sync import load_period_details
from website_analytics.dashboard_v3 import build_v3_daily_records
from website_analytics.information_mapping import load_information_mapping
from website_analytics.page_classification import (
    build_page_dimension,
    load_page_classification,
)
from website_analytics.periods import AnalyticsPeriod
from website_analytics.product_mapping import load_product_mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--fetch-result", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=PROJECT_ROOT / "cache")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--site-config", type=Path, default=PROJECT_ROOT / "config" / "sites.yaml"
    )
    parser.add_argument(
        "--product-mapping",
        type=Path,
        default=PROJECT_ROOT / "config" / "product_mappings" / "genemedi-net.yaml",
    )
    parser.add_argument(
        "--information-mapping",
        type=Path,
        default=PROJECT_ROOT
        / "config"
        / "information_mappings"
        / "genemedi-net.yaml",
    )
    parser.add_argument(
        "--page-classification",
        type=Path,
        default=PROJECT_ROOT
        / "config"
        / "page_classifications"
        / "genemedi-net.yaml",
    )
    parser.add_argument(
        "--page-dimension-fixture",
        type=Path,
        help="Optional fixed page-dimension fixture for offline validation",
    )
    return parser.parse_args()


def _object(path: Path, label: str) -> dict[str, Any]:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is unavailable") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain an object")
    return value


def _page_rows(path: Path) -> list[dict[str, object]]:
    document = _object(path, "page dimension fixture")
    value = document.get("rows")
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError("page dimension fixture must contain object rows")
    return [dict(row) for row in value]


def main() -> int:
    args = _arguments()
    site = require_site(load_sites(args.site_config), args.site)
    product_mapping = load_product_mapping(args.product_mapping, args.site)
    information_mapping = load_information_mapping(args.information_mapping, args.site)
    if product_mapping is None or information_mapping is None:
        raise ValueError("V3 mappings are unavailable")
    classification = load_page_classification(args.page_classification, args.site)
    if args.page_dimension_fixture is not None:
        page_rows = _page_rows(args.page_dimension_fixture)
    else:
        if site.inquiry_source is None:
            raise ValueError("registered read-only page dimension source is unavailable")
        page_rows = create_page_dimension_adapter(site.inquiry_source).query()
    page_dimension = build_page_dimension(classification, page_rows)
    period = AnalyticsPeriod("day", args.date, args.date)
    payload = build_v3_daily_records(
        site=args.site,
        data_date=args.date,
        fetch_result=_object(args.fetch_result, "approved CLI fetch result"),
        details=load_period_details(args.cache_dir, args.site, period),
        product_mapping=product_mapping,
        page_dimension=page_dimension,
        information_mapping=information_mapping,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "mode": "daily_dry_run",
                "write_enabled": False,
                "data_date": args.date.isoformat(),
                "output": str(args.output),
                "overview_records": len(payload["records"]["overview_daily"]),
                "product_records": len(payload["records"]["product_daily"]),
                "information_records": len(
                    payload["records"]["information_daily"]
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
