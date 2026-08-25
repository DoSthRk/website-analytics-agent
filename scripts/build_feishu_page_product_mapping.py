"""Build an audited full-site URL-to-product mapping artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from website_analytics.adapters.page_dimension import create_page_dimension_adapter
from website_analytics.config import load_sites, require_site
from website_analytics.page_classification import (
    build_page_dimension,
    load_page_classification,
)
from website_analytics.page_product_dimension import (
    build_page_product_document,
    normalize_snapshot_datetime,
)
from website_analytics.product_mapping import load_product_mapping


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", default="genemedi-net")
    parser.add_argument("--site-config", type=Path, default=Path("config/sites.yaml"))
    parser.add_argument(
        "--page-classification",
        type=Path,
        default=Path("config/page_classifications/genemedi-net.yaml"),
    )
    parser.add_argument(
        "--product-mapping",
        type=Path,
        default=Path("config/product_mappings/genemedi-net.yaml"),
    )
    parser.add_argument("--page-dimension-snapshot", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _snapshot(path: Path) -> tuple[list[dict[str, object]], str, str]:
    raw = path.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("page dimension snapshot is invalid") from error
    if not isinstance(document, dict) or not isinstance(document.get("rows"), list):
        raise ValueError("page dimension snapshot must contain rows")
    rows = document["rows"]
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("page dimension snapshot contains invalid rows")
    generated_at = document.get("generated_at")
    if not isinstance(generated_at, str):
        raise ValueError("page dimension snapshot is missing generated_at")
    return rows, normalize_snapshot_datetime(generated_at), hashlib.sha256(raw).hexdigest()


def main() -> int:
    args = _arguments()
    site = require_site(load_sites(args.site_config), args.site)
    if site.inquiry_source is None:
        raise ValueError("registered read-only page dimension is required")
    classification = load_page_classification(args.page_classification, site.site_key)
    product_mapping = load_product_mapping(args.product_mapping, site.site_key)
    if product_mapping is None:
        raise ValueError("approved product mapping is required")

    if args.page_dimension_snapshot is not None:
        source_rows, generated_at, source_sha256 = _snapshot(args.page_dimension_snapshot)
    else:
        source_rows = create_page_dimension_adapter(site.inquiry_source).query()
        raise ValueError(
            "live page-dimension builds require an immutable snapshot output before Feishu projection"
        )
    dimension = build_page_dimension(classification, source_rows)
    primary_domain = next((domain for domain in site.domains if domain.startswith("www.")), site.domains[0])
    document = build_page_product_document(
        site=site.site_key,
        base_url=f"https://{primary_domain}",
        page_dimension=dimension,
        product_mapping=product_mapping,
        source_generated_at=generated_at,
        source_sha256=source_sha256,
        source_rows=len(source_rows),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "output": str(args.output),
                "source": document["source"],
                "versions": document["versions"],
                "reconciliation": document["reconciliation"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
