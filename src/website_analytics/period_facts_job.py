"""Rebuild the period tables from the daily-facts store and write the changes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from website_analytics.crawler_traffic import load_crawler_shares
from website_analytics.daily_facts_store import last_day, load_days
from website_analytics.feishu_period_facts import (
    PeriodTarget,
    apply_period_plans,
    load_period_target,
    period_contract,
    plan_period_sync,
)
from website_analytics.feishu_records import RecordClient
from website_analytics.period_facts import build_period_facts, load_exclusions
from website_analytics.product_mapping import ProductMapping


def refresh_period_tables(
    *,
    site: str,
    mapping: ProductMapping,
    store: Path,
    exclusions: Path,
    crawler_store: Path,
    crawler_rule: Path,
    target: Path,
    client_factory: Callable[[PeriodTarget], RecordClient],
    apply: bool,
    after_write: Callable[[dict[str, list[dict[str, Any]]]], dict[str, Any]] | None = None,
    crawler_extra: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Full rebuild from every stored day; only changed rows reach Feishu.

    ``after_write`` runs only once every row was written and read back. Its
    failure is reported in the result and does not undo or fail the write.
    ``crawler_extra`` adds fingerprint days fetched in this run but not saved
    (dry runs).
    """
    records = load_days(store)
    if not records["overview_daily"]:
        raise ValueError("daily facts store is empty")
    last_final = last_day(records)
    build = build_period_facts(
        site=site,
        mapping=mapping,
        funnel_daily=records["product_funnel_daily"],
        overview_daily=records["overview_daily"],
        last_final=last_final,
        crawler=crawler_for(records, site=site, store=crawler_store, rule=crawler_rule, extra=crawler_extra),
        exclusions=load_exclusions(exclusions, site),
    )
    period_target = load_period_target(target)
    client = client_factory(period_target)
    refreshed_at = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None, microsecond=0)
    plans = plan_period_sync(client, period_target, period_contract(), build.records, refreshed_at=refreshed_at)
    result: dict[str, Any] = {
        "last_final": last_final.isoformat(),
        "stored_days": len(records["overview_daily"]),
        "candidates": len(build.candidates),
    }
    if apply:
        result["write_result"] = apply_period_plans(client, plans)
        if after_write is not None:
            try:
                result["alert"] = after_write(build.records)
            except Exception as error:  # noqa: BLE001 - reported, retried by the next run
                result["alert"] = {"status": "failed", "error": str(error)[:500]}
    else:
        result["plan"] = {plan.logical_name: plan.counts() for plan in plans}
    return result


def crawler_for(
    records: Mapping[str, list[dict[str, Any]]], *, site: str, store: Path, rule: Path,
    extra: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[date, dict[str, float]]:
    """Suspected crawler share per report line for every day in the daily facts."""
    funnel = records["product_funnel_daily"]
    versions = {str(row["mapping_version"]) for row in funnel}
    if len(versions) != 1:
        raise ValueError(f"daily facts mix product mapping versions: {sorted(versions)}")
    days = {date.fromisoformat(str(row["data_date"])[:10]) for row in records["overview_daily"]}
    return load_crawler_shares(store, rule, site=site, days=days, mapping_version=versions.pop(), extra=extra)
