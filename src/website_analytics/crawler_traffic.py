"""Suspected crawler sessions on product landing pages, from GA4 fingerprints.

A fingerprint is session channel x country x city x screen resolution x
browser x operating system. For each day, a fingerprint counts as suspected
crawler traffic when, over the trailing window (that day and the days before
it), it has at least ``min_sessions`` sessions and an engaged-session rate at
or below ``max_engaged_rate``. Real visitors engage 55-65% of the time on
genemedi.net; dozens of sessions with almost no engagement are not people.
The window is trailing, not the calendar month, so early-month days are judged
on enough history and a finished day does not change when the month goes on.

The GA4 fingerprint report is saved per data date: the site-wide totals per
fingerprint (the rule looks at the whole site) and the product-page sessions
already mapped to report lines. Re-running the rule needs no GA4 reads; a URL
rule change needs the days fetched again, like the daily-facts store.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from website_analytics.daily_facts_store import _atomic_write
from website_analytics.page_classification import PageDimension, canonical_page_path
from website_analytics.product_mapping import ProductMapping, match_product_rule

FINGERPRINT_DIMENSIONS = (
    "sessionDefaultChannelGroup",
    "country",
    "city",
    "screenResolution",
    "browser",
    "operatingSystem",
)
REPORT_DIMENSIONS = ("date", "landingPagePlusQueryString", *FINGERPRINT_DIMENSIONS)

Fingerprint = tuple[str, ...]


@dataclass(frozen=True)
class CrawlerRule:
    version: str
    window_days: int
    min_sessions: float
    max_engaged_rate: float

    def flags(self, sessions: float, engaged: float) -> bool:
        return sessions >= self.min_sessions and engaged <= self.max_engaged_rate * sessions


def load_crawler_rule(path: Path, site: str) -> CrawlerRule:
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if document.get("site") != site:
        raise ValueError("crawler rule belongs to another site")
    rule = CrawlerRule(
        version=str(document.get("version") or ""),
        window_days=int(document["window_days"]),
        min_sessions=float(document["min_sessions"]),
        max_engaged_rate=float(document["max_engaged_rate"]),
    )
    if not rule.version or not 7 <= rule.window_days <= 62 or rule.min_sessions < 1 or not 0 <= rule.max_engaged_rate < 1:
        raise ValueError("crawler rule needs a version, a 7-62 day window, min_sessions >= 1 and a rate in [0, 1)")
    return rule


# ---------------------------------------------------------------- store


def fingerprint_days(
    rows: Iterable[Mapping[str, Any]],
    *,
    days: Sequence[date],
    mapping: ProductMapping,
    page_dimension: PageDimension,
    ga4_metadata: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Turn GA4 fingerprint report rows into one document per requested day.

    Only the canonical path is used to find the product line; query strings and
    paths are not stored.
    """
    wanted = {day.isoformat() for day in days}
    site: dict[str, dict[Fingerprint, list[float]]] = {day: defaultdict(lambda: [0.0, 0.0]) for day in wanted}
    product: dict[str, dict[tuple[str, ...], list[float]]] = {day: defaultdict(lambda: [0.0, 0.0]) for day in wanted}
    lines: dict[str, str | None] = {}
    for row in rows:
        day = str(row["date"])[:10]
        if day not in wanted:
            raise ValueError(f"fingerprint row outside the requested days: {day}")
        key = tuple(str(row[name]) for name in FINGERPRINT_DIMENSIONS)
        sessions, engaged = float(row["sessions"]), float(row["engagedSessions"])
        site[day][key][0] += sessions
        site[day][key][1] += engaged
        raw = str(row["landingPagePlusQueryString"])
        if raw not in lines:
            lines[raw] = _report_line(raw, mapping, page_dimension)
        line = lines[raw]
        if line is not None:
            product[day][(line, *key)][0] += sessions
            product[day][(line, *key)][1] += engaged
    meta = dict(ga4_metadata or {})
    return {
        day: {
            "data_date": day,
            "mapping_version": mapping.version,
            "page_classification_version": page_dimension.version,
            "ga4": meta,
            "fingerprints": [[*key, *_whole(values)] for key, values in sorted(site[day].items())],
            "product": [[*key, *_whole(values)] for key, values in sorted(product[day].items())],
        }
        for day in sorted(wanted)
    }


def _report_line(raw: str, mapping: ProductMapping, page_dimension: PageDimension) -> str | None:
    path = canonical_page_path(raw)
    if not path:
        return None
    page = page_dimension.classify(path)
    rule = match_product_rule(mapping, path, page.page_class, page.template)
    if rule is None or not rule.include_in_product_report or page.page_class != "product_page":
        return None
    return rule.report_line_id


def _whole(values: Sequence[float]) -> list[float | int]:
    return [int(value) if float(value).is_integer() else value for value in values]


def save_fingerprint_days(store: Path, documents: Mapping[str, Mapping[str, Any]], fetched_at: datetime) -> list[str]:
    store.mkdir(parents=True, exist_ok=True)
    for day, document in sorted(documents.items()):
        _atomic_write(store / f"{day}.json", {**document, "fetched_at": fetched_at.isoformat(timespec="seconds")})
    return sorted(documents)


def load_fingerprint_days(store: Path) -> dict[str, dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {}
    for path in sorted(Path(store).glob("????-??-??.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("data_date") != path.stem:
            raise ValueError(f"fingerprint file is mislabeled: {path.name}")
        documents[path.stem] = document
    return documents


# ---------------------------------------------------------------- rule


def crawler_sessions(
    documents: Mapping[str, Mapping[str, Any]],
    days: Iterable[date],
    rule: CrawlerRule,
    *,
    mapping_version: str,
) -> dict[date, dict[str, float]]:
    """Suspected crawler sessions per report line for each requested day.

    Every requested day must have a stored document made with the same product
    mapping; earlier days of a window that are not stored are simply absent
    (only the first weeks of history have a shorter window).
    """
    targets = sorted(set(days))
    if not targets:
        return {}
    missing = [day.isoformat() for day in targets if day.isoformat() not in documents]
    if missing:
        raise ValueError(f"crawler fingerprints are missing {len(missing)} days, first {missing[0]}")
    versions = {str(documents[day.isoformat()].get("mapping_version")) for day in targets}
    if versions != {mapping_version}:
        raise ValueError(f"crawler fingerprints use product mapping {sorted(versions)}, daily facts use {mapping_version}")

    site_by_day = {
        day: {tuple(entry[:-2]): (float(entry[-2]), float(entry[-1])) for entry in document["fingerprints"]}
        for day, document in documents.items()
    }
    window: dict[Fingerprint, list[float]] = defaultdict(lambda: [0.0, 0.0])

    def add(day: date, sign: int) -> None:
        for key, (sessions, engaged) in site_by_day.get(day.isoformat(), {}).items():
            totals = window[key]
            totals[0] += sign * sessions
            totals[1] += sign * engaged

    result: dict[date, dict[str, float]] = {}
    first = targets[0] - timedelta(days=rule.window_days - 1)
    day = first
    wanted = set(targets)
    while day <= targets[-1]:
        add(day, +1)
        if day - timedelta(days=rule.window_days) >= first:
            add(day - timedelta(days=rule.window_days), -1)
        if day in wanted:
            flagged = {key for key, (sessions, engaged) in window.items() if rule.flags(sessions, engaged)}
            per_line: dict[str, float] = defaultdict(float)
            for entry in documents[day.isoformat()]["product"]:
                if tuple(entry[1:-2]) in flagged:
                    per_line[str(entry[0])] += float(entry[-2])
            result[day] = dict(per_line)
        day += timedelta(days=1)
    return result


def load_crawler_sessions(
    store: Path, rule_path: Path, *, site: str, days: Iterable[date], mapping_version: str,
    extra: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[date, dict[str, float]]:
    """Stored fingerprint days (plus freshly fetched ``extra`` ones) run through the site rule."""
    documents = load_fingerprint_days(store) | dict(extra or {})
    return crawler_sessions(documents, days, load_crawler_rule(rule_path, site), mapping_version=mapping_version)


def monthly_summary(
    documents: Mapping[str, Mapping[str, Any]], crawler: Mapping[date, Mapping[str, float]],
) -> list[dict[str, Any]]:
    """Product landing sessions and suspected crawler share by month, for review."""
    months: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for day, per_line in crawler.items():
        document = documents[day.isoformat()]
        month = months[day.isoformat()[:7]]
        month[0] += sum(float(entry[-2]) for entry in document["product"])
        month[1] += sum(per_line.values())
        month[2] += 1
    return [
        {"month": month, "days": int(days), "product_sessions": round(total), "suspected_crawler": round(bot),
         "share": round(bot / total, 3) if total else None}
        for month, (total, bot, days) in sorted(months.items())
    ]
