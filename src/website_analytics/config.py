from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from website_analytics.models import SiteConfig


class ConfigError(ValueError):
    """Raised when a site configuration cannot be used safely."""


def load_sites(path: str | Path) -> dict[str, SiteConfig]:
    """Load and validate the registered sites from a YAML configuration file."""
    try:
        with Path(path).open(encoding="utf-8") as config_file:
            document = yaml.safe_load(config_file)
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"could not load site configuration: {error}") from error

    if not isinstance(document, Mapping):
        raise ConfigError("site configuration must be a mapping containing 'sites'")

    raw_sites = document.get("sites")
    if not isinstance(raw_sites, Mapping):
        raise ConfigError("site configuration field 'sites' must be a mapping")

    sites: dict[str, SiteConfig] = {}
    for raw_key, raw_site in raw_sites.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ConfigError("site keys must be nonblank strings")
        site_key = raw_key.strip()
        if not isinstance(raw_site, Mapping):
            raise ConfigError(f"site '{site_key}' must be a mapping")

        sites[site_key] = SiteConfig(
            site_key=site_key,
            display_name=_required_text(raw_site, site_key, "display_name"),
            domains=_required_list(raw_site, site_key, "domains"),
            timezone=_required_text(raw_site, site_key, "timezone"),
            ga4_property_id=_required_identifier(raw_site, site_key, "ga4_property_id"),
            gsc_property_url=_required_text(raw_site, site_key, "gsc_property_url"),
            key_events=_optional_list(raw_site, site_key, "key_events"),
        )

    return sites


def require_site(sites: Mapping[str, SiteConfig], key: str) -> SiteConfig:
    """Return a registered site or raise an actionable configuration error."""
    try:
        return sites[key]
    except KeyError as error:
        raise ConfigError(f"site '{key}' is not registered") from error


def _required_text(site: Mapping[str, Any], site_key: str, field: str) -> str:
    value = site.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"site '{site_key}' field '{field}' must be a nonblank string")
    return value.strip()


def _required_identifier(site: Mapping[str, Any], site_key: str, field: str) -> str:
    value = site.get(field)
    if isinstance(value, bool) or value is None:
        raise ConfigError(f"site '{site_key}' field '{field}' must be nonblank")
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value).strip()
    raise ConfigError(f"site '{site_key}' field '{field}' must be nonblank")


def _required_list(site: Mapping[str, Any], site_key: str, field: str) -> tuple[str, ...]:
    value = site.get(field)
    if not isinstance(value, list) or not value:
        raise ConfigError(f"site '{site_key}' field '{field}' must be a nonempty list")
    return _text_items(value, site_key, field)


def _optional_list(site: Mapping[str, Any], site_key: str, field: str) -> tuple[str, ...]:
    value = site.get(field, [])
    if not isinstance(value, list):
        raise ConfigError(f"site '{site_key}' field '{field}' must be a list")
    return _text_items(value, site_key, field)


def _text_items(values: list[Any], site_key: str, field: str) -> tuple[str, ...]:
    items: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"site '{site_key}' field '{field}' must contain only nonblank strings"
            )
        items.append(value.strip())
    return tuple(items)
