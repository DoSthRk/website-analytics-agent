"""Recompute daily V3 and product-funnel facts from cached sources, without writes.

Used when URL rules change: every complete cached day is rebuilt with the new
mapping and compared with the previous mapping, so the shift between page
classes and product lines is visible before anything is written to Feishu.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from website_analytics.dashboard_sync import load_period_details
from website_analytics.dashboard_v3 import build_v3_daily_records
from website_analytics.dashboard_v3_backfill import load_daily_fetch_result
from website_analytics.information_mapping import InformationMapping
from website_analytics.page_classification import PageDimension
from website_analytics.periods import AnalyticsPeriod
from website_analytics.product_funnel import build_product_funnel_observation_daily
from website_analytics.product_mapping import ProductMapping

V3_TABLES = ("overview_daily", "product_daily", "information_daily")
FUNNEL_TABLES = ("product_funnel_daily", "product_funnel_stage_daily")
_PAGE_CLASS_FIELDS = ("product_page_sessions", "information_page_sessions", "other_page_sessions")


@dataclass(frozen=True)
class RuleSet:
    product_mapping: ProductMapping
    page_dimension: PageDimension
    information_mapping: InformationMapping


def recompute_days(
    *,
    site: str,
    days: Iterable[date],
    cache_dir: Path,
    audit_dir: Path,
    current: RuleSet,
    baseline: RuleSet,
) -> dict[str, Any]:
    """Rebuild each complete day with ``current`` rules and compare to ``baseline``.

    Days whose cache or audit is not complete are skipped and listed with the
    reason; they are never written as zero.
    """
    records: dict[str, list[dict[str, Any]]] = {name: [] for name in (*V3_TABLES, *FUNNEL_TABLES)}
    skipped: list[dict[str, str]] = []
    mismatches: list[dict[str, Any]] = []
    class_shift: Counter[str] = Counter()
    line_before: Counter[str] = Counter()
    line_after: Counter[str] = Counter()
    completed: list[str] = []

    for data_date in days:
        try:
            details = load_period_details(cache_dir, site, AnalyticsPeriod("day", data_date, data_date))
            fetch_result = load_daily_fetch_result(
                audit_dir=audit_dir, site=site, data_date=data_date, details=details
            )
            new = _build_v3(site, data_date, fetch_result, details, current)
            old = _build_v3(site, data_date, fetch_result, details, baseline)
            funnel = build_product_funnel_observation_daily(
                site=site,
                data_date=data_date,
                fetch_result=fetch_result,
                details=details,
                product_mapping=current.product_mapping,
                page_dimension=current.page_dimension,
            )["records"]
        except ValueError as error:
            skipped.append({"data_date": data_date.isoformat(), "reason": str(error)[:200]})
            continue

        day = data_date.isoformat()
        new_overview = _single(new["overview_daily"])
        old_overview = _single(old["overview_daily"])
        if new_overview.get("ga4_sessions") != old_overview.get("ga4_sessions"):
            mismatches.append({"data_date": day, "check": "total_sessions_changed"})
        product_sum = sum(_number(row.get("ga4_sessions")) for row in new["product_daily"])
        if product_sum != _number(new_overview.get("product_page_sessions")):
            mismatches.append({"data_date": day, "check": "product_lines_do_not_sum_to_product_pages"})
        funnel_sum = sum(
            _number(row.get("ga4_product_landing_sessions_all_channels"))
            for row in funnel["product_funnel_daily"]
        )
        if funnel_sum != product_sum:
            mismatches.append({"data_date": day, "check": "funnel_landing_differs_from_product_daily"})

        for field in _PAGE_CLASS_FIELDS:
            class_shift[field] += _number(new_overview.get(field)) - _number(old_overview.get(field))
        for row in old["product_daily"]:
            line_before[str(row["product_line_id"])] += _number(row.get("ga4_sessions"))
        for row in new["product_daily"]:
            line_after[str(row["product_line_id"])] += _number(row.get("ga4_sessions"))
        for name in V3_TABLES:
            records[name].extend(new[name])
        for name in FUNNEL_TABLES:
            records[name].extend(funnel[name])
        completed.append(day)

    lines = sorted(set(line_before) | set(line_after))
    return {
        "mode": "history_recompute_dry_run",
        "write_enabled": False,
        "site": site,
        "rules": {
            "current": _versions(current),
            "baseline": _versions(baseline),
        },
        "completed_days": completed,
        "skipped_days": skipped,
        "mismatches": mismatches,
        "page_class_session_shift": dict(class_shift),
        "product_line_sessions": [
            {"product_line_id": line, "baseline": line_before[line], "current": line_after[line]}
            for line in lines
        ],
        "record_counts": {name: len(rows) for name, rows in records.items()},
        "records": records,
    }


def _build_v3(
    site: str,
    data_date: date,
    fetch_result: Mapping[str, Any],
    details: Mapping[str, Any],
    rules: RuleSet,
) -> dict[str, list[dict[str, Any]]]:
    payload = build_v3_daily_records(
        site=site,
        data_date=data_date,
        fetch_result=fetch_result,
        details=details,
        product_mapping=rules.product_mapping,
        page_dimension=rules.page_dimension,
        information_mapping=rules.information_mapping,
    )
    records = payload["records"]
    if not all(isinstance(records.get(name), list) for name in V3_TABLES):
        raise ValueError("V3 daily payload is missing a declared table")
    return {name: records[name] for name in V3_TABLES}


def _single(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rows) != 1:
        raise ValueError("expected exactly one overview row per day")
    return rows[0]


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _versions(rules: RuleSet) -> dict[str, str]:
    return {
        "product_mapping": rules.product_mapping.version,
        "page_classification": rules.page_dimension.version,
        "information_mapping": rules.information_mapping.version,
    }
