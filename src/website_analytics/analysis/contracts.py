"""Strict, bounded public contracts. No credentials, SQL or arbitrary URLs."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from website_analytics.analysis import CONTRACT_VERSION
from website_analytics.models import DateRange
from website_analytics.page_classification import canonical_page_path

DOMAINS = ("genemedi.net", "www.genemedi.net")
MAX_PAGES = 100
CHANNELS = frozenset({"Direct", "Organic Search", "Paid Search", "Referral", "Email",
                      "Organic Social", "Paid Social", "Display", "Organic Video",
                      "Paid Video", "Organic Shopping", "Paid Shopping", "Affiliates",
                      "Audio", "SMS", "Mobile Push Notifications", "Cross-network", "Unassigned"})


class AnalysisError(ValueError):
    """Invalid or unsupported analysis request; messages contain no input secrets."""


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def strict_keys(value: object, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise AnalysisError("unsupported contract fields")
    return value


def text(value: object, *, limit: int = 1000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise AnalysisError("invalid text field")
    return value.strip()


def identifier(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", value):
        raise AnalysisError("invalid identifier")
    return value


def timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise AnalysisError("timestamp must include offset")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise AnalysisError("invalid timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AnalysisError("timestamp must include offset")
    return parsed


def calendar_date(value: object) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise AnalysisError("invalid calendar date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise AnalysisError("invalid calendar date") from None


def timezone_name(value: object) -> str:
    value = text(value, limit=100)
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        raise AnalysisError("unknown timezone") from None
    return value


def page_path(value: object) -> str:
    value = text(value, limit=1024)
    if any(c in value for c in ("?", "#", "\\")) or any(ord(c) < 32 for c in unquote(value)):
        raise AnalysisError("page URL must not contain query, fragment or control characters")
    try:
        url = urlsplit(value)
        if url.scheme:
            if url.scheme != "https" or url.hostname not in DOMAINS or url.username or url.password or url.port not in (None, 443):
                raise AnalysisError("page URL is outside the approved website")
        elif not value.startswith("/") or value.startswith("//") or url.netloc:
            raise AnalysisError("page must be a website path or approved HTTPS URL")
        decoded = unquote(url.path)
        if any(p in (".", "..") for p in decoded.split("/")) or "//" in decoded or any(c in decoded for c in ("\\", "?", "#")):
            raise AnalysisError("ambiguous page path")
        result = canonical_page_path(value)
    except ValueError:
        raise AnalysisError("invalid or out-of-scope page URL") from None
    if result is None:
        raise AnalysisError("invalid page path")
    return result


def page_set(value: object, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or not (0 if allow_empty else 1) <= len(value) <= MAX_PAGES:
        raise AnalysisError("page set must contain 1 to 100 paths")
    return tuple(sorted(set(page_path(x) for x in value)))


@dataclass(frozen=True)
class Selection:
    """Country/channel conditions apply to GA4 only; page intersection to all."""
    countries: tuple[str, ...] = ()
    channels: tuple[str, ...] = ()
    pages: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: object) -> Selection:
        value = strict_keys(value, set(), {"countries", "channels", "pages"})
        countries = value.get("countries", [])
        channels = value.get("channels", [])
        if not isinstance(countries, list) or len(countries) > 20 or any(not isinstance(x, str) or not re.fullmatch(r"[A-Z]{2}", x) for x in countries):
            raise AnalysisError("countries must be at most 20 ISO alpha-2 codes")
        if not isinstance(channels, list) or len(channels) > 20 or any(x not in CHANNELS for x in channels if isinstance(x, str)) or any(not isinstance(x, str) for x in channels):
            raise AnalysisError("unsupported GA4 session channel group")
        return cls(tuple(sorted(set(countries))), tuple(sorted(set(channels))), page_set(value.get("pages", []), allow_empty=True))

    def document(self) -> dict:
        return {k: list(v) for k, v in asdict(self).items()}


@dataclass(frozen=True)
class BatchDraft:
    batch_id: str
    revision: int
    previous_digest: str | None
    title: str
    description: str
    pages: tuple[str, ...]
    published_at: str
    published_timezone: str
    control_pages: tuple[str, ...] = ()
    control_rationale: str | None = None
    sample: bool = False

    @classmethod
    def parse(cls, raw: object) -> BatchDraft:
        value = strict_keys(raw, {"contract", "site", "batch_id", "revision", "previous_digest", "title", "description", "pages", "published_at", "published_timezone", "sample"}, {"control_pages", "control_rationale"})
        if value["contract"] != CONTRACT_VERSION or value["site"] != "genemedi-net":
            raise AnalysisError("unsupported contract or website")
        revision = value["revision"]
        prior = value["previous_digest"]
        if type(revision) is not int or revision < 1 or (revision == 1 and prior is not None) or (revision > 1 and (not isinstance(prior, str) or not re.fullmatch(r"[0-9a-f]{64}", prior))):
            raise AnalysisError("revision must reference its preceding confirmed digest")
        if type(value["sample"]) is not bool:
            raise AnalysisError("sample flag must be explicit")
        zone = timezone_name(value["published_timezone"])
        instant = timestamp(value["published_at"])
        if instant.astimezone(ZoneInfo(zone)).utcoffset() != instant.utcoffset():
            raise AnalysisError("published offset does not match the named timezone")
        pages = page_set(value["pages"])
        controls = page_set(value.get("control_pages", []), allow_empty=True)
        rationale = value.get("control_rationale")
        if set(pages) & set(controls):
            raise AnalysisError("changed and control page sets must be disjoint")
        if controls:
            rationale = text(rationale)
        elif rationale is not None:
            raise AnalysisError("control rationale requires control pages")
        return cls(identifier(value["batch_id"]), revision, prior, text(value["title"], limit=200), text(value["description"]), pages, instant.isoformat(), zone, controls, rationale, value["sample"])

    def document(self) -> dict:
        value = asdict(self)
        value.update(contract=CONTRACT_VERSION, site="genemedi-net")
        value["pages"], value["control_pages"] = list(self.pages), list(self.control_pages)
        return value

    @property
    def digest(self) -> str:
        return fingerprint(self.document())


@dataclass(frozen=True)
class Query:
    batch_id: str
    revision: int
    before: DateRange
    after: DateRange
    selection: Selection
    include_control: bool

    def document(self) -> dict:
        return {"contract": CONTRACT_VERSION, "site": "genemedi-net", "batch_id": self.batch_id,
                "revision": self.revision, "before": {"start": self.before.start.isoformat(), "end": self.before.end.isoformat()},
                "after": {"start": self.after.start.isoformat(), "end": self.after.end.isoformat()},
                "selection": self.selection.document(), "include_control": self.include_control}

    @classmethod
    def parse(cls, raw: object, batch: BatchDraft) -> Query:
        value = strict_keys(raw, {"contract", "site", "batch_id", "revision"}, {"window_days", "before", "after", "selection", "include_control"})
        if value["contract"] != CONTRACT_VERSION or value["site"] != "genemedi-net" or value["batch_id"] != batch.batch_id or type(value["revision"]) is not int or value["revision"] != batch.revision:
            raise AnalysisError("query must pin a confirmed batch revision")
        if type(value.get("include_control", False)) is not bool:
            raise AnalysisError("include_control must be boolean")
        if value.get("include_control") and not batch.control_pages:
            raise AnalysisError("batch has no confirmed controls")
        launch = timestamp(batch.published_at).astimezone(ZoneInfo(batch.published_timezone)).date()
        if "before" in value or "after" in value:
            if "window_days" in value or not {"before", "after"} <= value.keys():
                raise AnalysisError("supply both explicit periods or window_days")
            periods = []
            for label in ("before", "after"):
                period = strict_keys(value[label], {"start", "end"})
                periods.append(DateRange(calendar_date(period["start"]), calendar_date(period["end"])))
            before, after = periods
        else:
            days = value.get("window_days", 28)
            if type(days) is not int or not 1 <= days <= 90:
                raise AnalysisError("window_days must be 1 to 90")
            before = DateRange(launch - timedelta(days=days), launch - timedelta(days=1))
            after = DateRange(launch + timedelta(days=1), launch + timedelta(days=days))
        if before.start > before.end or after.start > after.end or before.end >= launch or after.start <= launch or (before.end-before.start).days != (after.end-after.start).days or (before.end-before.start).days >= 90:
            raise AnalysisError("periods must have equal 1 to 90 days, before/after launch; launch day is excluded")
        selection = Selection.parse(value.get("selection", {}))
        if selection.pages and not set(selection.pages) <= set(batch.pages):
            raise AnalysisError("page selection must be a subset of confirmed changed pages")
        return cls(batch.batch_id, batch.revision, before, after, selection, value.get("include_control", False))


def scope_document(period: DateRange, pages: tuple[str, ...], selection: Selection) -> dict:
    return {"site": "genemedi-net", "start": period.start.isoformat(), "end": period.end.isoformat(), "pages": list(pages), "countries": list(selection.countries), "channels": list(selection.channels)}


def metric_number(value: object) -> float:
    if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
        raise AnalysisError("metric must be a finite non-negative number")
    return float(value)
