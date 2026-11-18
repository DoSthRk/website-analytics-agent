"""Build a product-level observation funnel from existing daily source facts.

This phase deliberately preserves the boundaries between GSC, GA4, and the
website inquiry database.  The output is suitable for review or a later
Feishu publish step, but this module performs no external writes.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any

from website_analytics.page_classification import PageDimension
from website_analytics.product_mapping import ProductMapping, build_product_report


MEASUREMENT_VERSION = "stage-a-observation-v1"
SOURCE_BOUNDARY_NOTE = (
    "GA4按属性时区统计，GSC按Pacific Time统计，询盘按网站服务器日历统计；"
    "GSC点击、GA4会话与数据库询盘不是同一识别体系。跨来源阶段只用于规模"
    "对照和异常诊断，不称为严格转化率。"
)

_UNAVAILABLE_METRICS = (
    "ga4_organic_product_landing_sessions",
    "ga4_direct_product_arrival_sessions",
    "ga4_content_assisted_product_arrival_sessions",
    "ga4_other_assisted_product_arrival_sessions",
    "ga4_engaged_product_sessions",
    "ga4_form_start_sessions",
    "ga4_generate_lead_sessions",
)

_STAGES = (
    (
        "SEARCH_IMPRESSION",
        "1｜Google搜索曝光",
        "GSC",
        "曝光",
        "gsc_impressions",
        False,
        None,
        "起始规模；不与索引URL快照计算转化率。",
    ),
    (
        "SEARCH_CLICK",
        "2｜Google自然搜索点击",
        "GSC",
        "点击",
        "gsc_clicks",
        True,
        "gsc_ctr",
        "与上一阶段同属GSC，可计算搜索点击率。",
    ),
    (
        "PRODUCT_LANDING",
        "3｜产品落地会话（全部渠道）",
        "GA4",
        "会话",
        "ga4_product_landing_sessions_all_channels",
        False,
        None,
        "跨GSC到GA4，且包含全部渠道，只作规模对照。",
    ),
    (
        "ENGAGED_PRODUCT_LANDING",
        "4｜产品落地有效会话（全部渠道）",
        "GA4",
        "会话",
        "ga4_product_landing_engaged_sessions_all_channels",
        True,
        "ga4_landing_engagement_rate",
        "与上一阶段同属GA4产品落地页会话，可计算有效会话率。",
    ),
    (
        "ACCEPTED_INQUIRY",
        "5｜官网入库询盘",
        "官网数据库",
        "记录",
        "db_accepted_inquiries",
        False,
        None,
        "跨GA4到官网数据库，只作业务结果核对。",
    ),
)


def build_product_funnel_observation_daily(
    *,
    site: str,
    data_date: date,
    fetch_result: Mapping[str, Any],
    details: Mapping[str, Sequence[Mapping[str, Any]]],
    product_mapping: ProductMapping,
    page_dimension: PageDimension,
) -> dict[str, Any]:
    """Return one complete day of product observation facts without writes."""
    _require_complete_fetch(fetch_result)
    _require_single_day(details, data_date)
    refreshed_at = _freshness(fetch_result).astimezone(timezone.utc).isoformat()
    report = build_product_report(
        product_mapping,
        details,
        {},
        page_dimension,
    )
    report_lines = _index(report.get("reportLines"), "reportLineId")
    inquiry_lines = _index(report.get("inquiryReportLines"), "reportLineId")

    wide_rows: list[dict[str, Any]] = []
    stage_rows: list[dict[str, Any]] = []
    for configured in product_mapping.report_lines:
        line = _required(report_lines, configured.identifier)
        inquiry = _required(inquiry_lines, configured.identifier)
        daily_key = f"{site}|{data_date.isoformat()}|{configured.identifier}"
        row: dict[str, Any] = {
            "product_funnel_daily_key": daily_key,
            "site": site,
            "data_date": data_date.isoformat(),
            "data_status": "observation_complete",
            "product_line_id": configured.identifier,
            "product_name": configured.name,
            "category_l1": configured.category_l1,
            "category_l2": configured.category_l2,
            "category_l3": configured.category_l3,
            "gsc_impressions": _whole(line.get("gscImpressionsCurrent")),
            "gsc_clicks": _whole(line.get("gscClicksCurrent")),
            "gsc_position_impression_sum": _number(
                line.get("gscPositionImpressionSumCurrent")
            ),
            "ga4_product_landing_sessions_all_channels": _whole(
                line.get("ga4SessionsCurrent")
            ),
            "ga4_product_landing_engaged_sessions_all_channels": _whole(
                line.get("ga4EngagedSessionsCurrent")
            ),
            "ga4_product_landing_key_events_all_channels": _whole(
                line.get("ga4KeyEventsCurrent")
            ),
            "db_accepted_inquiries": _whole(
                inquiry.get("nonQuarantinedSubmissionsCurrent")
            ),
            "mapping_version": product_mapping.version,
            "measurement_version": MEASUREMENT_VERSION,
            "refreshed_at": refreshed_at,
            "source_boundary_note": SOURCE_BOUNDARY_NOTE,
        }
        row.update({metric: None for metric in _UNAVAILABLE_METRICS})
        _validate_observed_row(row)
        wide_rows.append(row)
        stage_rows.extend(_stage_rows(row))

    _require_unique_keys(wide_rows, "product_funnel_daily_key")
    _require_unique_keys(stage_rows, "product_funnel_stage_daily_key")
    _require_stage_reconciliation(wide_rows, stage_rows)
    return {
        "schema_version": "1",
        "mode": "product_funnel_observation_daily_dry_run",
        "write_enabled": False,
        "site": site,
        "data_date": data_date.isoformat(),
        "measurement_version": MEASUREMENT_VERSION,
        "source_cutoff_dates": {
            "gsc": data_date.isoformat(),
            "ga4": data_date.isoformat(),
            "inquiry": data_date.isoformat(),
        },
        "warnings": [
            SOURCE_BOUNDARY_NOTE,
            "产品落地会话来自GA4落地页维度，包含全部渠道，不代表站内产品页点击。",
            "未完成网站埋点的指标使用null，不使用0代替未知。",
        ],
        "records": {
            "product_funnel_daily": wide_rows,
            "product_funnel_stage_daily": stage_rows,
        },
    }


def _stage_rows(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    stages: list[dict[str, Any]] = []
    previous_value: int | None = None
    for order, stage in enumerate(_STAGES, start=1):
        (
            stage_id,
            stage_name,
            source,
            unit,
            metric,
            strict_from_previous,
            rate_key,
            note,
        ) = stage
        value = _whole(row.get(metric))
        rate = (
            _ratio(value, previous_value)
            if strict_from_previous and previous_value is not None
            else None
        )
        stages.append(
            {
                "product_funnel_stage_daily_key": (
                    f"{row['product_funnel_daily_key']}|{stage_id}"
                ),
                "product_funnel_daily_key": row["product_funnel_daily_key"],
                "site": row["site"],
                "data_date": row["data_date"],
                "data_status": row["data_status"],
                "product_line_id": row["product_line_id"],
                "product_name": row["product_name"],
                "category_l1": row["category_l1"],
                "category_l2": row["category_l2"],
                "category_l3": row["category_l3"],
                "stage_order": order,
                "stage_id": stage_id,
                "stage_name": stage_name,
                "stage_source": source,
                "stage_unit": unit,
                "stage_value": value,
                "strict_transition_from_previous": strict_from_previous,
                "rate_from_previous": rate,
                "rate_key": rate_key,
                "source_boundary_note": note,
                "mapping_version": row["mapping_version"],
                "measurement_version": row["measurement_version"],
                "refreshed_at": row["refreshed_at"],
            }
        )
        previous_value = value
    return stages


def _validate_observed_row(row: Mapping[str, Any]) -> None:
    impressions = _whole(row.get("gsc_impressions"))
    clicks = _whole(row.get("gsc_clicks"))
    landing = _whole(row.get("ga4_product_landing_sessions_all_channels"))
    engaged = _whole(
        row.get("ga4_product_landing_engaged_sessions_all_channels")
    )
    if clicks > impressions:
        raise ValueError("GSC product clicks exceed impressions")
    if engaged > landing:
        raise ValueError("GA4 engaged product landing sessions exceed sessions")
    if any(row.get(metric) is not None for metric in _UNAVAILABLE_METRICS):
        raise ValueError("unavailable instrumentation metrics must remain null")


def _require_stage_reconciliation(
    wide_rows: Sequence[Mapping[str, Any]],
    stage_rows: Sequence[Mapping[str, Any]],
) -> None:
    by_product: dict[str, list[Mapping[str, Any]]] = {}
    for stage in stage_rows:
        key = stage.get("product_funnel_daily_key")
        if not isinstance(key, str):
            raise ValueError("funnel stage product key is invalid")
        by_product.setdefault(key, []).append(stage)
    for row in wide_rows:
        key = str(row["product_funnel_daily_key"])
        stages = sorted(by_product.get(key, []), key=lambda item: item["stage_order"])
        if len(stages) != len(_STAGES):
            raise ValueError("product funnel does not contain every observation stage")
        for stage, definition in zip(stages, _STAGES, strict=True):
            metric = definition[4]
            if stage["stage_value"] != row[metric]:
                raise ValueError("product funnel stage does not reconcile daily fact")


def _require_complete_fetch(result: Mapping[str, Any]) -> None:
    if result.get("status") != "ok" or result.get("complete") is not True:
        raise ValueError("product funnel requires one complete three-source fetch")
    sources = result.get("sources")
    if not isinstance(sources, Mapping) or set(sources) != {
        "ga4",
        "gsc",
        "inquiry",
    }:
        raise ValueError("product funnel requires GA4, GSC, and inquiry")
    if any(
        not isinstance(value, Mapping) or value.get("status") != "ok"
        for value in sources.values()
    ):
        raise ValueError("product funnel requires all three sources to be ok")


def _require_single_day(
    details: Mapping[str, Sequence[Mapping[str, Any]]], expected: date
) -> None:
    expected_value = expected.isoformat()
    for collection in ("GA4 Daily", "GSC Daily", "Inquiry Daily"):
        for row in details.get(collection, ()):
            if row.get("date") != expected_value:
                raise ValueError("product funnel facts cannot contain another date")


def _freshness(result: Mapping[str, Any]) -> datetime:
    value = result.get("freshness")
    if not isinstance(value, str):
        raise ValueError("fetch freshness is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("fetch freshness is invalid") from error
    if parsed.tzinfo is None:
        raise ValueError("fetch freshness must include a timezone")
    return parsed


def _index(value: object, key: str) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("product funnel report collection is invalid")
    result: dict[str, Mapping[str, Any]] = {}
    for row in value:
        if not isinstance(row, Mapping) or not isinstance(row.get(key), str):
            raise ValueError("product funnel report row is invalid")
        row_key = str(row[key])
        if row_key in result:
            raise ValueError("product funnel report keys must be unique")
        result[row_key] = row
    return result


def _required(
    values: Mapping[str, Mapping[str, Any]], key: str
) -> Mapping[str, Any]:
    value = values.get(key)
    if value is None:
        raise ValueError(f"product funnel report is missing {key}")
    return value


def _require_unique_keys(rows: Sequence[Mapping[str, Any]], field: str) -> None:
    keys = [row.get(field) for row in rows]
    if not all(isinstance(key, str) and key for key in keys):
        raise ValueError(f"{field} must be non-empty text")
    if len(keys) != len(set(keys)):
        raise ValueError(f"{field} values must be unique")


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("metric must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("metric must be a finite non-negative number")
    return number


def _whole(value: object) -> int:
    number = _number(value)
    if not number.is_integer():
        raise ValueError("count metric must be a whole number")
    return int(number)


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None
