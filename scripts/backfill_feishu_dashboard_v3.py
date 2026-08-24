"""Build one local multi-day V3 backfill from approved CLI artifacts."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

from website_analytics.config import load_sites, require_site
from website_analytics.dashboard_v3_backfill import build_v3_backfill
from website_analytics.information_mapping import load_information_mapping
from website_analytics.page_classification import (
    build_page_dimension,
    load_page_classification,
)
from website_analytics.product_mapping import load_product_mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--cache-dir", type=Path, default=PROJECT_ROOT / "cache")
    parser.add_argument("--audit-dir", type=Path, default=PROJECT_ROOT / "audits")
    parser.add_argument("--page-dimension-fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--site-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "sites.yaml",
    )
    parser.add_argument(
        "--product-mapping",
        type=Path,
        default=PROJECT_ROOT / "config" / "product_mappings" / "genemedi-net.yaml",
    )
    parser.add_argument(
        "--information-mapping",
        type=Path,
        default=(
            PROJECT_ROOT
            / "config"
            / "information_mappings"
            / "genemedi-net.yaml"
        ),
    )
    parser.add_argument(
        "--page-classification",
        type=Path,
        default=(
            PROJECT_ROOT
            / "config"
            / "page_classifications"
            / "genemedi-net.yaml"
        ),
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


def _page_rows(path: Path, expected_site: str) -> list[dict[str, object]]:
    document = _object(path, "page dimension fixture")
    fixture_site = document.get("site")
    if fixture_site is not None and fixture_site != expected_site:
        raise ValueError("page dimension fixture belongs to another site")
    value = document.get("rows")
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError("page dimension fixture must contain object rows")
    return [dict(row) for row in value]


def main() -> int:
    args = _arguments()
    require_site(load_sites(args.site_config), args.site)
    product_mapping = load_product_mapping(args.product_mapping, args.site)
    information_mapping = load_information_mapping(
        args.information_mapping, args.site
    )
    if product_mapping is None or information_mapping is None:
        raise ValueError("V3 mappings are unavailable")
    classification = load_page_classification(
        args.page_classification, args.site
    )
    page_dimension = build_page_dimension(
        classification,
        _page_rows(args.page_dimension_fixture, args.site),
    )
    payload = build_v3_backfill(
        site=args.site,
        start=args.start,
        end=args.end,
        cache_dir=args.cache_dir,
        audit_dir=args.audit_dir,
        product_mapping=product_mapping,
        page_dimension=page_dimension,
        information_mapping=information_mapping,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    reconciliation = payload["reconciliation"]
    print(
        json.dumps(
            {
                "status": "ok",
                "mode": "backfill_dry_run",
                "write_enabled": False,
                "site": args.site,
                "date_range": payload["date_range"],
                "reconciliation": reconciliation["status"],
                "record_counts": reconciliation["record_counts"],
                "output": str(args.output),
            },
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
