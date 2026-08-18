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
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml


RuleMatchType = Literal["exact_path", "path_prefix", "any_keyword", "all_keywords"]
_MATCH_TYPES = frozenset({"exact_path", "path_prefix", "any_keyword", "all_keywords"})
_PAGE_METRICS = ("ga4Sessions", "gscClicks", "gscImpressions")


class ProductMappingError(ValueError):
    """Raised when a versioned product mapping is not safe to apply."""


@dataclass(frozen=True)
class ReportLine:
    identifier: str
    name: str


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


def build_product_report(
    mapping: ProductMapping,
    current_details: Mapping[str, Sequence[Mapping[str, Any]]],
    previous_details: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Build current/previous product aggregates without mixing GA4 and GSC."""
    current_pages = _mapped_pages(mapping, current_details)
    previous_pages = _mapped_pages(mapping, previous_details)
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
    return {
        "mappingVersion": mapping.version,
        "reportLines": report_lines,
        "pageMappings": current_pages,
    }


def _mapped_pages(
    mapping: ProductMapping, details: Mapping[str, Sequence[Mapping[str, Any]]]
) -> list[dict[str, str | float | bool]]:
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

    mapped_pages: list[dict[str, str | float | bool]] = []
    for path, metrics in metrics_by_path.items():
        rule = _match_rule(mapping.rules, path)
        if rule is None:
            continue
        page_class = _page_class(path)
        include = rule.include_in_product_report and page_class == "product_page"
        mapped_pages.append(
            {
                "canonicalPath": path,
                "productLineId": rule.product_line_id,
                "reportLineId": rule.report_line_id or "",
                "pageClass": page_class,
                "includeInProductReport": include,
                "mappingRuleId": rule.identifier,
                "mappingStatus": rule.mapping_status,
                "mappingReason": rule.reason,
                "ga4Sessions": metrics["ga4Sessions"],
                "gscClicks": metrics["gscClicks"],
                "gscImpressions": metrics["gscImpressions"],
                "gscCtr": _ctr(metrics["gscClicks"], metrics["gscImpressions"]),
            }
        )
    return sorted(
        mapped_pages,
        key=lambda row: (
            not bool(row["includeInProductReport"]),
            -float(row["ga4Sessions"]),
            -float(row["gscClicks"]),
            str(row["canonicalPath"]),
        ),
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
        line_summary["canonicalPages"] += 1
        for metric in _PAGE_METRICS:
            line_summary[metric] += float(page[metric])
    return summary


def _match_rule(rules: Sequence[ProductRule], path: str) -> ProductRule | None:
    for rule in rules:
        if rule.match_type == "exact_path" and path in rule.values:
            return rule
        if rule.match_type == "path_prefix" and any(path.startswith(value) for value in rule.values):
            return rule
        if rule.match_type == "any_keyword" and any(value in path for value in rule.values):
            return rule
        if rule.match_type == "all_keywords" and all(value in path for value in rule.values):
            return rule
    return None


def _page_path(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parts = urlsplit(value.strip())
    path = parts.path
    if not path.startswith("/"):
        return None
    normalized = path.casefold().rstrip("/")
    return normalized or "/"


def _page_class(path: str) -> str:
    if path.startswith("/i/"):
        return "product_page"
    if path.startswith("/pdf/"):
        return "pdf_asset"
    return "content_page"


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
        _reject_unknown(item, {"id", "name"}, f"report_lines[{index}]")
        identifier = _required_text(item, "id", f"report_lines[{index}]")
        if identifier in seen:
            raise ProductMappingError("product mapping report line IDs must be unique")
        seen.add(identifier)
        lines.append(ReportLine(identifier=identifier, name=_required_text(item, "name", f"report_lines[{index}]")))
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
            {"id", "priority", "match", "product_line_id", "report_line_id", "include_in_product_report", "mapping_status", "reason"},
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


def _reject_unknown(value: Mapping[object, object], allowed: set[str], context: str) -> None:
    unexpected = sorted(str(key) for key in value if key not in allowed)
    if unexpected:
        raise ProductMappingError(f"{context} has unexpected field '{unexpected[0]}'")
