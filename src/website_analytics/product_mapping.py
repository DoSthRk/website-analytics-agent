"""Approved product-line mapping for page-level GA4 and GSC exports.

The mapping is local, versioned, and deterministic. It never opens page URLs or
changes analytics source data; it only groups already-returned page dimensions.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml

from website_analytics.page_classification import (
    PageDimension,
    PageDimensionEntry,
    canonical_page_path,
)

if TYPE_CHECKING:
    from website_analytics.information_mapping import InformationMapping


RuleMatchType = Literal[
    "exact_path",
    "path_prefix",
    "any_keyword",
    "all_keywords",
    "page_class",
    "template_exact",
    "template_any_keyword",
]
_MATCH_TYPES = frozenset(
    {
        "exact_path",
        "path_prefix",
        "any_keyword",
        "all_keywords",
        "page_class",
        "template_exact",
        "template_any_keyword",
    }
)
_PAGE_METRICS = (
    "ga4Sessions",
    "gscClicks",
    "gscImpressions",
    "storedSubmissions",
    "quarantinedSubmissions",
    "nonQuarantinedSubmissions",
)
_INQUIRY_METRICS = (
    "storedSubmissions",
    "quarantinedSubmissions",
    "nonQuarantinedSubmissions",
)
_PAGE_TYPE_NAMES = {
    "product_page": "产品页",
    "information_page": "信息页",
    "technical_page": "技术页面",
    "unknown_unmapped": "未映射页面",
    "invalid_broken": "异常页面",
    "pdf_asset": "PDF资源",
}


class ProductMappingError(ValueError):
    """Raised when a versioned product mapping is not safe to apply."""


@dataclass(frozen=True)
class ReportLine:
    identifier: str
    name: str
    category_l1: str = ""
    category_l2: str = ""
    category_l3: str = ""


@dataclass(frozen=True)
class ProductRule:
    identifier: str
    priority: int
    match_type: RuleMatchType
    values: tuple[str, ...]
    product_line_id: str
    report_line_id: str | None
    include_in_product_report: bool
    mapping_status: str
    reason: str
    exclude_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProductMapping:
    site_key: str
    version: str
    report_lines: tuple[ReportLine, ...]
    rules: tuple[ProductRule, ...]


def load_product_mapping(path: str | Path, site_key: str) -> ProductMapping | None:
    """Load a mapping for a registered site, or return ``None`` when absent."""
    mapping_path = Path(path)
    if not mapping_path.exists():
        return None
    try:
        with mapping_path.open(encoding="utf-8") as mapping_file:
            document = yaml.safe_load(mapping_file)
    except (OSError, yaml.YAMLError) as error:
        raise ProductMappingError("could not load product mapping") from error
    if not isinstance(document, Mapping):
        raise ProductMappingError("product mapping must be a mapping")
    _reject_unknown(document, {"version", "site", "report_lines", "rules"}, "root")
    configured_site = _required_text(document, "site", "root")
    if configured_site != site_key:
        raise ProductMappingError("product mapping site does not match selected site")
    version = _required_text(document, "version", "root")
    report_lines = _parse_report_lines(document.get("report_lines"))
    rules = _parse_rules(document.get("rules"), {line.identifier for line in report_lines})
    return ProductMapping(
        site_key=site_key,
        version=version,
        report_lines=report_lines,
        rules=tuple(sorted(rules, key=lambda rule: (rule.priority, rule.identifier))),
    )


def match_product_rule(
    mapping: ProductMapping, path: str, page_class: str, template: str = ""
) -> ProductRule | None:
    """Return the first approved product rule for one canonical page path."""
    normalized_path = canonical_page_path(path)
    if normalized_path is None:
        return None
    return _match_rule(mapping.rules, normalized_path, page_class, template)


def build_product_report(
    mapping: ProductMapping,
    current_details: Mapping[str, Sequence[Mapping[str, Any]]],
    previous_details: Mapping[str, Sequence[Mapping[str, Any]]],
    page_dimension: PageDimension | None = None,
    information_mapping: InformationMapping | None = None,
) -> dict[str, Any]:
    """Build source-separated current/previous product aggregates."""
    current_pages = _mapped_pages(
        mapping, current_details, page_dimension, information_mapping
    )
    previous_pages = _mapped_pages(
        mapping, previous_details, page_dimension, information_mapping
    )
    current_summary = _summary_by_line(mapping, current_pages)
    previous_summary = _summary_by_line(mapping, previous_pages)

    report_lines: list[dict[str, str | int | float | None]] = []
    for line in mapping.report_lines:
        current = current_summary[line.identifier]
        previous = previous_summary[line.identifier]
        report_lines.append(
            {
                "reportLineId": line.identifier,
                "reportLine": line.name,
                "categoryL1": line.category_l1,
                "categoryL2": line.category_l2,
                "categoryL3": line.category_l3,
                "currentCanonicalPages": current["canonicalPages"],
                "ga4SessionsCurrent": current["ga4Sessions"],
                "ga4SessionsPrevious": previous["ga4Sessions"],
                "ga4SessionsDelta": current["ga4Sessions"] - previous["ga4Sessions"],
                "gscClicksCurrent": current["gscClicks"],
                "gscClicksPrevious": previous["gscClicks"],
                "gscClicksDelta": current["gscClicks"] - previous["gscClicks"],
                "gscImpressionsCurrent": current["gscImpressions"],
                "gscImpressionsPrevious": previous["gscImpressions"],
                "gscImpressionsDelta": current["gscImpressions"] - previous["gscImpressions"],
                "gscCtrCurrent": _ctr(current["gscClicks"], current["gscImpressions"]),
                "gscCtrPrevious": _ctr(previous["gscClicks"], previous["gscImpressions"]),
                "gscCtrDelta": _delta_ctr(current, previous),
            }
        )
    result = {
        "mappingVersion": mapping.version,
        "pageClassificationVersion": (
            page_dimension.version if page_dimension is not None else "unavailable"
        ),
        "pageDimensionSummary": (
            dict(page_dimension.summary) if page_dimension is not None else {}
        ),
        "reportLines": report_lines,
        "pageTypeLines": _page_type_report_lines(current_pages, previous_pages),
        "classificationCoverage": _classification_coverage(current_pages),
        "pageMappings": current_pages,
        "productPageMappings": [
            page for page in current_pages if page.get("pageClass") == "product_page"
        ],
        "inquiryReportLines": (
            _inquiry_report_lines(mapping, current_pages, previous_pages)
            if "Inquiry Pages" in current_details or "Inquiry Pages" in previous_details
            else []
        ),
    }
    if information_mapping is not None:
        from website_analytics.information_mapping import build_information_report

        result.update(
            build_information_report(
                information_mapping,
                current_pages,
                previous_pages,
            )
        )
    return result


def _mapped_pages(
    mapping: ProductMapping,
    details: Mapping[str, Sequence[Mapping[str, Any]]],
    page_dimension: PageDimension | None,
    information_mapping: InformationMapping | None,
) -> list[dict[str, Any]]:
    metrics_by_path: dict[str, dict[str, float]] = defaultdict(
        lambda: {metric: 0.0 for metric in _PAGE_METRICS}
    )
    for record in details.get("GA4 Pages", ()):
        path = _page_path(record.get("landingPagePlusQueryString"))
        if path:
            metrics_by_path[path]["ga4Sessions"] += _metric(record, "sessions")
    for record in details.get("GSC Pages", ()):
        path = _page_path(record.get("page"))
        if path:
            metrics_by_path[path]["gscClicks"] += _metric(record, "clicks")
            metrics_by_path[path]["gscImpressions"] += _metric(record, "impressions")
    for record in details.get("Inquiry Pages", ()):
        path = _page_path(record.get("sourceUrl"))
        if path:
            for metric in _INQUIRY_METRICS:
                metrics_by_path[path][metric] += _metric(record, metric)

    mapped_pages: list[dict[str, Any]] = []
    for path, metrics in metrics_by_path.items():
        page = (
            page_dimension.classify(path)
            if page_dimension is not None
            else _unavailable_page_dimension(path)
        )
        rule = _match_rule(mapping.rules, path, page.page_class, page.template)
        include = bool(
            rule is not None
            and rule.include_in_product_report
            and page.page_class == "product_page"
        )
        mapped_page = {
                "canonicalPath": path,
                "pageId": page.page_id if page.page_id is not None else "",
                "template": page.template,
                "pageClass": page.page_class,
                "classificationStatus": page.classification_status,
                "classificationEvidence": page.classification_evidence,
                "hasOrphanRoute": page.has_orphan_route,
                "productLineId": rule.product_line_id if rule is not None else "",
                "reportLineId": (rule.report_line_id or "") if rule is not None else "",
                "includeInProductReport": include,
                "mappingRuleId": rule.identifier if rule is not None else "",
                "mappingStatus": rule.mapping_status if rule is not None else "unmatched",
                "mappingReason": (
                    rule.reason
                    if rule is not None
                    else "No approved product-line rule matched this page."
                ),
                "ga4Sessions": metrics["ga4Sessions"],
                "gscClicks": metrics["gscClicks"],
                "gscImpressions": metrics["gscImpressions"],
                "gscCtr": _ctr(metrics["gscClicks"], metrics["gscImpressions"]),
                "storedSubmissions": metrics["storedSubmissions"],
                "quarantinedSubmissions": metrics["quarantinedSubmissions"],
                "nonQuarantinedSubmissions": metrics["nonQuarantinedSubmissions"],
        }
        if information_mapping is not None:
            from website_analytics.information_mapping import classify_information_page

            mapped_page.update(
                classify_information_page(
                    information_mapping,
                    path=path,
                    template=page.template,
                    page_class=page.page_class,
                )
            )
        mapped_pages.append(mapped_page)
    return sorted(
        mapped_pages,
        key=lambda row: (
            not bool(row["includeInProductReport"]),
            -float(row["ga4Sessions"]),
            -float(row["gscClicks"]),
            -float(row["storedSubmissions"]),
            str(row["canonicalPath"]),
        ),
    )


def _page_type_report_lines(
    current_pages: Sequence[Mapping[str, Any]],
    previous_pages: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    current = _summary_by_page_type(current_pages)
    previous = _summary_by_page_type(previous_pages)
    lines: list[dict[str, Any]] = []
    for page_type, name in _PAGE_TYPE_NAMES.items():
        now = current[page_type]
        before = previous[page_type]
        lines.append(
            {
                "pageTypeId": page_type,
                "pageType": name,
                "currentCanonicalPages": now["canonicalPages"],
                "previousCanonicalPages": before["canonicalPages"],
                "ga4SessionsCurrent": now["ga4Sessions"],
                "ga4SessionsPrevious": before["ga4Sessions"],
                "ga4SessionsDelta": now["ga4Sessions"] - before["ga4Sessions"],
                "gscClicksCurrent": now["gscClicks"],
                "gscClicksPrevious": before["gscClicks"],
                "gscClicksDelta": now["gscClicks"] - before["gscClicks"],
                "gscImpressionsCurrent": now["gscImpressions"],
                "gscImpressionsPrevious": before["gscImpressions"],
                "gscImpressionsDelta": now["gscImpressions"] - before["gscImpressions"],
                "gscCtrCurrent": _ctr(now["gscClicks"], now["gscImpressions"]),
                "gscCtrPrevious": _ctr(before["gscClicks"], before["gscImpressions"]),
                "storedSubmissionsCurrent": now["storedSubmissions"],
                "storedSubmissionsPrevious": before["storedSubmissions"],
                "nonQuarantinedSubmissionsCurrent": now["nonQuarantinedSubmissions"],
                "nonQuarantinedSubmissionsPrevious": before["nonQuarantinedSubmissions"],
            }
        )
    return lines


def _summary_by_page_type(
    pages: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, float | int]]:
    summary = {
        page_type: {
            "canonicalPages": 0,
            **{metric: 0.0 for metric in _PAGE_METRICS},
        }
        for page_type in _PAGE_TYPE_NAMES
    }
    for page in pages:
        page_type = page.get("pageClass")
        if page_type not in summary:
            continue
        values = summary[str(page_type)]
        values["canonicalPages"] += 1
        for metric in _PAGE_METRICS:
            values[metric] += float(page[metric])
    return summary


def _classification_coverage(pages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    classified = {"product_page", "information_page"}
    observed_sessions = sum(float(page["ga4Sessions"]) for page in pages)
    classified_sessions = sum(
        float(page["ga4Sessions"])
        for page in pages
        if page.get("pageClass") in classified
    )
    observed_clicks = sum(float(page["gscClicks"]) for page in pages)
    classified_clicks = sum(
        float(page["gscClicks"])
        for page in pages
        if page.get("pageClass") in classified
    )
    observed_inquiries = sum(float(page["nonQuarantinedSubmissions"]) for page in pages)
    classified_inquiries = sum(
        float(page["nonQuarantinedSubmissions"])
        for page in pages
        if page.get("pageClass") in classified
    )
    return {
        "observedCanonicalPages": len(pages),
        "classifiedCanonicalPages": sum(
            1 for page in pages if page.get("pageClass") in classified
        ),
        "unknownCanonicalPages": sum(
            1 for page in pages if page.get("pageClass") == "unknown_unmapped"
        ),
        "invalidCanonicalPages": sum(
            1 for page in pages if page.get("pageClass") == "invalid_broken"
        ),
        "ga4ObservedSessions": observed_sessions,
        "ga4ClassifiedSessions": classified_sessions,
        "ga4ClassifiedRate": _ratio(classified_sessions, observed_sessions),
        "gscObservedClicks": observed_clicks,
        "gscClassifiedClicks": classified_clicks,
        "gscClassifiedRate": _ratio(classified_clicks, observed_clicks),
        "inquiryObservedSubmissions": observed_inquiries,
        "inquiryClassifiedSubmissions": classified_inquiries,
        "inquiryClassifiedRate": _ratio(classified_inquiries, observed_inquiries),
    }


def _unavailable_page_dimension(path: str) -> PageDimensionEntry:
    return PageDimensionEntry(
        canonical_path=path,
        page_id=None,
        template="",
        page_class="unknown_unmapped",
        classification_status="dimension_unavailable",
        classification_evidence="Approved page dimension was not supplied.",
    )


def _summary_by_line(
    mapping: ProductMapping, pages: Sequence[Mapping[str, str | float | bool]]
) -> dict[str, dict[str, int | float]]:
    summary = {
        line.identifier: {"canonicalPages": 0, **{metric: 0.0 for metric in _PAGE_METRICS}}
        for line in mapping.report_lines
    }
    for page in pages:
        report_line_id = page.get("reportLineId")
        if not page.get("includeInProductReport") or report_line_id not in summary:
            continue
        line_summary = summary[str(report_line_id)]
        if (
            float(page["ga4Sessions"]) > 0
            or float(page["gscClicks"]) > 0
            or float(page["gscImpressions"]) > 0
        ):
            line_summary["canonicalPages"] += 1
        for metric in _PAGE_METRICS:
            line_summary[metric] += float(page[metric])
    return summary


def _inquiry_report_lines(
    mapping: ProductMapping,
    current_pages: Sequence[Mapping[str, str | float | bool]],
    previous_pages: Sequence[Mapping[str, str | float | bool]],
) -> list[dict[str, str | int | float]]:
    current = _inquiry_summary_by_line(mapping, current_pages)
    previous = _inquiry_summary_by_line(mapping, previous_pages)
    return [
        {
            "reportLineId": line.identifier,
            "reportLine": line.name,
            "currentInquiryPages": current[line.identifier]["pages"],
            "storedSubmissionsCurrent": current[line.identifier]["storedSubmissions"],
            "storedSubmissionsPrevious": previous[line.identifier]["storedSubmissions"],
            "storedSubmissionsDelta": current[line.identifier]["storedSubmissions"]
            - previous[line.identifier]["storedSubmissions"],
            "quarantinedSubmissionsCurrent": current[line.identifier]["quarantinedSubmissions"],
            "quarantinedSubmissionsPrevious": previous[line.identifier]["quarantinedSubmissions"],
            "quarantinedSubmissionsDelta": current[line.identifier]["quarantinedSubmissions"]
            - previous[line.identifier]["quarantinedSubmissions"],
            "nonQuarantinedSubmissionsCurrent": current[line.identifier]["nonQuarantinedSubmissions"],
            "nonQuarantinedSubmissionsPrevious": previous[line.identifier]["nonQuarantinedSubmissions"],
            "nonQuarantinedSubmissionsDelta": current[line.identifier]["nonQuarantinedSubmissions"]
            - previous[line.identifier]["nonQuarantinedSubmissions"],
        }
        for line in mapping.report_lines
    ]


def _inquiry_summary_by_line(
    mapping: ProductMapping, pages: Sequence[Mapping[str, str | float | bool]]
) -> dict[str, dict[str, int | float]]:
    summary = {
        line.identifier: {"pages": 0, **{metric: 0.0 for metric in _INQUIRY_METRICS}}
        for line in mapping.report_lines
    }
    for page in pages:
        report_line_id = page.get("reportLineId")
        if not page.get("includeInProductReport") or report_line_id not in summary:
            continue
        line_summary = summary[str(report_line_id)]
        if float(page["storedSubmissions"]) > 0:
            line_summary["pages"] += 1
        for metric in _INQUIRY_METRICS:
            line_summary[metric] += float(page[metric])
    return summary


def _match_rule(
    rules: Sequence[ProductRule], path: str, page_class: str, template: str
) -> ProductRule | None:
    normalized_template = template.casefold()
    for rule in rules:
        if any(value in path for value in rule.exclude_values):
            continue
        if rule.match_type == "exact_path" and path in rule.values:
            return rule
        if rule.match_type == "path_prefix" and any(path.startswith(value) for value in rule.values):
            return rule
        if rule.match_type == "any_keyword" and any(value in path for value in rule.values):
            return rule
        if rule.match_type == "all_keywords" and all(value in path for value in rule.values):
            return rule
        if rule.match_type == "page_class" and page_class.casefold() in rule.values:
            return rule
        if rule.match_type == "template_exact" and normalized_template in rule.values:
            return rule
        if rule.match_type == "template_any_keyword" and any(
            value in normalized_template for value in rule.values
        ):
            return rule
    return None


def _page_path(value: object) -> str | None:
    return canonical_page_path(value)


def _metric(record: Mapping[str, Any], field: str) -> float:
    value = record.get(field, 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProductMappingError(f"page metric '{field}' must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ProductMappingError(f"page metric '{field}' must be finite")
    return number


def _ctr(clicks: float, impressions: float) -> float | None:
    return clicks / impressions if impressions else None


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _delta_ctr(current: Mapping[str, int | float], previous: Mapping[str, int | float]) -> float | None:
    current_ctr = _ctr(float(current["gscClicks"]), float(current["gscImpressions"]))
    previous_ctr = _ctr(float(previous["gscClicks"]), float(previous["gscImpressions"]))
    if current_ctr is None or previous_ctr is None:
        return None
    return current_ctr - previous_ctr


def _parse_report_lines(value: object) -> tuple[ReportLine, ...]:
    if not isinstance(value, list) or not value:
        raise ProductMappingError("product mapping report_lines must be a non-empty list")
    lines: list[ReportLine] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ProductMappingError("product mapping report line must be a mapping")
        _reject_unknown(
            item,
            {"id", "name", "category_l1", "category_l2", "category_l3"},
            f"report_lines[{index}]",
        )
        identifier = _required_text(item, "id", f"report_lines[{index}]")
        if identifier in seen:
            raise ProductMappingError("product mapping report line IDs must be unique")
        seen.add(identifier)
        lines.append(
            ReportLine(
                identifier=identifier,
                name=_required_text(item, "name", f"report_lines[{index}]"),
                category_l1=_optional_text(item, "category_l1", f"report_lines[{index}]"),
                category_l2=_optional_text(item, "category_l2", f"report_lines[{index}]"),
                category_l3=_optional_text(item, "category_l3", f"report_lines[{index}]"),
            )
        )
    return tuple(lines)


def _parse_rules(value: object, report_line_ids: set[str]) -> list[ProductRule]:
    if not isinstance(value, list) or not value:
        raise ProductMappingError("product mapping rules must be a non-empty list")
    rules: list[ProductRule] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        context = f"rules[{index}]"
        if not isinstance(item, Mapping):
            raise ProductMappingError("product mapping rule must be a mapping")
        _reject_unknown(
            item,
            {"id", "priority", "match", "exclude_values", "product_line_id", "report_line_id", "include_in_product_report", "mapping_status", "reason"},
            context,
        )
        identifier = _required_text(item, "id", context)
        if identifier in seen:
            raise ProductMappingError("product mapping rule IDs must be unique")
        seen.add(identifier)
        priority = item.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
            raise ProductMappingError(f"{context} priority must be a non-negative integer")
        match_type, values = _parse_match(item.get("match"), context)
        exclude_values = _parse_optional_values(item.get("exclude_values"), context)
        report_line_id = item.get("report_line_id")
        if report_line_id is not None and (not isinstance(report_line_id, str) or report_line_id not in report_line_ids):
            raise ProductMappingError(f"{context} report_line_id must reference report_lines")
        include = item.get("include_in_product_report")
        if not isinstance(include, bool):
            raise ProductMappingError(f"{context} include_in_product_report must be boolean")
        if include and report_line_id is None:
            raise ProductMappingError(f"{context} reporting rules require report_line_id")
        status = _required_text(item, "mapping_status", context)
        if status != "approved":
            raise ProductMappingError(f"{context} mapping_status must be approved")
        rules.append(
            ProductRule(
                identifier=identifier,
                priority=priority,
                match_type=match_type,
                values=values,
                exclude_values=exclude_values,
                product_line_id=_required_text(item, "product_line_id", context),
                report_line_id=report_line_id,
                include_in_product_report=include,
                mapping_status=status,
                reason=_required_text(item, "reason", context),
            )
        )
    return rules


def _parse_match(value: object, context: str) -> tuple[RuleMatchType, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        raise ProductMappingError(f"{context} match must be a mapping")
    _reject_unknown(value, {"type", "values"}, f"{context}.match")
    match_type = _required_text(value, "type", f"{context}.match")
    if match_type not in _MATCH_TYPES:
        raise ProductMappingError(f"{context} has unsupported match type")
    raw_values = value.get("values")
    if not isinstance(raw_values, list) or not raw_values:
        raise ProductMappingError(f"{context} match values must be a non-empty list")
    values = tuple(_required_path_text(item, f"{context}.match") for item in raw_values)
    return match_type, values  # type: ignore[return-value]


def _required_path_text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProductMappingError(f"{context} values must be nonblank strings")
    return value.strip().casefold()


def _required_text(value: Mapping[str, object], field: str, context: str) -> str:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw.strip():
        raise ProductMappingError(f"{context} field '{field}' must be a nonblank string")
    return raw.strip()


def _optional_text(value: Mapping[str, object], field: str, context: str) -> str:
    raw = value.get(field, "")
    if not isinstance(raw, str):
        raise ProductMappingError(f"{context} field '{field}' must be a string")
    return raw.strip()


def _parse_optional_values(value: object, context: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not value:
        raise ProductMappingError(f"{context} exclude_values must be a non-empty list")
    return tuple(_required_path_text(item, context) for item in value)


def _reject_unknown(value: Mapping[object, object], allowed: set[str], context: str) -> None:
    unexpected = sorted(str(key) for key in value if key not in allowed)
    if unexpected:
        raise ProductMappingError(f"{context} has unexpected field '{unexpected[0]}'")
