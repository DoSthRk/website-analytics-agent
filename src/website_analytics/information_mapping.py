"""Versioned topic and content-type classification for information pages."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml


MatchField = Literal["path", "template"]
MatchType = Literal["exact", "path_prefix", "any_keyword"]
_MATCH_FIELDS = frozenset({"path", "template"})
_MATCH_TYPES = frozenset({"exact", "path_prefix", "any_keyword"})
_PAGE_METRICS = (
    "ga4Sessions",
    "gscClicks",
    "gscImpressions",
    "storedSubmissions",
    "quarantinedSubmissions",
    "nonQuarantinedSubmissions",
)


class InformationMappingError(ValueError):
    """Raised when an information-page mapping is not safe to apply."""


@dataclass(frozen=True)
class InformationDimensionValue:
    identifier: str
    name: str


@dataclass(frozen=True)
class InformationRule:
    identifier: str
    priority: int
    match_field: MatchField
    match_type: MatchType
    values: tuple[str, ...]
    target_id: str
    reason: str


@dataclass(frozen=True)
class InformationMapping:
    site_key: str
    version: str
    themes: tuple[InformationDimensionValue, ...]
    content_types: tuple[InformationDimensionValue, ...]
    theme_rules: tuple[InformationRule, ...]
    content_type_rules: tuple[InformationRule, ...]
    fallback_theme_id: str
    fallback_content_type_id: str


def load_information_mapping(
    path: str | Path, site_key: str
) -> InformationMapping | None:
    """Load an information-page mapping, or return ``None`` when absent."""
    mapping_path = Path(path)
    if not mapping_path.exists():
        return None
    try:
        with mapping_path.open(encoding="utf-8") as mapping_file:
            document = yaml.safe_load(mapping_file)
    except (OSError, yaml.YAMLError) as error:
        raise InformationMappingError("could not load information mapping") from error
    if not isinstance(document, Mapping):
        raise InformationMappingError("information mapping must be a mapping")
    _reject_unknown(
        document,
        {
            "version",
            "site",
            "themes",
            "content_types",
            "theme_rules",
            "content_type_rules",
            "fallback_theme_id",
            "fallback_content_type_id",
        },
        "root",
    )
    configured_site = _required_text(document, "site", "root")
    if configured_site != site_key:
        raise InformationMappingError(
            "information mapping site does not match selected site"
        )
    themes = _parse_dimension_values(document.get("themes"), "themes")
    content_types = _parse_dimension_values(
        document.get("content_types"), "content_types"
    )
    theme_ids = {value.identifier for value in themes}
    content_type_ids = {value.identifier for value in content_types}
    fallback_theme_id = _required_text(document, "fallback_theme_id", "root")
    fallback_content_type_id = _required_text(
        document, "fallback_content_type_id", "root"
    )
    if fallback_theme_id not in theme_ids:
        raise InformationMappingError("fallback_theme_id must reference themes")
    if fallback_content_type_id not in content_type_ids:
        raise InformationMappingError(
            "fallback_content_type_id must reference content_types"
        )
    return InformationMapping(
        site_key=site_key,
        version=_required_text(document, "version", "root"),
        themes=themes,
        content_types=content_types,
        theme_rules=_parse_rules(document.get("theme_rules"), theme_ids, "theme_rules"),
        content_type_rules=_parse_rules(
            document.get("content_type_rules"), content_type_ids, "content_type_rules"
        ),
        fallback_theme_id=fallback_theme_id,
        fallback_content_type_id=fallback_content_type_id,
    )


def classify_information_page(
    mapping: InformationMapping,
    *,
    path: str,
    template: str,
    page_class: str,
) -> dict[str, str]:
    """Return stable information dimensions without changing the page class."""
    if page_class != "information_page":
        return {
            "informationThemeId": "",
            "informationTheme": "",
            "informationContentTypeId": "",
            "informationContentType": "",
            "informationThemeRuleId": "",
            "informationContentTypeRuleId": "",
            "informationThemeStatus": "not_applicable",
            "informationContentTypeStatus": "not_applicable",
        }
    theme_rule = _match_rule(mapping.theme_rules, path, template)
    content_rule = _match_rule(mapping.content_type_rules, path, template)
    theme_id = theme_rule.target_id if theme_rule else mapping.fallback_theme_id
    content_type_id = (
        content_rule.target_id if content_rule else mapping.fallback_content_type_id
    )
    theme_names = {value.identifier: value.name for value in mapping.themes}
    content_type_names = {
        value.identifier: value.name for value in mapping.content_types
    }
    return {
        "informationThemeId": theme_id,
        "informationTheme": theme_names[theme_id],
        "informationContentTypeId": content_type_id,
        "informationContentType": content_type_names[content_type_id],
        "informationThemeRuleId": theme_rule.identifier if theme_rule else "",
        "informationContentTypeRuleId": content_rule.identifier if content_rule else "",
        "informationThemeStatus": "matched" if theme_rule else "fallback",
        "informationContentTypeStatus": "matched" if content_rule else "fallback",
    }


def build_information_report(
    mapping: InformationMapping,
    current_pages: Sequence[Mapping[str, Any]],
    previous_pages: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate information-page metrics by theme and content type."""
    return {
        "informationMappingVersion": mapping.version,
        "informationThemeLines": _dimension_lines(
            mapping.themes,
            current_pages,
            previous_pages,
            id_field="informationThemeId",
            id_output="themeId",
            name_output="theme",
        ),
        "informationContentTypeLines": _dimension_lines(
            mapping.content_types,
            current_pages,
            previous_pages,
            id_field="informationContentTypeId",
            id_output="contentTypeId",
            name_output="contentType",
        ),
        "informationClassificationCoverage": _coverage(current_pages),
        "informationPageMappings": [
            dict(page)
            for page in current_pages
            if page.get("pageClass") == "information_page"
        ],
    }


def _dimension_lines(
    values: Sequence[InformationDimensionValue],
    current_pages: Sequence[Mapping[str, Any]],
    previous_pages: Sequence[Mapping[str, Any]],
    *,
    id_field: str,
    id_output: str,
    name_output: str,
) -> list[dict[str, Any]]:
    current = _summarize(values, current_pages, id_field)
    previous = _summarize(values, previous_pages, id_field)
    lines: list[dict[str, Any]] = []
    for value in values:
        now = current[value.identifier]
        before = previous[value.identifier]
        lines.append(
            {
                id_output: value.identifier,
                name_output: value.name,
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
                "gscImpressionsDelta": (
                    now["gscImpressions"] - before["gscImpressions"]
                ),
                "storedSubmissionsCurrent": now["storedSubmissions"],
                "storedSubmissionsPrevious": before["storedSubmissions"],
                "nonQuarantinedSubmissionsCurrent": now[
                    "nonQuarantinedSubmissions"
                ],
                "nonQuarantinedSubmissionsPrevious": before[
                    "nonQuarantinedSubmissions"
                ],
            }
        )
    return lines


def _summarize(
    values: Sequence[InformationDimensionValue],
    pages: Sequence[Mapping[str, Any]],
    id_field: str,
) -> dict[str, dict[str, float | int]]:
    summary = {
        value.identifier: {
            "canonicalPages": 0,
            **{metric: 0.0 for metric in _PAGE_METRICS},
        }
        for value in values
    }
    for page in pages:
        identifier = page.get(id_field)
        if page.get("pageClass") != "information_page" or identifier not in summary:
            continue
        row = summary[str(identifier)]
        row["canonicalPages"] += 1
        for metric in _PAGE_METRICS:
            row[metric] += float(page.get(metric, 0.0))
    return summary


def _coverage(pages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    information_pages = [
        page for page in pages if page.get("pageClass") == "information_page"
    ]
    total = len(information_pages)
    theme_matched = sum(
        1 for page in information_pages if page.get("informationThemeStatus") == "matched"
    )
    content_matched = sum(
        1
        for page in information_pages
        if page.get("informationContentTypeStatus") == "matched"
    )
    return {
        "observedInformationPages": total,
        "explicitThemePages": theme_matched,
        "explicitThemeRate": theme_matched / total if total else None,
        "fallbackThemePages": total - theme_matched,
        "explicitContentTypePages": content_matched,
        "explicitContentTypeRate": content_matched / total if total else None,
        "fallbackContentTypePages": total - content_matched,
    }


def _match_rule(
    rules: Sequence[InformationRule], path: str, template: str
) -> InformationRule | None:
    normalized = {"path": path.casefold(), "template": template.casefold()}
    for rule in rules:
        value = normalized[rule.match_field]
        if rule.match_type == "exact" and value in rule.values:
            return rule
        if rule.match_type == "path_prefix" and any(
            value.startswith(candidate) for candidate in rule.values
        ):
            return rule
        if rule.match_type == "any_keyword" and any(
            candidate in value for candidate in rule.values
        ):
            return rule
    return None


def _parse_dimension_values(
    value: object, context: str
) -> tuple[InformationDimensionValue, ...]:
    if not isinstance(value, list) or not value:
        raise InformationMappingError(f"{context} must be a non-empty list")
    result: list[InformationDimensionValue] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        item_context = f"{context}[{index}]"
        if not isinstance(item, Mapping):
            raise InformationMappingError(f"{item_context} must be a mapping")
        _reject_unknown(item, {"id", "name"}, item_context)
        identifier = _required_text(item, "id", item_context)
        if identifier in seen:
            raise InformationMappingError(f"{context} IDs must be unique")
        seen.add(identifier)
        result.append(
            InformationDimensionValue(
                identifier=identifier,
                name=_required_text(item, "name", item_context),
            )
        )
    return tuple(result)


def _parse_rules(
    value: object, target_ids: set[str], context: str
) -> tuple[InformationRule, ...]:
    if not isinstance(value, list) or not value:
        raise InformationMappingError(f"{context} must be a non-empty list")
    rules: list[InformationRule] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        item_context = f"{context}[{index}]"
        if not isinstance(item, Mapping):
            raise InformationMappingError(f"{item_context} must be a mapping")
        _reject_unknown(
            item, {"id", "priority", "match", "target_id", "reason"}, item_context
        )
        identifier = _required_text(item, "id", item_context)
        if identifier in seen:
            raise InformationMappingError(f"{context} IDs must be unique")
        seen.add(identifier)
        priority = item.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
            raise InformationMappingError(
                f"{item_context} priority must be a non-negative integer"
            )
        target_id = _required_text(item, "target_id", item_context)
        if target_id not in target_ids:
            raise InformationMappingError(
                f"{item_context} target_id must reference its dimension"
            )
        match_field, match_type, values = _parse_match(
            item.get("match"), item_context
        )
        rules.append(
            InformationRule(
                identifier=identifier,
                priority=priority,
                match_field=match_field,
                match_type=match_type,
                values=values,
                target_id=target_id,
                reason=_required_text(item, "reason", item_context),
            )
        )
    return tuple(sorted(rules, key=lambda rule: (rule.priority, rule.identifier)))


def _parse_match(
    value: object, context: str
) -> tuple[MatchField, MatchType, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        raise InformationMappingError(f"{context} match must be a mapping")
    _reject_unknown(value, {"field", "type", "values"}, f"{context}.match")
    field = _required_text(value, "field", f"{context}.match")
    match_type = _required_text(value, "type", f"{context}.match")
    if field not in _MATCH_FIELDS:
        raise InformationMappingError(f"{context} has unsupported match field")
    if match_type not in _MATCH_TYPES:
        raise InformationMappingError(f"{context} has unsupported match type")
    raw_values = value.get("values")
    if not isinstance(raw_values, list) or not raw_values:
        raise InformationMappingError(
            f"{context} match values must be a non-empty list"
        )
    values = tuple(_value_text(item, context) for item in raw_values)
    return field, match_type, values  # type: ignore[return-value]


def _value_text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InformationMappingError(f"{context} match values must be strings")
    return value.strip().casefold()


def _required_text(value: Mapping[str, object], field: str, context: str) -> str:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw.strip():
        raise InformationMappingError(
            f"{context} field '{field}' must be a nonblank string"
        )
    return raw.strip()


def _reject_unknown(
    value: Mapping[object, object], allowed: set[str], context: str
) -> None:
    unexpected = sorted(str(key) for key in value if key not in allowed)
    if unexpected:
        raise InformationMappingError(
            f"{context} has unexpected field '{unexpected[0]}'"
        )
