"""Build additive daily facts for the date-selectable Feishu V3 dashboard."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any

from website_analytics.information_mapping import InformationMapping
from website_analytics.page_classification import PageDimension
from website_analytics.product_mapping import ProductMapping, build_product_report


DATE_BOUNDARY_NOTE = (
    "GA4按属性时区统计；GSC按Pacific Time统计；"
    "询盘按网站服务器日历统计。三者是独立数据源，不直接视为转化漏斗。"
)
_PAGE_METRICS = (
    "ga4Sessions",
    "gscClicks",
    "gscImpressions",
    "storedSubmissions",
    "nonQuarantinedSubmissions",
)


def build_v3_daily_records(
    *,
    site: str,
    data_date: date,
    fetch_result: Mapping[str, Any],
    details: Mapping[str, Sequence[Mapping[str, Any]]],
    product_mapping: ProductMapping,
    page_dimension: PageDimension,
    information_mapping: InformationMapping,
) -> dict[str, Any]:
    """Return one complete day of additive V3 facts without external writes.

    The caller must fetch exactly one calendar day with the approved CLI. This
    avoids introducing a new GA4/GSC dimension while still making every metric
    additive across a user-selected date range.
    """
    _require_complete_fetch(fetch_result)
    _require_single_day(details, data_date)
    refreshed_at = _freshness(fetch_result)
    totals = _totals(fetch_result)
    report = build_product_report(
        product_mapping,
        details,
        {},
        page_dimension,
        information_mapping,
    )
    daily_key = f"{site}|{data_date.isoformat()}"
    data_date_cell = data_date.isoformat()
    refreshed_cell = refreshed_at.astimezone(timezone.utc).isoformat()

    page_types = _index(report.get("pageTypeLines"), "pageTypeId")
    product_type = _required(page_types, "product_page")
    information_type = _required(page_types, "information_page")
    total_sessions = _source_number(totals, "ga4", "sessions")
    product_sessions = _number(product_type.get("ga4SessionsCurrent"))
    information_sessions = _number(information_type.get("ga4SessionsCurrent"))
    classified_sessions = product_sessions + information_sessions
    other_sessions = total_sessions - classified_sessions
    if other_sessions < 0:
        raise ValueError("classified page sessions exceed GA4 daily sessions")

    overview = {
        "daily_key": daily_key,
        "site": site,
        "data_date": data_date_cell,
        "data_status": "complete",
        "ga4_sessions": _whole(total_sessions),
        "ga4_key_events": _whole(
            _source_number(totals, "ga4", "keyEvents")
        ),
        "gsc_clicks": _whole(_source_number(totals, "gsc", "clicks")),
        "gsc_impressions": _whole(
            _source_number(totals, "gsc", "impressions")
        ),
        "stored_submissions": _whole(
            _source_number(totals, "inquiry", "storedSubmissions")
        ),
        "accepted_inquiries": _whole(
            _source_number(totals, "inquiry", "nonQuarantinedSubmissions")
        ),
        "product_page_sessions": _whole(product_sessions),
        "information_page_sessions": _whole(information_sessions),
        "other_page_sessions": _whole(other_sessions),
        "classified_page_sessions": _whole(classified_sessions),
        "page_classification_version": page_dimension.version,
        "product_mapping_version": product_mapping.version,
        "information_mapping_version": information_mapping.version,
        "refreshed_at": refreshed_cell,
        "source_boundary_note": DATE_BOUNDARY_NOTE,
    }

    report_lines = _index(report.get("reportLines"), "reportLineId")
    inquiry_lines = _index(report.get("inquiryReportLines"), "reportLineId")
    product_records: list[dict[str, Any]] = []
    for configured in product_mapping.report_lines:
        line = _required(report_lines, configured.identifier)
        inquiry = _required(inquiry_lines, configured.identifier)
        product_records.append(
            {
                "product_daily_key": f"{daily_key}|{configured.identifier}",
                "daily_key": daily_key,
                "site": site,
                "data_date": data_date_cell,
                "data_status": "complete",
                "product_line_id": configured.identifier,
                "product_name": configured.name,
                "category_l1": configured.category_l1,
                "category_l2": configured.category_l2,
                "category_l3": configured.category_l3,
                "ga4_sessions": _whole(line.get("ga4SessionsCurrent")),
                "gsc_clicks": _whole(line.get("gscClicksCurrent")),
                "gsc_impressions": _whole(line.get("gscImpressionsCurrent")),
                "stored_submissions": _whole(
                    inquiry.get("storedSubmissionsCurrent")
                ),
                "accepted_inquiries": _whole(
                    inquiry.get("nonQuarantinedSubmissionsCurrent")
                ),
                "included_pages": _whole(line.get("currentCanonicalPages")),
                "mapping_version": product_mapping.version,
                "refreshed_at": refreshed_cell,
            }
        )

    information_summary = _information_summary(
        report.get("informationPageMappings")
    )
    information_records: list[dict[str, Any]] = []
    for theme in information_mapping.themes:
        for content_type in information_mapping.content_types:
            key = (theme.identifier, content_type.identifier)
            metrics = information_summary.get(key, _empty_information_metrics())
            information_records.append(
                {
                    "information_daily_key": (
                        f"{daily_key}|{theme.identifier}|{content_type.identifier}"
                    ),
                    "daily_key": daily_key,
                    "site": site,
                    "data_date": data_date_cell,
                    "data_status": "complete",
                    "theme_id": theme.identifier,
                    "theme": theme.name,
                    "content_type_id": content_type.identifier,
                    "content_type": content_type.name,
                    "ga4_sessions": _whole(metrics["ga4Sessions"]),
                    "gsc_clicks": _whole(metrics["gscClicks"]),
                    "gsc_impressions": _whole(metrics["gscImpressions"]),
                    "stored_submissions": _whole(
                        metrics["storedSubmissions"]
                    ),
                    "accepted_inquiries": _whole(
                        metrics["nonQuarantinedSubmissions"]
                    ),
                    "included_pages": _whole(metrics["includedPages"]),
                    "mapping_version": information_mapping.version,
                    "refreshed_at": refreshed_cell,
                }
            )

    _require_product_totals(product_records, product_type)
    _require_information_totals(information_records, information_type)
    return {
        "schema_version": "3",
        "mode": "daily_dry_run",
        "write_enabled": False,
        "site": site,
        "data_date": data_date_cell,
        "records": {
            "overview_daily": [overview],
            "product_daily": product_records,
            "information_daily": information_records,
        },
    }


def _require_complete_fetch(result: Mapping[str, Any]) -> None:
    if result.get("status") != "ok" or result.get("complete") is not True:
        raise ValueError("V3 daily records require one complete three-source fetch")
    sources = result.get("sources")
    if not isinstance(sources, Mapping) or set(sources) != {
        "ga4",
        "gsc",
        "inquiry",
    }:
        raise ValueError("V3 daily records require GA4, GSC, and inquiry")
    if any(
        not isinstance(value, Mapping) or value.get("status") != "ok"
        for value in sources.values()
    ):
        raise ValueError("V3 daily records require all three sources to be ok")


def _require_single_day(
    details: Mapping[str, Sequence[Mapping[str, Any]]], expected: date
) -> None:
    expected_value = expected.isoformat()
    for collection in ("GA4 Daily", "GSC Daily", "Inquiry Daily"):
        for row in details.get(collection, ()):
            value = row.get("date")
            if value != expected_value:
                raise ValueError("V3 daily facts cannot contain another date")


def _freshness(result: Mapping[str, Any]) -> datetime:
    value = result.get("freshness")
    if not isinstance(value, str):
        raise ValueError("fetch freshness is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("fetch freshness must include a timezone")
    return parsed


def _totals(result: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    totals = result.get("totals")
    if not isinstance(totals, Mapping):
        raise ValueError("fetch totals are missing")
    normalized: dict[str, Mapping[str, Any]] = {}
    for source, metrics in totals.items():
        if not isinstance(source, str) or not isinstance(metrics, Mapping):
            raise ValueError("fetch totals are invalid")
        normalized[source] = metrics
    return normalized


def _source_number(
    totals: Mapping[str, Mapping[str, Any]], source: str, metric: str
) -> float:
    source_metrics = totals.get(source)
    if not isinstance(source_metrics, Mapping) or metric not in source_metrics:
        raise ValueError(f"fetch totals are missing {source}.{metric}")
    return _number(source_metrics[metric])


def _information_summary(
    value: object,
) -> dict[tuple[str, str], dict[str, float]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("information page mappings are unavailable")
    summary: dict[tuple[str, str], dict[str, float]] = defaultdict(
        _empty_information_metrics
    )
    observed_paths: dict[tuple[str, str], set[str]] = defaultdict(set)
    for page in value:
        if not isinstance(page, Mapping) or page.get("pageClass") != "information_page":
            raise ValueError("information page mapping is invalid")
        theme_id = page.get("informationThemeId")
        content_type_id = page.get("informationContentTypeId")
        path = page.get("canonicalPath")
        if not all(isinstance(item, str) and item for item in (theme_id, content_type_id, path)):
            raise ValueError("information page dimensions are invalid")
        key = (str(theme_id), str(content_type_id))
        row = summary[key]
        for metric in _PAGE_METRICS:
            row[metric] += _number(page.get(metric))
        observed_paths[key].add(str(path))
    for key, paths in observed_paths.items():
        summary[key]["includedPages"] = float(len(paths))
    return dict(summary)


def _empty_information_metrics() -> dict[str, float]:
    return {metric: 0.0 for metric in (*_PAGE_METRICS, "includedPages")}


def _require_product_totals(
    rows: Sequence[Mapping[str, Any]], page_type: Mapping[str, Any]
) -> None:
    _require_metric_totals(
        rows,
        page_type,
        {
            "ga4_sessions": "ga4SessionsCurrent",
            "gsc_clicks": "gscClicksCurrent",
            "gsc_impressions": "gscImpressionsCurrent",
            "stored_submissions": "storedSubmissionsCurrent",
            "accepted_inquiries": "nonQuarantinedSubmissionsCurrent",
        },
        "product",
    )


def _require_information_totals(
    rows: Sequence[Mapping[str, Any]], page_type: Mapping[str, Any]
) -> None:
    _require_metric_totals(
        rows,
        page_type,
        {
            "ga4_sessions": "ga4SessionsCurrent",
            "gsc_clicks": "gscClicksCurrent",
            "gsc_impressions": "gscImpressionsCurrent",
            "stored_submissions": "storedSubmissionsCurrent",
            "accepted_inquiries": "nonQuarantinedSubmissionsCurrent",
        },
        "information",
    )


def _require_metric_totals(
    rows: Sequence[Mapping[str, Any]],
    expected: Mapping[str, Any],
    metrics: Mapping[str, str],
    label: str,
) -> None:
    for row_metric, expected_metric in metrics.items():
        actual = sum(_number(row.get(row_metric)) for row in rows)
        if actual != _number(expected.get(expected_metric)):
            raise ValueError(f"{label} daily facts do not reconcile {row_metric}")


def _index(value: object, key: str) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("dashboard report collection is invalid")
    result: dict[str, Mapping[str, Any]] = {}
    for row in value:
        if not isinstance(row, Mapping) or not isinstance(row.get(key), str):
            raise ValueError("dashboard report row is invalid")
        result[str(row[key])] = row
    return result


def _required(
    values: Mapping[str, Mapping[str, Any]], key: str
) -> Mapping[str, Any]:
    value = values.get(key)
    if value is None:
        raise ValueError(f"dashboard report is missing {key}")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("metric must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("metric must be finite")
    return number


def _whole(value: object) -> int:
    return int(_number(value))
