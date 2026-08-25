"""Audited canonical-URL to product-category dimension for Feishu."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from website_analytics.page_classification import PageDimension
from website_analytics.product_mapping import ProductMapping, match_product_rule


SCHEMA_VERSION = "page-product-mapping.v1"


def validate_contract(contract: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate the standalone Feishu dimension-table contract."""

    if str(contract.get("version")) != "1":
        raise ValueError("page-product contract must use version 1")
    if contract.get("logical_name") != "page_product_mapping":
        raise ValueError("page-product contract logical name is invalid")
    display_name = contract.get("display_name")
    stable_key = contract.get("stable_key")
    fields = contract.get("fields")
    if not isinstance(display_name, str) or not display_name.strip():
        raise ValueError("page-product contract display name is required")
    if not isinstance(fields, list) or not fields:
        raise ValueError("page-product contract fields are required")
    names: set[str] = set()
    keys: set[str] = set()
    for field in fields:
        if not isinstance(field, Mapping):
            raise ValueError("page-product contract contains an invalid field")
        key = _text(field.get("key"), "field key")
        name = _text(field.get("name"), "field name")
        field_type = _text(field.get("type"), "field type")
        if field_type not in {"text", "url", "select", "datetime"}:
            raise ValueError(f"unsupported page-product field type: {field_type}")
        if key in keys or name in names:
            raise ValueError("page-product contract contains duplicate fields")
        keys.add(key)
        names.add(name)
    if stable_key != fields[0]["name"]:
        raise ValueError("page-product stable key must be the first field")
    partitions = contract.get("partitions")
    if not isinstance(partitions, list) or not partitions:
        raise ValueError("page-product contract partitions are required")
    partition_keys: set[str] = set()
    partition_names: set[str] = set()
    for partition in partitions:
        if not isinstance(partition, Mapping):
            raise ValueError("page-product contract contains an invalid partition")
        partition_key = _text(partition.get("key"), "partition key")
        partition_name = _text(partition.get("display_name"), "partition display name")
        page_classes = partition.get("page_classes")
        if (
            not isinstance(page_classes, list)
            or not page_classes
            or not all(isinstance(value, str) and value for value in page_classes)
        ):
            raise ValueError("page-product partition page_classes are invalid")
        category_l1 = partition.get("category_l1")
        excluded = partition.get("exclude_category_l1")
        if category_l1 is not None and excluded is not None:
            raise ValueError("page-product partition category filters conflict")
        for values in (category_l1, excluded):
            if values is not None and (
                not isinstance(values, list)
                or not values
                or not all(isinstance(value, str) and value for value in values)
            ):
                raise ValueError("page-product partition category filter is invalid")
        if partition_key in partition_keys or partition_name in partition_names:
            raise ValueError("page-product contract contains duplicate partitions")
        partition_keys.add(partition_key)
        partition_names.add(partition_name)
    return contract


def partition_records(
    contract: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    """Split every URL into exactly one bounded Feishu table partition."""

    validate_contract(contract)
    partitions = contract["partitions"]
    output: dict[str, list[Mapping[str, Any]]] = {
        str(partition["key"]): [] for partition in partitions
    }
    for record in records:
        matches: list[str] = []
        page_class = record.get("page_class")
        category_l1 = record.get("category_l1")
        for partition in partitions:
            if page_class not in partition["page_classes"]:
                continue
            included = partition.get("category_l1")
            excluded = partition.get("exclude_category_l1")
            if included is not None and category_l1 not in included:
                continue
            if excluded is not None and category_l1 in excluded:
                continue
            matches.append(str(partition["key"]))
        if len(matches) != 1:
            raise ValueError(
                f"page-product row must match exactly one partition: {record.get('url_key')}"
            )
        output[matches[0]].append(record)
    if sum(len(rows) for rows in output.values()) != len(records):
        raise AssertionError("page-product partition reconciliation failed")
    return output


def build_page_product_document(
    *,
    site: str,
    base_url: str,
    page_dimension: PageDimension,
    product_mapping: ProductMapping,
    source_generated_at: str,
    source_sha256: str,
    source_rows: int,
) -> dict[str, Any]:
    """Build one immutable row per canonical path without opening any URL."""

    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
        raise ValueError("base_url must be an HTTPS origin")
    origin = f"https://{parsed.hostname}"
    report_lines = {line.identifier: line for line in product_mapping.report_lines}
    rows: list[dict[str, Any]] = []
    page_classes: Counter[str] = Counter()
    product_categories: Counter[str] = Counter()
    mapping_statuses: Counter[str] = Counter()

    for path, page in sorted(page_dimension.entries.items()):
        rule = (
            match_product_rule(
                product_mapping,
                path,
                page.page_class,
                page.template,
            )
            if page.page_class == "product_page"
            else None
        )
        report_line = (
            report_lines.get(rule.report_line_id)
            if rule is not None and rule.report_line_id is not None
            else None
        )
        include = bool(
            rule is not None
            and rule.include_in_product_report
            and page.page_class == "product_page"
        )
        mapping_status = (
            rule.mapping_status
            if rule is not None
            else "unmatched" if page.page_class == "product_page" else "not_applicable"
        )
        row: dict[str, Any] = {
            "url_key": f"{site}|{path}",
            "site": site,
            "hostname": parsed.hostname,
            "full_url": f"{origin}{path}",
            "canonical_path": path,
            "page_id": "" if page.page_id is None else str(page.page_id),
            "template": page.template,
            "page_class": page.page_class,
            "classification_status": page.classification_status,
            "product_category_id": report_line.identifier if report_line else "",
            "product_category": report_line.name if report_line else "",
            "category_l1": report_line.category_l1 if report_line else "",
            "category_l2": report_line.category_l2 if report_line else "",
            "category_l3": report_line.category_l3 if report_line else "",
            "mapping_rule_id": rule.identifier if rule is not None else "",
            "mapping_status": mapping_status,
            "include_in_product_report": "是" if include else "否",
            "page_classification_version": page_dimension.version,
            "product_mapping_version": product_mapping.version,
            "source_snapshot_at": source_generated_at,
        }
        row["row_hash"] = _row_hash(row)
        rows.append(row)
        page_classes[page.page_class] += 1
        mapping_statuses[mapping_status] += 1
        if report_line is not None:
            product_categories[report_line.identifier] += 1

    stable_keys = {row["url_key"] for row in rows}
    if len(stable_keys) != len(rows):
        raise ValueError("page-product dimension contains duplicate URL keys")
    product_pages = page_classes["product_page"]
    assigned_product_pages = sum(
        1
        for row in rows
        if row["page_class"] == "product_page" and row["product_category_id"]
    )
    if assigned_product_pages != product_pages:
        raise ValueError(
            "approved product mapping does not cover every classified product page"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "passed",
        "site": site,
        "source": {
            "kind": "page_dimension_snapshot",
            "generated_at": source_generated_at,
            "sha256": source_sha256,
            "source_rows": source_rows,
        },
        "versions": {
            "page_classification": page_dimension.version,
            "product_mapping": product_mapping.version,
        },
        "reconciliation": {
            "status": "passed",
            "record_count": len(rows),
            "unique_url_keys": len(stable_keys),
            "product_pages": product_pages,
            "assigned_product_pages": assigned_product_pages,
            "page_classes": dict(sorted(page_classes.items())),
            "mapping_statuses": dict(sorted(mapping_statuses.items())),
            "product_categories": dict(sorted(product_categories.items())),
        },
        "records": rows,
    }


def validate_document(
    document: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Validate an audited mapping artifact before any Feishu write."""

    validate_contract(contract)
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("page-product document schema version is invalid")
    if document.get("status") != "passed":
        raise ValueError("page-product document status has not passed")
    reconciliation = document.get("reconciliation")
    if not isinstance(reconciliation, Mapping) or reconciliation.get("status") != "passed":
        raise ValueError("page-product reconciliation has not passed")
    records = document.get("records")
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        raise ValueError("page-product records are invalid")
    fields = contract["fields"]
    expected_keys = {str(field["key"]) for field in fields}
    stable_key = str(fields[0]["key"])
    stable_values: set[str] = set()
    for row in records:
        if set(row) != expected_keys:
            raise ValueError("page-product record fields differ from the contract")
        value = _text(row.get(stable_key), stable_key)
        if value in stable_values:
            raise ValueError(f"duplicate page-product stable key: {value}")
        stable_values.add(value)
        expected_hash = _row_hash({key: value for key, value in row.items() if key != "row_hash"})
        if row.get("row_hash") != expected_hash:
            raise ValueError(f"page-product row hash mismatch: {value}")
        if row.get("page_class") != "product_page" and any(
            row.get(key)
            for key in (
                "product_category_id",
                "product_category",
                "category_l1",
                "category_l2",
                "category_l3",
                "mapping_rule_id",
            )
        ):
            raise ValueError(f"non-product row contains product mapping: {value}")
    if reconciliation.get("record_count") != len(records):
        raise ValueError("page-product reconciled record count differs")
    if reconciliation.get("unique_url_keys") != len(stable_values):
        raise ValueError("page-product reconciled unique-key count differs")
    if reconciliation.get("assigned_product_pages") != reconciliation.get("product_pages"):
        raise ValueError("page-product reconciliation has unmapped product pages")
    return records


def _row_hash(row: Mapping[str, Any]) -> str:
    payload = json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()


def normalize_snapshot_datetime(value: str) -> str:
    """Normalize an ISO snapshot timestamp while preserving its instant."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("source snapshot timestamp is invalid") from error
    return parsed.isoformat().replace("+00:00", "Z")
