"""Authoritative page classification built from the legacy pages routing dimension."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

import yaml


PageClass = Literal[
    "product_page",
    "information_page",
    "unknown_unmapped",
    "invalid_broken",
    "pdf_asset",
]
_OVERRIDABLE_CLASSES = frozenset({"product_page", "information_page"})


class PageClassificationError(ValueError):
    """Raised when the page dimension or its approved overrides are invalid."""


@dataclass(frozen=True)
class PageOverride:
    page_id: int
    page_class: PageClass
    reason: str


@dataclass(frozen=True)
class PageClassificationConfig:
    site_key: str
    version: str
    overrides: Mapping[int, PageOverride]


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
class PageDimension:
    site_key: str
    version: str
    entries: Mapping[str, PageDimensionEntry]
    summary: Mapping[str, int]

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
        return entry if entry is not None else _unknown_entry(path)


def load_page_classification(path: str | Path, site_key: str) -> PageClassificationConfig:
    """Load the reviewed page overrides for one registered site."""
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise PageClassificationError("could not load page classification") from error
    if not isinstance(document, Mapping):
        raise PageClassificationError("page classification must be a mapping")
    _reject_unknown(document, {"version", "site", "overrides"}, "root")
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
    return PageClassificationConfig(
        site_key=site_key,
        version=version,
        overrides=overrides,
    )


def build_page_dimension(
    config: PageClassificationConfig,
    source_rows: Sequence[Mapping[str, object]],
) -> PageDimension:
    """Build one deterministic path dimension from fixed urltable/pages rows."""
    grouped: dict[str, list[tuple[int, int | None, str]]] = defaultdict(list)
    for raw in source_rows:
        route_url = raw.get("route_url")
        if not isinstance(route_url, str):
            raise PageClassificationError("page dimension route URL must be text")
        path = canonical_page_path(route_url)
        if path is None:
            raise PageClassificationError("page dimension route URL is invalid")
        route_page_id = _page_id(raw.get("route_page_id"), "route page ID")
        content_value = raw.get("content_page_id")
        content_page_id = (
            None if content_value is None else _page_id(content_value, "content page ID")
        )
        template_value = raw.get("template")
        if template_value is not None and not isinstance(template_value, str):
            raise PageClassificationError("page dimension template must be text or null")
        grouped[path].append((route_page_id, content_page_id, (template_value or "").strip()))

    entries: dict[str, PageDimensionEntry] = {}
    overrides_applied = 0
    orphan_routes = 0
    duplicate_paths = 0
    for path, candidates in grouped.items():
        valid = [candidate for candidate in candidates if candidate[1] is not None]
        invalid = [candidate for candidate in candidates if candidate[1] is None]
        orphan_routes += len(invalid)
        if len(candidates) > 1:
            duplicate_paths += 1
        if len(valid) != 1:
            entries[path] = PageDimensionEntry(
                canonical_path=path,
                page_id=None,
                template="",
                page_class="invalid_broken",
                classification_status="invalid",
                classification_evidence=(
                    "urltable route has no matching pages row."
                    if not valid
                    else "URL resolves to multiple pages rows."
                ),
                has_orphan_route=bool(invalid),
            )
            continue
        _, content_page_id, template = valid[0]
        assert content_page_id is not None
        override = config.overrides.get(content_page_id)
        if override is not None:
            page_class = override.page_class
            status = "manual_override"
            evidence = override.reason
            overrides_applied += 1
        elif "sideba" in template.casefold():
            page_class = "product_page"
            status = "template_rule"
            evidence = "pages.template contains SideBa/SideBar (case-insensitive)."
        else:
            page_class = "information_page"
            status = "template_rule"
            evidence = "pages.template does not contain SideBa/SideBar."
        entries[path] = PageDimensionEntry(
            canonical_path=path,
            page_id=content_page_id,
            template=template,
            page_class=page_class,
            classification_status=status,
            classification_evidence=evidence,
            has_orphan_route=bool(invalid),
        )

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
            "invalidBrokenPaths": class_counts["invalid_broken"],
            "orphanRoutes": orphan_routes,
            "duplicatePaths": duplicate_paths,
            "overridesApplied": overrides_applied,
        },
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
