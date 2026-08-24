"""Assemble and reconcile a bounded V3 backfill from approved local artifacts."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from website_analytics.dashboard_sync import load_period_details
from website_analytics.dashboard_v3 import build_v3_daily_records
from website_analytics.information_mapping import InformationMapping
from website_analytics.page_classification import PageDimension
from website_analytics.periods import AnalyticsPeriod
from website_analytics.product_mapping import ProductMapping


MAXIMUM_BACKFILL_DAYS = 400
_SOURCE_METRICS = {
    "ga4": ("GA4 Daily", ("sessions", "keyEvents")),
    "gsc": ("GSC Daily", ("clicks", "impressions")),
    "inquiry": (
        "Inquiry Daily",
        (
            "storedSubmissions",
            "quarantinedSubmissions",
            "nonQuarantinedSubmissions",
        ),
    ),
}
_STABLE_KEYS = {
    "overview_daily": "daily_key",
    "product_daily": "product_daily_key",
    "information_daily": "information_daily_key",
}


def build_v3_backfill(
    *,
    site: str,
    start: date,
    end: date,
    cache_dir: Path,
    audit_dir: Path,
    product_mapping: ProductMapping,
    page_dimension: PageDimension,
    information_mapping: InformationMapping,
) -> dict[str, Any]:
    """Build consecutive complete daily facts without calling APIs or Feishu."""
    days = _date_range(start, end)
    records: dict[str, list[dict[str, Any]]] = {
        logical_name: [] for logical_name in _STABLE_KEYS
    }
    daily_source_totals: list[dict[str, Any]] = []

    for data_date in days:
        period = AnalyticsPeriod("day", data_date, data_date)
        details = load_period_details(cache_dir, site, period)
        fetch_result = load_daily_fetch_result(
            audit_dir=audit_dir,
            site=site,
            data_date=data_date,
            details=details,
        )
        payload = build_v3_daily_records(
            site=site,
            data_date=data_date,
            fetch_result=fetch_result,
            details=details,
            product_mapping=product_mapping,
            page_dimension=page_dimension,
            information_mapping=information_mapping,
        )
        for logical_name in records:
            rows = payload["records"].get(logical_name)
            if not isinstance(rows, list):
                raise ValueError("V3 daily payload is missing a declared table")
            records[logical_name].extend(rows)
        daily_source_totals.append(
            {
                "data_date": data_date.isoformat(),
                "totals": fetch_result["totals"],
            }
        )

    for logical_name, key in _STABLE_KEYS.items():
        _require_unique(records[logical_name], key)
    _require_expected_record_counts(
        days=len(days),
        records=records,
        product_lines=len(product_mapping.report_lines),
        information_combinations=(
            len(information_mapping.themes)
            * len(information_mapping.content_types)
        ),
    )
    additive_totals = _overview_totals(records["overview_daily"])
    _require_source_reconciliation(daily_source_totals, additive_totals)

    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": "3",
        "mode": "backfill_dry_run",
        "write_enabled": False,
        "site": site,
        "date_range": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "inclusive": True,
            "days": len(days),
        },
        "generated_at": generated_at,
        "records": records,
        "reconciliation": {
            "status": "passed",
            "complete_days": len(days),
            "record_counts": {
                name: len(rows) for name, rows in records.items()
            },
            "unique_stable_keys": True,
            "additive_totals": additive_totals,
            "page_dimension": dict(page_dimension.summary),
            "page_classification_version": page_dimension.version,
            "product_mapping_version": product_mapping.version,
            "information_mapping_version": information_mapping.version,
        },
    }


def load_daily_fetch_result(
    *,
    audit_dir: Path,
    site: str,
    data_date: date,
    details: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Rebuild the approved CLI summary from one audit and its redacted caches."""
    audit = _object(audit_path_for_day(audit_dir, site, data_date), "daily audit")
    request = audit.get("request")
    if not isinstance(request, Mapping):
        raise ValueError("daily audit request is unavailable")
    expected_date = data_date.isoformat()
    expected_range = {"start": expected_date, "end": expected_date}
    if (
        request.get("command") != "fetch"
        or request.get("site") != site
        or request.get("date_range") != expected_range
    ):
        raise ValueError("daily audit does not match the requested site and date")

    source_statuses = audit.get("source_statuses")
    if not isinstance(source_statuses, Mapping):
        raise ValueError("daily audit source statuses are unavailable")
    freshness = source_statuses.get("generated_at")
    _require_utc_timestamp(freshness)
    sources = source_statuses.get("sources")
    if not isinstance(sources, Mapping) or set(sources) != set(_SOURCE_METRICS):
        raise ValueError("daily audit must contain exactly three approved sources")
    normalized_sources: dict[str, dict[str, Any]] = {}
    for source in _SOURCE_METRICS:
        status = sources.get(source)
        if not isinstance(status, Mapping) or status.get("status") != "ok":
            raise ValueError(f"daily audit source is not complete: {source}")
        if source == "gsc" and status.get("truncated") is True:
            raise ValueError("daily GSC details are truncated")
        normalized_sources[source] = dict(status)

    totals: dict[str, dict[str, float]] = {}
    for source, (collection, metrics) in _SOURCE_METRICS.items():
        totals[source] = {
            metric: _sum_metric(details.get(collection, ()), metric)
            for metric in metrics
        }
    return {
        "status": "ok",
        "complete": True,
        "freshness": freshness,
        "date_range": expected_range,
        "sources": normalized_sources,
        "totals": totals,
    }


def audit_path_for_day(root: Path, site: str, data_date: date) -> Path:
    """Return the deterministic audit path written by the approved CLI."""
    digest = hashlib.sha256(site.encode("utf-8")).hexdigest()[:12]
    value = data_date.isoformat()
    return root / f"{digest}-{value}-{value}.json"


def _date_range(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError("backfill end date must not precede start date")
    count = (end - start).days + 1
    if count > MAXIMUM_BACKFILL_DAYS:
        raise ValueError("backfill exceeds the 400-day safety limit")
    return [start + timedelta(days=offset) for offset in range(count)]


def _object(path: Path, label: str) -> dict[str, Any]:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is unavailable") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain an object")
    return value


def _require_utc_timestamp(value: object) -> None:
    if not isinstance(value, str):
        raise ValueError("daily audit freshness is unavailable")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("daily audit freshness is invalid") from error
    if parsed.tzinfo is None:
        raise ValueError("daily audit freshness must include a timezone")


def _sum_metric(rows: Sequence[Mapping[str, Any]], metric: str) -> float:
    total = 0.0
    for row in rows:
        value = row.get(metric)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"daily cache metric is invalid: {metric}")
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"daily cache metric is invalid: {metric}")
        total += number
    return total


def _require_unique(rows: Sequence[Mapping[str, Any]], key: str) -> None:
    values = [row.get(key) for row in rows]
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError(f"backfill contains an invalid stable key: {key}")
    if len(values) != len(set(values)):
        raise ValueError(f"backfill contains a duplicate stable key: {key}")


def _require_expected_record_counts(
    *,
    days: int,
    records: Mapping[str, Sequence[Mapping[str, Any]]],
    product_lines: int,
    information_combinations: int,
) -> None:
    expected = {
        "overview_daily": days,
        "product_daily": days * product_lines,
        "information_daily": days * information_combinations,
    }
    actual = {name: len(records[name]) for name in expected}
    if actual != expected:
        raise ValueError("V3 backfill record counts do not match the daily grain")


def _overview_totals(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    metrics = (
        "ga4_sessions",
        "ga4_key_events",
        "gsc_clicks",
        "gsc_impressions",
        "stored_submissions",
        "accepted_inquiries",
        "product_page_sessions",
        "information_page_sessions",
        "other_page_sessions",
        "classified_page_sessions",
    )
    return {
        metric: sum(_whole(row.get(metric), metric) for row in rows)
        for metric in metrics
    }


def _whole(value: object, metric: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"V3 overview metric is invalid: {metric}")
    return value


def _require_source_reconciliation(
    daily_source_totals: Sequence[Mapping[str, Any]],
    overview_totals: Mapping[str, int],
) -> None:
    source_total = {
        "ga4_sessions": 0.0,
        "ga4_key_events": 0.0,
        "gsc_clicks": 0.0,
        "gsc_impressions": 0.0,
        "stored_submissions": 0.0,
        "accepted_inquiries": 0.0,
    }
    for day in daily_source_totals:
        totals = day.get("totals")
        if not isinstance(totals, Mapping):
            raise ValueError("daily source totals are unavailable")
        source_total["ga4_sessions"] += _nested_number(totals, "ga4", "sessions")
        source_total["ga4_key_events"] += _nested_number(totals, "ga4", "keyEvents")
        source_total["gsc_clicks"] += _nested_number(totals, "gsc", "clicks")
        source_total["gsc_impressions"] += _nested_number(
            totals, "gsc", "impressions"
        )
        source_total["stored_submissions"] += _nested_number(
            totals, "inquiry", "storedSubmissions"
        )
        source_total["accepted_inquiries"] += _nested_number(
            totals, "inquiry", "nonQuarantinedSubmissions"
        )
    for metric, value in source_total.items():
        if not value.is_integer() or int(value) != overview_totals.get(metric):
            raise ValueError(f"V3 backfill does not reconcile source total: {metric}")


def _nested_number(
    totals: Mapping[str, Any], source: str, metric: str
) -> float:
    values = totals.get(source)
    if not isinstance(values, Mapping):
        raise ValueError(f"daily source totals are missing: {source}")
    value = values.get(metric)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"daily source total is invalid: {source}.{metric}")
    return float(value)
