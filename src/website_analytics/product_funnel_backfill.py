"""Assemble a bounded product-funnel backfill from approved local artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from website_analytics.dashboard_sync import load_period_details
from website_analytics.dashboard_v3_backfill import load_daily_fetch_result
from website_analytics.page_classification import PageDimension
from website_analytics.periods import AnalyticsPeriod
from website_analytics.product_funnel import build_product_funnel_observation_daily
from website_analytics.product_mapping import ProductMapping


MAXIMUM_BACKFILL_DAYS = 400
TABLE_KEYS = ("product_funnel_daily", "product_funnel_stage_daily")
STABLE_KEYS = {
    "product_funnel_daily": "product_funnel_daily_key",
    "product_funnel_stage_daily": "product_funnel_stage_daily_key",
}
ADDITIVE_METRICS = (
    "gsc_impressions",
    "gsc_clicks",
    "gsc_position_impression_sum",
    "ga4_product_landing_sessions_all_channels",
    "ga4_product_landing_engaged_sessions_all_channels",
    "ga4_product_landing_key_events_all_channels",
    "db_accepted_inquiries",
)


def build_product_funnel_backfill(
    *,
    site: str,
    start: date,
    end: date,
    cache_dir: Path,
    audit_dir: Path,
    product_mapping: ProductMapping,
    page_dimension: PageDimension,
) -> dict[str, Any]:
    """Build consecutive complete observation days without network access."""
    days = _date_range(start, end)
    records: dict[str, list[dict[str, Any]]] = {key: [] for key in TABLE_KEYS}
    for data_date in days:
        period = AnalyticsPeriod("day", data_date, data_date)
        details = load_period_details(cache_dir, site, period)
        fetch_result = load_daily_fetch_result(
            audit_dir=audit_dir,
            site=site,
            data_date=data_date,
            details=details,
        )
        payload = build_product_funnel_observation_daily(
            site=site,
            data_date=data_date,
            fetch_result=fetch_result,
            details=details,
            product_mapping=product_mapping,
            page_dimension=page_dimension,
        )
        for logical_name in TABLE_KEYS:
            rows = payload["records"].get(logical_name)
            if not isinstance(rows, list):
                raise ValueError("product funnel daily payload is incomplete")
            records[logical_name].extend(rows)

    for logical_name, stable_key in STABLE_KEYS.items():
        _require_unique(records[logical_name], stable_key)
    expected_counts = {
        "product_funnel_daily": len(days) * len(product_mapping.report_lines),
        "product_funnel_stage_daily": (
            len(days) * len(product_mapping.report_lines) * 5
        ),
    }
    actual_counts = {key: len(records[key]) for key in TABLE_KEYS}
    if actual_counts != expected_counts:
        raise ValueError("product funnel backfill record counts are invalid")
    _require_stage_totals(records)
    additive_totals = _additive_totals(records["product_funnel_daily"])
    records_sha256 = hashlib.sha256(
        json.dumps(
            records,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "1",
        "mode": "product_funnel_backfill_dry_run",
        "write_enabled": False,
        "site": site,
        "date_range": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "inclusive": True,
            "days": len(days),
        },
        "generated_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "records": records,
        "reconciliation": {
            "status": "passed",
            "complete_days": len(days),
            "record_counts": actual_counts,
            "unique_stable_keys": True,
            "stage_totals_match_daily_facts": True,
            "additive_totals": additive_totals,
            "records_sha256": records_sha256,
            "page_dimension": dict(page_dimension.summary),
            "page_classification_version": page_dimension.version,
            "product_mapping_version": product_mapping.version,
        },
    }


def _date_range(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError("backfill end date must not precede start date")
    count = (end - start).days + 1
    if count > MAXIMUM_BACKFILL_DAYS:
        raise ValueError("backfill exceeds the 400-day safety limit")
    return [start + timedelta(days=offset) for offset in range(count)]


def _require_unique(rows: Sequence[Mapping[str, Any]], key: str) -> None:
    values = [row.get(key) for row in rows]
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError(f"backfill contains an invalid stable key: {key}")
    if len(values) != len(set(values)):
        raise ValueError(f"backfill contains a duplicate stable key: {key}")


def _additive_totals(rows: Sequence[Mapping[str, Any]]) -> dict[str, float | int]:
    totals: dict[str, float | int] = {}
    for metric in ADDITIVE_METRICS:
        total = sum(_number(row.get(metric), metric) for row in rows)
        totals[metric] = int(total) if total.is_integer() else total
    return totals


def _require_stage_totals(
    records: Mapping[str, Sequence[Mapping[str, Any]]],
) -> None:
    wide = records["product_funnel_daily"]
    stages = records["product_funnel_stage_daily"]
    expected = {
        "SEARCH_IMPRESSION": sum(
            _number(row.get("gsc_impressions"), "gsc_impressions") for row in wide
        ),
        "SEARCH_CLICK": sum(
            _number(row.get("gsc_clicks"), "gsc_clicks") for row in wide
        ),
        "PRODUCT_LANDING": sum(
            _number(
                row.get("ga4_product_landing_sessions_all_channels"),
                "ga4_product_landing_sessions_all_channels",
            )
            for row in wide
        ),
        "ENGAGED_PRODUCT_LANDING": sum(
            _number(
                row.get("ga4_product_landing_engaged_sessions_all_channels"),
                "ga4_product_landing_engaged_sessions_all_channels",
            )
            for row in wide
        ),
        "ACCEPTED_INQUIRY": sum(
            _number(row.get("db_accepted_inquiries"), "db_accepted_inquiries")
            for row in wide
        ),
    }
    actual = {stage: 0.0 for stage in expected}
    for row in stages:
        stage = row.get("stage_id")
        if stage not in actual:
            raise ValueError("product funnel backfill contains an unknown stage")
        actual[str(stage)] += _number(row.get("stage_value"), "stage_value")
    if actual != expected:
        raise ValueError("product funnel stage totals do not match daily facts")


def _number(value: object, metric: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"product funnel metric is invalid: {metric}")
    number = float(value)
    if number < 0:
        raise ValueError(f"product funnel metric is invalid: {metric}")
    return number
