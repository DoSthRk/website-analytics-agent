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
    "Inquiry Daily",
    "Inquiry Pages",
)


def build_workbook_payload(
    report: Mapping[str, Any],
    details: Mapping[str, Sequence[Mapping[str, Any]]],
    audit: Mapping[str, Any],
    product_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic, JSON-safe description of an analytics workbook."""
    _validate_detail_names(details)
    site = _text(report.get("display_name") or report.get("site"))
    date_range = _date_range_text(report.get("date_range"))
    selection_timezone = _text(report.get("selection_timezone"))
    freshness = _freshness_text(report.get("freshness") or audit.get("generated_at"))
    source_names = _source_names(details, audit)

    sheets: list[dict[str, Any]] = [
        _sheet(
            "README",
            "readme",
            _readme_rows(
                site,
                date_range,
                selection_timezone,
                freshness,
                source_names,
                report.get("comparison"),
                product_report,
            ),
        ),
        _sheet(
            "Executive Summary",
            "summary",
            _summary_rows(
                site,
                date_range,
                selection_timezone,
                freshness,
                report.get("comparison"),
            ),
        ),
    ]

    if product_report is not None:
        sheets.extend(
            [
                _sheet(
                    "Product Weekly Summary",
                    "product_summary",
                    _product_summary_rows(
                        site,
                        date_range,
                        selection_timezone,
                        freshness,
                        product_report,
                    ),
                ),
                _sheet(
                    "Product Page Mapping",
                    "detail",
                    _product_page_mapping_rows(product_report),
                    detail=True,
                ),
            ]
        )
        if _has_inquiry_product_report(product_report):
            sheets.append(
                _sheet(
                    "Product Inquiry Summary",
                    "product_inquiry_summary",
                    _product_inquiry_summary_rows(
                        site,
                        date_range,
                        selection_timezone,
                        freshness,
                        product_report,
                    ),
                )
            )

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
            _audit_rows(
                site,
                date_range,
                selection_timezone,
                freshness,
                audit,
                report.get("comparison"),
            ),
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
    site: str,
    date_range: str,
    selection_timezone: str,
    freshness: str,
    comparison: object,
) -> list[list[Any]]:
    comparison_rows = _comparison_rows(comparison)
    rows: list[list[Any]] = [
        ["Executive Summary"],
        ["Site", site],
        ["Current date range", date_range],
        *_date_semantics_rows(selection_timezone),
        ["Freshness", freshness],
    ]
    if comparison_rows:
        rows.extend(comparison_rows)
    rows.append([])
    rows.append(
        ["Source", "Metric", "Current", "Previous", "Delta"]
        if comparison_rows
        else ["Source", "Metric", "Current"]
    )
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
            row = [
                _json_value(source),
                _json_value(metric),
                _json_value(values.get("current")),
            ]
            if comparison_rows:
                row.extend(
                    [
                        _json_value(values.get("previous")),
                        _json_value(values.get("delta")),
                    ]
                )
            rows.append(row)
    return rows


def _product_summary_rows(
    site: str,
    date_range: str,
    selection_timezone: str,
    freshness: str,
    product_report: Mapping[str, Any],
) -> list[list[Any]]:
    report_lines = product_report.get("reportLines")
    if not isinstance(report_lines, Sequence):
        raise ValueError("product report must contain reportLines")
    headers = [
        "reportLineId",
        "reportLine",
        "currentCanonicalPages",
        "ga4SessionsCurrent",
        "ga4SessionsPrevious",
        "ga4SessionsDelta",
        "gscClicksCurrent",
        "gscClicksPrevious",
        "gscClicksDelta",
        "gscImpressionsCurrent",
        "gscImpressionsPrevious",
        "gscImpressionsDelta",
        "gscCtrCurrent",
        "gscCtrPrevious",
        "gscCtrDelta",
    ]
    rows: list[list[Any]] = [
        ["Product Weekly Summary"],
        ["Site", site],
        ["Current date range", date_range],
        ["Selection timezone", selection_timezone],
        ["Freshness", freshness],
        ["Mapping version", _text(product_report.get("mappingVersion"))],
        [
            "Scope",
            "Only approved mapped product pages are included; GA4 and GSC metrics remain separate.",
        ],
        [],
        headers,
    ]
    for report_line in report_lines:
        if not isinstance(report_line, Mapping):
            raise ValueError("product report lines must be mappings")
        rows.append(
            [
                _json_value(report_line.get(header))
                for header in headers
            ]
        )
    return rows


def _product_page_mappings(product_report: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    pages = product_report.get("pageMappings")
    if not isinstance(pages, Sequence) or isinstance(pages, (str, bytes)):
        raise ValueError("product report must contain pageMappings")
    if not all(isinstance(page, Mapping) for page in pages):
        raise ValueError("product page mappings must be mappings")
    return pages


def _product_page_mapping_rows(product_report: Mapping[str, Any]) -> list[list[Any]]:
    """Return a renderable mapping sheet even when the period has no matched pages."""
    pages = _product_page_mappings(product_report)
    if pages:
        return _detail_rows(pages)
    return [
        ["status"],
        [
            "No page-level GA4, GSC, or inquiry records in this export period matched the approved product rules."
        ],
    ]


def _has_inquiry_product_report(product_report: Mapping[str, Any]) -> bool:
    report_lines = product_report.get("inquiryReportLines")
    if report_lines is None:
        return False
    if not isinstance(report_lines, Sequence) or isinstance(report_lines, (str, bytes)):
        raise ValueError("product report inquiryReportLines must be a sequence")
    if not all(isinstance(report_line, Mapping) for report_line in report_lines):
        raise ValueError("product report inquiry report lines must be mappings")
    return bool(report_lines)


def _product_inquiry_summary_rows(
    site: str,
    date_range: str,
    selection_timezone: str,
    freshness: str,
    product_report: Mapping[str, Any],
) -> list[list[Any]]:
    report_lines = product_report.get("inquiryReportLines")
    if not isinstance(report_lines, Sequence) or isinstance(report_lines, (str, bytes)):
        raise ValueError("product report inquiryReportLines must be a sequence")
    headers = [
        "reportLineId",
        "reportLine",
        "currentInquiryPages",
        "storedSubmissionsCurrent",
        "storedSubmissionsPrevious",
        "storedSubmissionsDelta",
        "quarantinedSubmissionsCurrent",
        "quarantinedSubmissionsPrevious",
        "quarantinedSubmissionsDelta",
        "nonQuarantinedSubmissionsCurrent",
        "nonQuarantinedSubmissionsPrevious",
        "nonQuarantinedSubmissionsDelta",
    ]
    rows: list[list[Any]] = [
        ["Product Inquiry Summary"],
        ["Site", site],
        ["Current date range", date_range],
        ["Selection timezone", selection_timezone],
        ["Freshness", freshness],
        ["Mapping version", _text(product_report.get("mappingVersion"))],
        [
            "Scope",
            "Database submissions only. Non-quarantined is a legacy form-rule status, not a manual lead-quality decision.",
        ],
        [],
        headers,
    ]
    for report_line in report_lines:
        if not isinstance(report_line, Mapping):
            raise ValueError("product report inquiry report lines must be mappings")
        rows.append([_json_value(report_line.get(header)) for header in headers])
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
    site: str,
    date_range: str,
    selection_timezone: str,
    freshness: str,
    audit: Mapping[str, Any],
    comparison: object,
) -> list[list[Any]]:
    rows: list[list[Any]] = [
        ["Audit Manifest"],
        ["Site", site],
        ["Current date range", date_range],
        *_date_semantics_rows(selection_timezone),
        ["Generated at", _freshness_text(audit.get("generated_at") or freshness)],
    ]
    rows.extend(_comparison_rows(comparison))
    rows.extend([[], ["Source", "Status", "Rows", "Freshness"]])
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


def _readme_rows(
    site: str,
    date_range: str,
    selection_timezone: str,
    freshness: str,
    source_names: Sequence[str],
    comparison: object,
    product_report: Mapping[str, Any] | None,
) -> list[list[Any]]:
    rows: list[list[Any]] = [
        ["Website Analytics Report"],
        ["Site", site],
        ["Current date range", date_range],
        *_date_semantics_rows(selection_timezone),
        ["Freshness", freshness],
        ["Sources", ", ".join(source_names)],
    ]
    rows.extend(_comparison_rows(comparison))
    rows.extend([
        [],
        ["Metric semantics"],
        ["GA4 sessions", "Visits/session starts tracked by GA4."],
        [
            "GA4 users",
            "Users are unique within the selected interval; interval aggregates are not daily sums.",
        ],
        ["GA4 key events", "Configured GA4 key-event count."],
        ["GSC clicks", "Google Search result clicks."],
        ["GSC impressions", "Google Search result impressions."],
        ["GSC CTR", "Clicks divided by impressions."],
        ["GSC position", "Impression-weighted average search position."],
        ["Database stored submissions", "Form submissions successfully written to the website database."],
        [
            "Database non-quarantined submissions",
            "Stored submissions not marked SPAM_QUARANTINE by the legacy form rule.",
        ],
        [],
        ["Limitations"],
        [
            "GA4 vs GSC",
            "GA4 sessions are not GSC clicks; the platforms measure different actions.",
        ],
        [
            "GA4 user aggregation",
            "Users are unique within an interval; do not add daily user values.",
        ],
        [
            "GSC detail scope",
            "GSC page and query rows can be bounded or capped; partial reports are not exhaustive.",
        ],
        [
            "Inquiry source boundary",
            "Database inquiry records are distinct from GA4 key events; legacy server date boundaries can differ from GA4 and GSC.",
        ],
    ])
    if product_report is not None:
        rows.extend(
            [
                [],
                ["Product mapping"],
                [
                    "Product weekly summary",
                    "Uses approved mapping rules and only includes matched product pages; GA4 and GSC metrics remain separate.",
                ],
                ["Product mapping version", _text(product_report.get("mappingVersion"))],
            ]
        )
    return rows


def _comparison_rows(comparison: object) -> list[list[Any]]:
    if not isinstance(comparison, Mapping):
        return []
    kind = _text(comparison.get("kind"))
    previous_date_range = _date_range_text(comparison.get("date_range"))
    if not kind or not previous_date_range:
        return []
    rows: list[list[Any]] = [
        ["Comparison kind", kind],
        ["Previous date range", previous_date_range],
    ]
    freshness = _freshness_text(comparison.get("freshness"))
    if freshness:
        rows.append(["Comparison freshness", freshness])
    status = _completion_text(comparison.get("complete"))
    if status:
        rows.append(["Comparison status", status])
    previous_complete = _completion_text(comparison.get("previous_complete"))
    if previous_complete:
        rows.append(["Previous source completeness", previous_complete])
    source_coverage = _completion_text(comparison.get("source_coverage_complete"))
    if source_coverage:
        rows.append(["Source coverage", source_coverage])
    metric_coverage = _completion_text(comparison.get("metric_coverage_complete"))
    if metric_coverage:
        rows.append(["Metric coverage", metric_coverage])
    source_statuses = _source_status_text(comparison.get("sources"))
    if source_statuses:
        rows.append(["Previous source status", source_statuses])
    return rows


def _completion_text(value: object) -> str:
    if value is True:
        return "Complete"
    if value is False:
        return "Partial"
    return ""


def _source_status_text(value: object) -> str:
    if not isinstance(value, Mapping):
        return ""
    statuses = []
    for source in sorted(value):
        status = value[source]
        label = _text(status.get("status")) if isinstance(status, Mapping) else ""
        statuses.append(f"{str(source).upper()}: {label or 'unknown'}")
    return "; ".join(statuses)


def _date_semantics_rows(selection_timezone: str) -> list[list[Any]]:
    return [
        ["Selection timezone", selection_timezone],
        [
            "Selection convention",
            "Local convention only for relative dates; explicit ISO dates are passed unchanged.",
        ],
        [
            "GA4 date boundary",
            "GA4 uses its property reporting timezone; verify it matches selection timezone.",
        ],
        [
            "GSC date boundary",
            "GSC uses Pacific Time (PT; UTC-7/UTC-8); daily boundaries can differ.",
        ],
    ]


def _source_names(
    details: Mapping[str, Sequence[Mapping[str, Any]]], audit: Mapping[str, Any]
) -> list[str]:
    sources = {detail_name.split(" ", maxsplit=1)[0].upper() for detail_name in details}
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
