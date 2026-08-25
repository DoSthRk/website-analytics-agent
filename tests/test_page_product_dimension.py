from __future__ import annotations

import json
from pathlib import Path

import pytest

from website_analytics.page_classification import (
    PageClassificationConfig,
    build_page_dimension,
)
from website_analytics.page_product_dimension import (
    build_page_product_document,
    partition_records,
    validate_contract,
    validate_document,
)
from website_analytics.product_mapping import load_product_mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_page_product_dimension_has_one_hashed_row_per_canonical_url() -> None:
    mapping = load_product_mapping(
        PROJECT_ROOT / "config" / "product_mappings" / "genemedi-net.yaml",
        "genemedi-net",
    )
    assert mapping is not None
    dimension = build_page_dimension(
        PageClassificationConfig("genemedi-net", "page-v1", {}),
        [
            _row("i/gmp-vt-p173", 1, "indexwithSideBa_product"),
            _row("products", 2, "index-general"),
        ],
    )
    document = build_page_product_document(
        site="genemedi-net",
        base_url="https://www.genemedi.net",
        page_dimension=dimension,
        product_mapping=mapping,
        source_generated_at="2026-08-24T09:04:05Z",
        source_sha256="a" * 64,
        source_rows=2,
    )
    contract = json.loads(
        (PROJECT_ROOT / "config" / "feishu_dashboard" / "v3" / "page_product_mapping_contract.json").read_text(
            encoding="utf-8"
        )
    )
    records = validate_document(document, validate_contract(contract))
    partitions = partition_records(contract, records)

    assert len(records) == 2
    product = next(row for row in records if row["canonical_path"] == "/i/gmp-vt-p173")
    assert product["full_url"] == "https://www.genemedi.net/i/gmp-vt-p173"
    assert product["product_category_id"] == "VT_INFECTIOUS"
    assert product["include_in_product_report"] == "是"
    information = next(row for row in records if row["canonical_path"] == "/products")
    assert information["page_class"] == "information_page"
    assert information["product_category_id"] == ""
    assert information["mapping_rule_id"] == ""
    assert information["mapping_status"] == "not_applicable"
    assert information["include_in_product_report"] == "否"
    assert len(product["row_hash"]) == 64
    assert document["reconciliation"]["unique_url_keys"] == 2
    assert document["reconciliation"]["assigned_product_pages"] == 1
    assert partitions["product_tarmart"] == []
    assert [row["canonical_path"] for row in partitions["product_other"]] == [
        "/i/gmp-vt-p173"
    ]
    assert [row["canonical_path"] for row in partitions["information"]] == [
        "/products"
    ]


def test_page_product_document_rejects_changed_row_after_hashing() -> None:
    mapping = load_product_mapping(
        PROJECT_ROOT / "config" / "product_mappings" / "genemedi-net.yaml",
        "genemedi-net",
    )
    assert mapping is not None
    dimension = build_page_dimension(
        PageClassificationConfig("genemedi-net", "page-v1", {}),
        [_row("i/gmp-vt-p173", 1, "indexwithSideBa_product")],
    )
    document = build_page_product_document(
        site="genemedi-net",
        base_url="https://www.genemedi.net",
        page_dimension=dimension,
        product_mapping=mapping,
        source_generated_at="2026-08-24T09:04:05Z",
        source_sha256="a" * 64,
        source_rows=1,
    )
    contract = json.loads(
        (PROJECT_ROOT / "config" / "feishu_dashboard" / "v3" / "page_product_mapping_contract.json").read_text(
            encoding="utf-8"
        )
    )
    document["records"][0]["product_category"] = "changed"

    with pytest.raises(ValueError, match="row hash mismatch"):
        validate_document(document, contract)


def _row(path: str, page_id: int, template: str) -> dict[str, object]:
    return {
        "route_url": path,
        "route_page_id": page_id,
        "route_source": "pages",
        "content_page_id": page_id,
        "template": template,
    }
