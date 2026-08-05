"""JSON-safe workbook payloads for the Artifact Tool XLSX builder."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from website_analytics.url_safety import sanitize_url_query


DETAIL_SHEET_ORDER = (
    "GA4 Daily",
    "GA4 Pages",
    "GSC Daily",
    "GSC Pages",
    "GSC Queries",
)


def build_workbook_payload(
    report: Mapping[str, Any],
    details: Mapping[str, Sequence[Mapping[str, Any]]],
    audit: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a deterministic, JSON-safe description of an analytics workbook."""
    _validate_detail_names(details)
    site = _text(report.get("display_name") or report.get("site"))
    date_range = _date_range_text(report.get("date_range"))
    freshness = _freshness_text(report.get("freshness") or audit.get("generated_at"))
    source_names = _source_names(details, audit)

    sheets: list[dict[str, Any]] = [
        _sheet(
            "README",
            "readme",
            [
                ["Website Analytics Report"],
                ["Site", site],
                ["Date range", date_range],
                ["Freshness", freshness],
                ["Sources", ", ".join(source_names)],
            ],
        ),
        _sheet(
            "Executive Summary",
            "summary",
            _summary_rows(site, date_range, freshness, report.get("comparison")),
        ),
    ]

    for detail_name in DETAIL_SHEET_ORDER:
        if detail_name in details:
            sheets.append(
                _sheet(
                    detail_name,
                    "detail",
                    _detail_rows(details[detail_name]),
                    detail=True,
                )
            )

    sheets.append(
        _sheet(
            "Audit",
            "audit",
            _audit_rows(site, date_range, freshness, audit),
        )
    )
    payload = {"sheets": sheets}
    json.dumps(payload, allow_nan=False)
    return payload


def _sheet(
    name: str, kind: str, rows: list[list[Any]], *, detail: bool = False
) -> dict[str, Any]:
    return {"name": name, "kind": kind, "detail": detail, "rows": rows}


def _summary_rows(
    site: str, date_range: str, freshness: str, comparison: object
) -> list[list[Any]]:
    rows: list[list[Any]] = [
        ["Executive Summary"],
        ["Site", site],
        ["Date range", date_range],
        ["Freshness", freshness],
        [],
        ["Source", "Metric", "Current", "Previous", "Delta"],
    ]
    metrics = comparison.get("metrics", {}) if isinstance(comparison, Mapping) else {}
    if not isinstance(metrics, Mapping):
        return rows
    for source in sorted(metrics):
        source_metrics = metrics[source]
        if not isinstance(source_metrics, Mapping):
            continue
        for metric in sorted(source_metrics):
            values = source_metrics[metric]
            if not isinstance(values, Mapping):
                continue
            rows.append(
                [
                    _json_value(source),
                    _json_value(metric),
                    _json_value(values.get("current")),
                    _json_value(values.get("previous")),
                    _json_value(values.get("delta")),
                ]
            )
    return rows


def _detail_rows(records: Sequence[Mapping[str, Any]]) -> list[list[Any]]:
    headers: list[str] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("detail rows must be mappings")
        for key in record:
            if not isinstance(key, str):
                raise ValueError("detail row keys must be strings")
            if key not in headers:
                headers.append(key)
    if not headers:
        return [["No rows returned"]]
    return [headers] + [
        [_json_value(record.get(header)) for header in headers] for record in records
    ]


def _audit_rows(
    site: str, date_range: str, freshness: str, audit: Mapping[str, Any]
) -> list[list[Any]]:
    rows: list[list[Any]] = [
        ["Audit Manifest"],
        ["Site", site],
        ["Date range", date_range],
        ["Generated at", _freshness_text(audit.get("generated_at") or freshness)],
        [],
        ["Source", "Status", "Rows", "Freshness"],
    ]
    sources = audit.get("sources", {})
    if not isinstance(sources, Mapping):
        return rows
    for source in sorted(sources):
        status = sources[source]
        if not isinstance(status, Mapping):
            status = {}
        rows.append(
            [
                _json_value(source),
                _json_value(status.get("status")),
                _json_value(status.get("rows", status.get("row_count"))),
                _freshness_text(status.get("freshness", status.get("generated_at"))),
            ]
        )
    return rows


def _source_names(
    details: Mapping[str, Sequence[Mapping[str, Any]]], audit: Mapping[str, Any]
) -> list[str]:
    sources = {
        "GA4" if detail_name.startswith("GA4") else "GSC"
        for detail_name in details
    }
    audit_sources = audit.get("sources", {})
    if isinstance(audit_sources, Mapping):
        sources.update(str(source).upper() for source in audit_sources)
    return sorted(sources)


def _date_range_text(value: object) -> str:
    if not isinstance(value, Mapping):
        return ""
    start = _text(value.get("start"))
    end = _text(value.get("end"))
    return f"{start} to {end}" if start and end else start or end


def _text(value: object) -> str:
    normalized = _json_value(value)
    return normalized if isinstance(normalized, str) else "" if normalized is None else str(normalized)


def _freshness_text(value: object) -> str:
    text = _text(value)
    if not text or text.startswith("Retrieved: "):
        return text
    if text.endswith("Z") and "T" in text:
        if text.endswith(":00Z"):
            text = f"{text[:-4]}Z"
        return f"Retrieved: {text[:-1].replace('T', ' ')} UTC"
    return f"Retrieved: {text}"


def _json_value(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return sanitize_url_query(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("workbook payload does not allow non-finite numbers")
        return value
    raise ValueError(f"workbook payload value is not JSON-safe: {type(value).__name__}")


def _validate_detail_names(details: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
    unknown = set(details) - set(DETAIL_SHEET_ORDER)
    if unknown:
        raise ValueError(f"unsupported detail sheets: {', '.join(sorted(unknown))}")
