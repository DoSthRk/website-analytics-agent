"""Read-only aggregate coverage audit for approved page mapping rules.

The command uses the registered page-dimension adapter and emits counts only.
It never prints page URLs, credentials, inquiry records, or arbitrary SQL output.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from website_analytics.adapters.page_dimension import create_page_dimension_adapter
from website_analytics.config import load_sites, require_site
from website_analytics.information_mapping import (
    classify_information_page,
    load_information_mapping,
)
from website_analytics.page_classification import (
    build_page_dimension,
    load_page_classification,
)
from website_analytics.product_mapping import (
    load_product_mapping,
    match_product_rule,
)


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
    parser.add_argument(
        "--information-mapping",
        type=Path,
        default=Path("config/information_mappings/genemedi-net.yaml"),
    )
    return parser.parse_args()


def _counter_rows(counter: Counter[str]) -> list[dict[str, str | int]]:
    return [
        {"id": identifier, "pages": pages}
        for identifier, pages in counter.most_common()
    ]


def main() -> int:
    args = _arguments()
    site = require_site(load_sites(args.site_config), args.site)
    if site.inquiry_source is None:
        raise ValueError("registered read-only page dimension is required")
    classification = load_page_classification(args.page_classification, site.site_key)
    product_mapping = load_product_mapping(args.product_mapping, site.site_key)
    information_mapping = load_information_mapping(
        args.information_mapping, site.site_key
    )
    if product_mapping is None or information_mapping is None:
        raise ValueError("approved product and information mappings are required")
    dimension = build_page_dimension(
        classification,
        create_page_dimension_adapter(site.inquiry_source).query(),
    )

    product_lines: Counter[str] = Counter()
    product_rules: Counter[str] = Counter()
    product_fallback_templates: Counter[str] = Counter()
    information_themes: Counter[str] = Counter()
    information_content_types: Counter[str] = Counter()
    information_theme_statuses: Counter[str] = Counter()
    information_content_statuses: Counter[str] = Counter()
    information_fallback_templates: Counter[str] = Counter()

    for path, page in dimension.entries.items():
        if page.page_class == "product_page":
            rule = match_product_rule(
                product_mapping, path, page.page_class, page.template
            )
            if rule is None or rule.report_line_id is None:
                product_lines["UNMATCHED"] += 1
                continue
            product_lines[rule.report_line_id] += 1
            product_rules[rule.identifier] += 1
            if rule.identifier == "other-product-fallback":
                product_fallback_templates[page.template or "[blank]"] += 1
        elif page.page_class == "information_page":
            assigned = classify_information_page(
                information_mapping,
                path=path,
                template=page.template,
                page_class=page.page_class,
            )
            information_themes[assigned["informationThemeId"]] += 1
            information_content_types[assigned["informationContentTypeId"]] += 1
            information_theme_statuses[assigned["informationThemeStatus"]] += 1
            information_content_statuses[
                assigned["informationContentTypeStatus"]
            ] += 1
            if assigned["informationContentTypeStatus"] == "fallback":
                information_fallback_templates[page.template or "[blank]"] += 1

    product_pages = int(dimension.summary.get("productPages", 0))
    information_pages = int(dimension.summary.get("informationPages", 0))
    product_assigned = product_pages - product_lines["UNMATCHED"]
    explicit_information_content = information_content_statuses["matched"]
    result = {
        "status": "ok",
        "site": site.site_key,
        "pageClassificationVersion": dimension.version,
        "productMappingVersion": product_mapping.version,
        "informationMappingVersion": information_mapping.version,
        "dimension": dict(dimension.summary),
        "product": {
            "pages": product_pages,
            "assignedPages": product_assigned,
            "assignedRate": product_assigned / product_pages if product_pages else None,
            "reportLines": _counter_rows(product_lines),
            "rules": _counter_rows(product_rules),
            "otherProductTemplates": _counter_rows(product_fallback_templates),
        },
        "information": {
            "pages": information_pages,
            "themes": _counter_rows(information_themes),
            "contentTypes": _counter_rows(information_content_types),
            "themeStatuses": _counter_rows(information_theme_statuses),
            "contentTypeStatuses": _counter_rows(information_content_statuses),
            "explicitContentTypeRate": (
                explicit_information_content / information_pages
                if information_pages
                else None
            ),
            "fallbackContentTemplates": _counter_rows(
                information_fallback_templates
            ),
        },
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
