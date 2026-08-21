"""Authoritative page classification built from the legacy pages routing dimension."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

import yaml


PageClass = Literal[
    "product_page",
    "information_page",
    "technical_page",
    "unknown_unmapped",
    "invalid_broken",
    "pdf_asset",
]
_OVERRIDABLE_CLASSES = frozenset({"product_page", "information_page"})
_ROUTE_SOURCE_CLASSES = frozenset({"product_page", "information_page"})
_PATH_RULE_CLASSES = frozenset(
    {"product_page", "information_page", "technical_page"}
)
_PATH_MATCH_TYPES = frozenset({"exact_path", "path_prefix"})


class PageClassificationError(ValueError):
    """Raised when the page dimension or its approved overrides are invalid."""


@dataclass(frozen=True)
class PageOverride:
    page_id: int
    page_class: PageClass
    reason: str


@dataclass(frozen=True)
class PageRouteAlias:
    identifier: str
    source_prefix: str
    target_prefix: str
    reason: str


@dataclass(frozen=True)
class RouteSourceRule:
    dbname: str
    page_class: PageClass
    template: str
    reason: str


@dataclass(frozen=True)
class PagePathRule:
    identifier: str
    match_type: str
    value: str
    page_class: PageClass
    template: str
    reason: str


@dataclass(frozen=True)
class PageClassificationConfig:
    site_key: str
    version: str
    overrides: Mapping[int, PageOverride]
    route_aliases: tuple[PageRouteAlias, ...] = ()
    route_sources: Mapping[str, RouteSourceRule] = field(default_factory=dict)
    path_rules: tuple[PagePathRule, ...] = ()


@dataclass(frozen=True)
class PageDimensionEntry:
    canonical_path: str
    page_id: int | None
    template: str
    page_class: PageClass
    classification_status: str
    classification_evidence: str
    has_orphan_route: bool = False


@dataclass(frozen=True)
class _RouteCandidate:
    route_page_id: int | None
    route_source: str
    content_page_id: int | None
    template: str


@dataclass(frozen=True)
class PageDimension:
    site_key: str
    version: str
    entries: Mapping[str, PageDimensionEntry]
    summary: Mapping[str, int]
    route_aliases: tuple[PageRouteAlias, ...] = ()
    path_rules: tuple[PagePathRule, ...] = ()

    def classify(self, value: object) -> PageDimensionEntry:
        path = canonical_page_path(value)
        if path is None:
            return _unknown_entry("[invalid-path]")
        if path.startswith("/pdf/"):
            return PageDimensionEntry(
                canonical_path=path,
                page_id=None,
                template="",
                page_class="pdf_asset",
                classification_status="asset",
                classification_evidence="URL path uses the approved /pdf/ asset prefix.",
            )
        entry = self.entries.get(path)
        if entry is not None:
            return entry
        for rule in self.path_rules:
            if not _path_rule_matches(path, rule):
                continue
            return PageDimensionEntry(
                canonical_path=path,
                page_id=None,
                template=rule.template,
                page_class=rule.page_class,
                classification_status="runtime_path_rule",
                classification_evidence=rule.reason,
            )
        alias_matches: list[tuple[PageRouteAlias, str, PageDimensionEntry]] = []
        for alias in self.route_aliases:
            target_path = _aliased_path(path, alias)
            if target_path is None:
                continue
            target = self.entries.get(target_path)
            if target is None or target.classification_status != "template_rule":
                continue
            alias_matches.append((alias, target_path, target))
        if len(alias_matches) != 1:
            return _unknown_entry(path)
        alias, target_path, target = alias_matches[0]
        return replace(
            target,
            canonical_path=path,
            classification_status="template_rule_via_route_alias",
            classification_evidence=(
                f"Approved route alias '{alias.identifier}' resolves to {target_path}. "
                f"{target.classification_evidence} Alias evidence: {alias.reason}"
            ),
        )


def load_page_classification(path: str | Path, site_key: str) -> PageClassificationConfig:
    """Load the reviewed page overrides for one registered site."""
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise PageClassificationError("could not load page classification") from error
    if not isinstance(document, Mapping):
        raise PageClassificationError("page classification must be a mapping")
    _reject_unknown(
        document,
        {
            "version",
            "site",
            "overrides",
            "route_aliases",
            "route_sources",
            "path_rules",
        },
        "root",
    )
    configured_site = _required_text(document, "site", "root")
    if configured_site != site_key:
        raise PageClassificationError("page classification site does not match selected site")
    version = _required_text(document, "version", "root")
    raw_overrides = document.get("overrides", [])
    if not isinstance(raw_overrides, list):
        raise PageClassificationError("page classification overrides must be a list")
    overrides: dict[int, PageOverride] = {}
    for index, raw in enumerate(raw_overrides):
        context = f"overrides[{index}]"
        if not isinstance(raw, Mapping):
            raise PageClassificationError(f"{context} must be a mapping")
        _reject_unknown(raw, {"page_id", "page_class", "reason"}, context)
        page_id = raw.get("page_id")
        if isinstance(page_id, bool) or not isinstance(page_id, int) or page_id < 0:
            raise PageClassificationError(f"{context} page_id must be a non-negative integer")
        if page_id in overrides:
            raise PageClassificationError("page classification override page IDs must be unique")
        page_class = _required_text(raw, "page_class", context)
        if page_class not in _OVERRIDABLE_CLASSES:
            raise PageClassificationError(f"{context} page_class is unsupported")
        overrides[page_id] = PageOverride(
            page_id=page_id,
            page_class=page_class,  # type: ignore[arg-type]
            reason=_required_text(raw, "reason", context),
        )
    raw_aliases = document.get("route_aliases", [])
    if not isinstance(raw_aliases, list):
        raise PageClassificationError("page classification route_aliases must be a list")
    route_aliases: list[PageRouteAlias] = []
    alias_ids: set[str] = set()
    source_prefixes: list[str] = []
    for index, raw in enumerate(raw_aliases):
        context = f"route_aliases[{index}]"
        if not isinstance(raw, Mapping):
            raise PageClassificationError(f"{context} must be a mapping")
        _reject_unknown(raw, {"id", "source_prefix", "target_prefix", "reason"}, context)
        identifier = _required_text(raw, "id", context)
        if identifier in alias_ids:
            raise PageClassificationError("page classification route alias IDs must be unique")
        source_prefix = _route_prefix(raw, "source_prefix", context)
        target_prefix = _route_prefix(raw, "target_prefix", context)
        if any(_prefixes_overlap(source_prefix, existing) for existing in source_prefixes):
            raise PageClassificationError("page classification route alias prefixes must not overlap")
        alias_ids.add(identifier)
        source_prefixes.append(source_prefix)
        route_aliases.append(
            PageRouteAlias(
                identifier=identifier,
                source_prefix=source_prefix,
                target_prefix=target_prefix,
                reason=_required_text(raw, "reason", context),
            )
        )
    route_sources = _parse_route_sources(document.get("route_sources", []))
    path_rules = _parse_path_rules(document.get("path_rules", []))
    return PageClassificationConfig(
        site_key=site_key,
        version=version,
        overrides=overrides,
        route_aliases=tuple(route_aliases),
        route_sources=route_sources,
        path_rules=path_rules,
    )


def _parse_route_sources(value: object) -> Mapping[str, RouteSourceRule]:
    if not isinstance(value, list):
        raise PageClassificationError("page classification route_sources must be a list")
    rules: dict[str, RouteSourceRule] = {}
    for index, raw in enumerate(value):
        context = f"route_sources[{index}]"
        if not isinstance(raw, Mapping):
            raise PageClassificationError(f"{context} must be a mapping")
        _reject_unknown(raw, {"dbname", "page_class", "template", "reason"}, context)
        dbname = _required_text(raw, "dbname", context)
        normalized = dbname.casefold()
        if normalized == "pages" or normalized in rules:
            raise PageClassificationError(
                "page classification route source names must be unique and exclude pages"
            )
        page_class = _required_text(raw, "page_class", context)
        if page_class not in _ROUTE_SOURCE_CLASSES:
            raise PageClassificationError(f"{context} page_class is unsupported")
        rules[normalized] = RouteSourceRule(
            dbname=dbname,
            page_class=page_class,  # type: ignore[arg-type]
            template=_required_text(raw, "template", context),
            reason=_required_text(raw, "reason", context),
        )
    return rules


def _parse_path_rules(value: object) -> tuple[PagePathRule, ...]:
    if not isinstance(value, list):
        raise PageClassificationError("page classification path_rules must be a list")
    rules: list[PagePathRule] = []
    identifiers: set[str] = set()
    for index, raw in enumerate(value):
        context = f"path_rules[{index}]"
        if not isinstance(raw, Mapping):
            raise PageClassificationError(f"{context} must be a mapping")
        _reject_unknown(raw, {"id", "match", "page_class", "template", "reason"}, context)
        identifier = _required_text(raw, "id", context)
        if identifier in identifiers:
            raise PageClassificationError("page classification path rule IDs must be unique")
        match = raw.get("match")
        if not isinstance(match, Mapping):
            raise PageClassificationError(f"{context} match must be a mapping")
        _reject_unknown(match, {"type", "value"}, f"{context}.match")
        match_type = _required_text(match, "type", f"{context}.match")
        if match_type not in _PATH_MATCH_TYPES:
            raise PageClassificationError(f"{context} match type is unsupported")
        path_value = _classification_path(
            _required_text(match, "value", f"{context}.match"), context
        )
        page_class = _required_text(raw, "page_class", context)
        if page_class not in _PATH_RULE_CLASSES:
            raise PageClassificationError(f"{context} page_class is unsupported")
        rule = PagePathRule(
            identifier=identifier,
            match_type=match_type,
            value=path_value,
            page_class=page_class,  # type: ignore[arg-type]
            template=_required_text(raw, "template", context),
            reason=_required_text(raw, "reason", context),
        )
        if any(_path_rules_overlap(rule, existing) for existing in rules):
            raise PageClassificationError("page classification path rules must not overlap")
        identifiers.add(identifier)
        rules.append(rule)
    return tuple(rules)


def build_page_dimension(
    config: PageClassificationConfig,
    source_rows: Sequence[Mapping[str, object]],
) -> PageDimension:
    """Build one deterministic path dimension from fixed route metadata."""
    grouped: dict[str, list[_RouteCandidate]] = defaultdict(list)
    for raw in source_rows:
        route_url = raw.get("route_url")
        if not isinstance(route_url, str):
            raise PageClassificationError("page dimension route URL must be text")
        path = canonical_page_path(route_url)
        if path is None:
            raise PageClassificationError("page dimension route URL is invalid")
        route_page_value = raw.get("route_page_id")
        route_page_id = (
            None
            if route_page_value is None
            else _page_id(route_page_value, "route page ID")
        )
        content_value = raw.get("content_page_id")
        content_page_id = (
            None if content_value is None else _page_id(content_value, "content page ID")
        )
        route_source_value = raw.get("route_source", "pages")
        if not isinstance(route_source_value, str) or not route_source_value.strip():
            raise PageClassificationError("page dimension route source must be text")
        template_value = raw.get("template")
        if template_value is not None and not isinstance(template_value, str):
            raise PageClassificationError("page dimension template must be text or null")
        grouped[path].append(
            _RouteCandidate(
                route_page_id=route_page_id,
                route_source=route_source_value.strip(),
                content_page_id=content_page_id,
                template=(template_value or "").strip(),
            )
        )

    entries: dict[str, PageDimensionEntry] = {}
    overrides_applied = 0
    orphan_routes = 0
    duplicate_paths = 0
    unapproved_route_source_rows = 0
    for path, candidates in grouped.items():
        if len(candidates) > 1:
            duplicate_paths += 1
        resolved: list[PageDimensionEntry] = []
        invalid_reasons: list[str] = []
        for candidate in candidates:
            source_key = candidate.route_source.casefold()
            if source_key == "pages":
                if candidate.content_page_id is None:
                    orphan_routes += 1
                    invalid_reasons.append("urltable route has no matching pages row.")
                    continue
                override = config.overrides.get(candidate.content_page_id)
                if override is not None:
                    page_class = override.page_class
                    status = "manual_override"
                    evidence = override.reason
                elif "sideba" in candidate.template.casefold():
                    page_class = "product_page"
                    status = "template_rule"
                    evidence = "pages.template contains SideBa/SideBar (case-insensitive)."
                else:
                    page_class = "information_page"
                    status = "template_rule"
                    evidence = "pages.template does not contain SideBa/SideBar."
                resolved.append(
                    PageDimensionEntry(
                        canonical_path=path,
                        page_id=candidate.content_page_id,
                        template=candidate.template,
                        page_class=page_class,
                        classification_status=status,
                        classification_evidence=evidence,
                    )
                )
                continue
            source_rule = config.route_sources.get(source_key)
            if source_rule is None:
                unapproved_route_source_rows += 1
                invalid_reasons.append(
                    f"urltable.dbname '{candidate.route_source}' has no approved route-source rule."
                )
                continue
            resolved.append(
                PageDimensionEntry(
                    canonical_path=path,
                    page_id=candidate.route_page_id,
                    template=source_rule.template,
                    page_class=source_rule.page_class,
                    classification_status="dynamic_route_rule",
                    classification_evidence=(
                        f"Approved urltable.dbname '{source_rule.dbname}' route. "
                        f"{source_rule.reason}"
                    ),
                )
            )
        if len(resolved) != 1:
            entries[path] = PageDimensionEntry(
                canonical_path=path,
                page_id=None,
                template="",
                page_class="invalid_broken",
                classification_status="invalid",
                classification_evidence=(
                    " ".join(dict.fromkeys(invalid_reasons))
                    if not resolved and invalid_reasons
                    else "URL resolves to multiple approved route records."
                ),
                has_orphan_route=bool(invalid_reasons),
            )
            continue
        entry = resolved[0]
        if entry.classification_status == "manual_override":
            overrides_applied += 1
        entries[path] = replace(entry, has_orphan_route=bool(invalid_reasons))

    class_counts = defaultdict(int)
    for entry in entries.values():
        class_counts[entry.page_class] += 1
    return PageDimension(
        site_key=config.site_key,
        version=config.version,
        entries=entries,
        summary={
            "sourceRows": len(source_rows),
            "canonicalPaths": len(entries),
            "productPages": class_counts["product_page"],
            "informationPages": class_counts["information_page"],
            "dynamicProductPages": sum(
                1
                for entry in entries.values()
                if entry.page_class == "product_page"
                and entry.classification_status == "dynamic_route_rule"
            ),
            "dynamicInformationPages": sum(
                1
                for entry in entries.values()
                if entry.page_class == "information_page"
                and entry.classification_status == "dynamic_route_rule"
            ),
            "invalidBrokenPaths": class_counts["invalid_broken"],
            "orphanRoutes": orphan_routes,
            "duplicatePaths": duplicate_paths,
            "unapprovedRouteSourceRows": unapproved_route_source_rows,
            "overridesApplied": overrides_applied,
            "routeAliasRules": len(config.route_aliases),
            "routeSourceRules": len(config.route_sources),
            "pathRules": len(config.path_rules),
        },
        route_aliases=config.route_aliases,
        path_rules=config.path_rules,
    )


def canonical_page_path(value: object) -> str | None:
    """Normalize GA4, GSC, inquiry, and legacy route values to one path key."""
    if not isinstance(value, str) or not value.strip():
        return "/" if value == "" else None
    text = value.strip()
    parts = urlsplit(text)
    path = parts.path
    if not path.startswith("/"):
        path = f"/{path}"
    normalized = unquote(path).casefold().rstrip("/")
    return normalized or "/"


def _unknown_entry(path: str) -> PageDimensionEntry:
    return PageDimensionEntry(
        canonical_path=path,
        page_id=None,
        template="",
        page_class="unknown_unmapped",
        classification_status="unmapped",
        classification_evidence="URL is absent from the approved urltable/pages dimension.",
    )


def _route_prefix(value: Mapping[str, object], field: str, context: str) -> str:
    raw = _required_text(value, field, context)
    if not raw.startswith("/") or raw.startswith("//") or "?" in raw or "#" in raw:
        raise PageClassificationError(f"{context} field '{field}' must be an absolute URL path prefix")
    normalized = unquote(raw).casefold()
    if normalized == "/":
        raise PageClassificationError(f"{context} field '{field}' cannot be the site root")
    return normalized


def _prefixes_overlap(left: str, right: str) -> bool:
    return left.startswith(right) or right.startswith(left)


def _aliased_path(path: str, alias: PageRouteAlias) -> str | None:
    if not path.startswith(alias.source_prefix):
        return None
    suffix = path[len(alias.source_prefix) :]
    if not suffix:
        return None
    return f"{alias.target_prefix}{suffix}"


def _classification_path(value: str, context: str) -> str:
    if not value.startswith("/") or value.startswith("//") or "?" in value or "#" in value:
        raise PageClassificationError(f"{context} match value must be an absolute URL path")
    normalized = canonical_page_path(value)
    if normalized is None or normalized == "/":
        raise PageClassificationError(f"{context} match value cannot be the site root")
    return normalized


def _path_rule_matches(path: str, rule: PagePathRule) -> bool:
    if rule.match_type == "exact_path":
        return path == rule.value
    return path == rule.value or path.startswith(f"{rule.value}/")


def _path_rules_overlap(left: PagePathRule, right: PagePathRule) -> bool:
    if left.match_type == "exact_path" and right.match_type == "exact_path":
        return left.value == right.value
    if left.match_type == "path_prefix" and right.match_type == "path_prefix":
        return (
            left.value == right.value
            or left.value.startswith(f"{right.value}/")
            or right.value.startswith(f"{left.value}/")
        )
    exact = left if left.match_type == "exact_path" else right
    prefix = right if left.match_type == "exact_path" else left
    return exact.value == prefix.value or exact.value.startswith(f"{prefix.value}/")


def _page_id(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PageClassificationError(f"page dimension {field} is invalid")
    return value


def _required_text(value: Mapping[str, object], field: str, context: str) -> str:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw.strip():
        raise PageClassificationError(f"{context} field '{field}' must be nonblank text")
    return raw.strip()


def _reject_unknown(value: Mapping[object, object], allowed: set[str], context: str) -> None:
    unexpected = sorted(str(key) for key in value if key not in allowed)
    if unexpected:
        raise PageClassificationError(f"{context} has unexpected field '{unexpected[0]}'")
