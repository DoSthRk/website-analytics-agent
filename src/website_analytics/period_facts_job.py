"""Rebuild the period tables from the daily-facts store and write the changes."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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
    target: Path,
    client_factory: Callable[[PeriodTarget], RecordClient],
    apply: bool,
    after_write: Callable[[dict[str, list[dict[str, Any]]]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Full rebuild from every stored day; only changed rows reach Feishu.

    ``after_write`` runs only once every row was written and read back. Its
    failure is reported in the result and does not undo or fail the write.
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
