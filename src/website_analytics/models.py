from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class SiteConfig:
    site_key: str
    display_name: str
    domains: tuple[str, ...]
    timezone: str
    ga4_property_id: str
    gsc_property_url: str
    key_events: tuple[str, ...] = ()


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date
