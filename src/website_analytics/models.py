from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class InquirySourceConfig:
    """A fixed, read-only inquiry source approved for one registered site."""

    kind: str
    credential_env: str
    credential_target: str | None = None


@dataclass(frozen=True)
class SiteConfig:
    site_key: str
    display_name: str
    domains: tuple[str, ...]
    timezone: str
    ga4_property_id: str
    gsc_property_url: str
    key_events: tuple[str, ...] = ()
    inquiry_source: InquirySourceConfig | None = None


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date
