"""Build read-only V3 dashboard prototype records from approved cache data."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any

from website_analytics.information_mapping import (
    InformationMapping,
    classify_information_page,
)
from website_analytics.page_classification import canonical_page_path
from website_analytics.product_mapping import ProductMapping, match_product_rule


_METRICS = (
    "ga4_sessions",
    "gsc_clicks",
    "gsc_impressions",
    "stored_submissions",
    "quarantined_submissions",
    "accepted_inquiries",
)


def build_v3_dry_run(
    *,
    site: str,
    current_start: date,
    current_end: date,
    previous_start: date,
    previous_end: date,
    current_details: Mapping[str, Sequence[Mapping[str, Any]]],
    previous_details: Mapping[str, Sequence[Mapping[str, Any]]],
    product_mapping: ProductMapping,
    information_mapping: InformationMapping,
    product_paths: set[str],
    refreshed_at: datetime,
) -> dict[str, Any]:
    """Return a V3 dry-run payload without performing any external write.

    ``product_paths`` is deliberately explicit evidence. Information pages are
    admitted only when a versioned slug rule matches; all remaining traffic is
    kept in the overview's ``other`` bucket so prototype coverage stays honest.
    """
    if current_end < current_start or previous_end < previous_start:
        raise ValueError("period end cannot precede period start")
    if refreshed_at.tzinfo is None:
        raise ValueError("refreshed_at must include a timezone")

    normalized_product_paths = {
        normalized
        for value in product_paths
        if (normalized := canonical_page_path(value)) is not None
    }
    periods = (
        _PeriodData(
            label=f"{current_start.isoformat()} 至 {current_end.isoformat()}",
            start=current_start,
            end=current_end,
            details=current_details,
            is_current=True,
        ),
        _PeriodData(
            label=f"{previous_start.isoformat()} 至 {previous_end.isoformat()}",
            start=previous_start,
            end=previous_end,
            details=previous_details,
            is_current=False,
        ),
    )
    classified = [
        _classify_period(
            period,
            product_paths=normalized_product_paths,
            product_mapping=product_mapping,
            information_mapping=information_mapping,
        )
        for period in periods
    ]
    current, previous = classified

    overview_records = [
        _overview_record(
            site=site,
            period=period,
            classified=data,
            comparison=previous if period.is_current else None,
            refreshed_at=refreshed_at,
        )
        for period, data in zip(periods, classified, strict=True)
    ]
    product_records = _product_records(
        site=site,
        periods=periods,
        classified=classified,
        product_mapping=product_mapping,
        refreshed_at=refreshed_at,
    )
    information_records = _information_records(
        site=site,
        periods=periods,
        classified=classified,
        information_mapping=information_mapping,
        refreshed_at=refreshed_at,
    )
    return {
        "schema_version": "3",
        "mode": "dry_run",
        "write_enabled": False,
        "site": site,
        "classification_scope": {
            "status": "prototype_partial",
            "product_basis": "reviewed_product_page_evidence_snapshot",
            "information_basis": "explicit_versioned_slug_rules_only",
            "unmatched_handling": "kept_as_other; never silently assigned",
            "product_evidence_paths": len(normalized_product_paths),
            "note": (
                "三类数据源均为完整缓存；页面分类仅用于 V3 原型，"
                "正式同步前须改用同周期 pages.template 页面维表。"
            ),
        },
        "records": {
            "overview_periods": overview_records,
            "product_periods": product_records,
            "information_periods": information_records,
        },
    }


class _PeriodData:
    def __init__(
        self,
        *,
        label: str,
        start: date,
        end: date,
        details: Mapping[str, Sequence[Mapping[str, Any]]],
        is_current: bool,
    ) -> None:
        self.label = label
        self.start = start
        self.end = end
        self.details = details
        self.is_current = is_current


def _classify_period(
    period: _PeriodData,
    *,
    product_paths: set[str],
    product_mapping: ProductMapping,
    information_mapping: InformationMapping,
) -> dict[str, Any]:
    paths = _metrics_by_path(period.details)
    products: dict[str, dict[str, float | int]] = defaultdict(_empty_summary)
    information: dict[tuple[str, str], dict[str, float | int]] = defaultdict(
        _empty_summary
    )
    page_types: dict[str, dict[str, float | int]] = defaultdict(_empty_summary)

    for path, metrics in paths.items():
        if path in product_paths:
            product_rule = match_product_rule(
                product_mapping, path, "product_page", ""
            )
            if (
                product_rule is not None
                and product_rule.include_in_product_report
                and product_rule.report_line_id is not None
            ):
                _add_summary(products[product_rule.report_line_id], metrics)
                _add_summary(page_types["product_page"], metrics)
                continue
        information_result = classify_information_page(
            information_mapping,
            path=path,
            template="",
            page_class="information_page",
        )
        explicit_information = (
            information_result["informationThemeStatus"] == "matched"
            or information_result["informationContentTypeStatus"] == "matched"
        )
        if explicit_information and path not in product_paths:
            key = (
                information_result["informationThemeId"],
                information_result["informationContentTypeId"],
            )
            _add_summary(information[key], metrics)
            _add_summary(page_types["information_page"], metrics)
            continue
        _add_summary(page_types["other"], metrics)

    return {
        "totals": _period_totals(period.details),
        "products": products,
        "information": information,
        "page_types": page_types,
    }


def _metrics_by_path(
    details: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = defaultdict(_empty_metrics)
    for row in details.get("GA4 Pages", ()):
        path = canonical_page_path(row.get("landingPagePlusQueryString"))
        if path is not None:
            result[path]["ga4_sessions"] += _number(row.get("sessions"))
    for row in details.get("GSC Pages", ()):
        path = canonical_page_path(row.get("page"))
        if path is not None:
            result[path]["gsc_clicks"] += _number(row.get("clicks"))
            result[path]["gsc_impressions"] += _number(row.get("impressions"))
    for row in details.get("Inquiry Pages", ()):
        path = canonical_page_path(row.get("sourceUrl"))
        if path is not None:
            result[path]["stored_submissions"] += _number(
                row.get("storedSubmissions")
            )
            result[path]["quarantined_submissions"] += _number(
                row.get("quarantinedSubmissions")
            )
            result[path]["accepted_inquiries"] += _number(
                row.get("nonQuarantinedSubmissions")
            )
    return dict(result)


def _period_totals(
    details: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, float | None]:
    ga4_daily = details.get("GA4 Daily", ())
    gsc_daily = details.get("GSC Daily", ())
    inquiry_daily = details.get("Inquiry Daily", ())
    sessions = sum(_number(row.get("sessions")) for row in ga4_daily)
    engaged = sum(_number(row.get("engagedSessions")) for row in ga4_daily)
    clicks = sum(_number(row.get("clicks")) for row in gsc_daily)
    impressions = sum(_number(row.get("impressions")) for row in gsc_daily)
    return {
        "ga4_sessions": sessions,
        "ga4_active_users": sum(
            _number(row.get("activeUsers")) for row in ga4_daily
        ),
        "ga4_key_events": sum(_number(row.get("keyEvents")) for row in ga4_daily),
        "ga4_engagement_rate": engaged / sessions if sessions else None,
        "gsc_clicks": clicks,
        "gsc_impressions": impressions,
        "gsc_ctr": clicks / impressions if impressions else None,
        "gsc_position": (
            sum(
                _number(row.get("position")) * _number(row.get("impressions"))
                for row in gsc_daily
            )
            / impressions
            if impressions
            else None
        ),
        "stored_submissions": sum(
            _number(row.get("storedSubmissions")) for row in inquiry_daily
        ),
        "quarantined_submissions": sum(
            _number(row.get("quarantinedSubmissions")) for row in inquiry_daily
        ),
        "accepted_inquiries": sum(
            _number(row.get("nonQuarantinedSubmissions")) for row in inquiry_daily
        ),
    }


def _overview_record(
    *,
    site: str,
    period: _PeriodData,
    classified: Mapping[str, Any],
    comparison: Mapping[str, Any] | None,
    refreshed_at: datetime,
) -> dict[str, Any]:
    totals = classified["totals"]
    page_types = classified["page_types"]
    product = page_types.get("product_page", _empty_summary())
    information = page_types.get("information_page", _empty_summary())
    previous_totals = comparison["totals"] if comparison is not None else None
    classified_sessions = _number(product["ga4_sessions"]) + _number(
        information["ga4_sessions"]
    )
    sessions = _number(totals["ga4_sessions"])
    return {
        "period_key": _period_key(site, period),
        "site": site,
        "period_label": period.label,
        "period_kind": "周",
        "period_start": period.start.isoformat(),
        "period_end": period.end.isoformat(),
        "is_current": period.is_current,
        "dashboard_windows": (
            ["当前周期", "近4周", "近12周"]
            if period.is_current
            else ["近4周", "近12周"]
        ),
        "data_status": "页面分类原型",
        "ga4_sessions": _whole(totals["ga4_sessions"]),
        "ga4_active_users": _whole(totals["ga4_active_users"]),
        "ga4_key_events": _whole(totals["ga4_key_events"]),
        "gsc_clicks": _whole(totals["gsc_clicks"]),
        "gsc_impressions": _whole(totals["gsc_impressions"]),
        "gsc_ctr": totals["gsc_ctr"],
        "gsc_position": totals["gsc_position"],
        "stored_submissions": _whole(totals["stored_submissions"]),
        "accepted_inquiries": _whole(totals["accepted_inquiries"]),
        "product_page_sessions": _whole(product["ga4_sessions"]),
        "information_page_sessions": _whole(information["ga4_sessions"]),
        "other_page_sessions": _whole(sessions - classified_sessions),
        "page_classification_rate": classified_sessions / sessions if sessions else None,
        "sessions_delta": _metric_delta(totals, previous_totals, "ga4_sessions"),
        "clicks_delta": _metric_delta(totals, previous_totals, "gsc_clicks"),
        "inquiries_delta": _metric_delta(
            totals, previous_totals, "accepted_inquiries"
        ),
        "ga4_available_through": period.end.isoformat(),
        "gsc_available_through": period.end.isoformat(),
        "inquiry_available_through": period.end.isoformat(),
        "refreshed_at": refreshed_at.astimezone(timezone.utc).isoformat(),
        "operations_summary": _overview_hint(totals, previous_totals),
    }


def _product_records(
    *,
    site: str,
    periods: Sequence[_PeriodData],
    classified: Sequence[Mapping[str, Any]],
    product_mapping: ProductMapping,
    refreshed_at: datetime,
) -> list[dict[str, Any]]:
    current_products = classified[0]["products"]
    previous_products = classified[1]["products"]
    rows: list[dict[str, Any]] = []
    for index, period in enumerate(periods):
        values = classified[index]["products"]
        for line in product_mapping.report_lines:
            metrics = values.get(line.identifier, _empty_summary())
            counterpart = (
                previous_products.get(line.identifier, _empty_summary())
                if period.is_current
                else None
            )
            if not _has_activity(metrics) and not (
                period.is_current
                and _has_activity(previous_products.get(line.identifier, {}))
            ):
                continue
            rows.append(
                {
                    "product_period_key": (
                        f"{_period_key(site, period)}|{line.identifier}"
                    ),
                    "period_key": _period_key(site, period),
                    "site": site,
                    "period_kind": "周",
                    "period_start": period.start.isoformat(),
                    "period_end": period.end.isoformat(),
                    "is_current": period.is_current,
                    "dashboard_windows": (
                        ["当前周期", "近4周", "近12周"]
                        if period.is_current
                        else ["近4周", "近12周"]
                    ),
                    "product_line_id": line.identifier,
                    "product_name": line.name,
                    "category_l1": line.category_l1,
                    "category_l2": line.category_l2,
                    "category_l3": line.category_l3,
                    **_metric_cells(metrics, counterpart),
                    "mapping_version": product_mapping.version,
                    "mapping_status": "页面证据快照原型",
                    "operations_hint": _row_hint(metrics),
                    "refreshed_at": refreshed_at.astimezone(timezone.utc).isoformat(),
                }
            )
    return rows


def _information_records(
    *,
    site: str,
    periods: Sequence[_PeriodData],
    classified: Sequence[Mapping[str, Any]],
    information_mapping: InformationMapping,
    refreshed_at: datetime,
) -> list[dict[str, Any]]:
    theme_names = {value.identifier: value.name for value in information_mapping.themes}
    content_names = {
        value.identifier: value.name for value in information_mapping.content_types
    }
    previous_information = classified[1]["information"]
    keys = sorted(set(classified[0]["information"]) | set(previous_information))
    rows: list[dict[str, Any]] = []
    for index, period in enumerate(periods):
        values = classified[index]["information"]
        for theme_id, content_type_id in keys:
            metrics = values.get((theme_id, content_type_id), _empty_summary())
            counterpart = (
                previous_information.get(
                    (theme_id, content_type_id), _empty_summary()
                )
                if period.is_current
                else None
            )
            if not _has_activity(metrics) and not (
                period.is_current
                and _has_activity(
                    previous_information.get((theme_id, content_type_id), {})
                )
            ):
                continue
            rows.append(
                {
                    "information_period_key": (
                        f"{_period_key(site, period)}|{theme_id}|{content_type_id}"
                    ),
                    "period_key": _period_key(site, period),
                    "site": site,
                    "period_kind": "周",
                    "period_start": period.start.isoformat(),
                    "period_end": period.end.isoformat(),
                    "is_current": period.is_current,
                    "dashboard_windows": (
                        ["当前周期", "近4周", "近12周"]
                        if period.is_current
                        else ["近4周", "近12周"]
                    ),
                    "theme_id": theme_id,
                    "theme": theme_names[theme_id],
                    "content_type_id": content_type_id,
                    "content_type": content_names[content_type_id],
                    **_metric_cells(metrics, counterpart),
                    "mapping_version": information_mapping.version,
                    "mapping_status": "明确slug规则原型",
                    "operations_hint": _row_hint(metrics),
                    "refreshed_at": refreshed_at.astimezone(timezone.utc).isoformat(),
                }
            )
    return rows


def _metric_cells(
    metrics: Mapping[str, Any], comparison: Mapping[str, Any] | None
) -> dict[str, Any]:
    impressions = _number(metrics.get("gsc_impressions", 0))
    clicks = _number(metrics.get("gsc_clicks", 0))
    return {
        "ga4_sessions": _whole(metrics.get("ga4_sessions", 0)),
        "gsc_clicks": _whole(clicks),
        "gsc_impressions": _whole(impressions),
        "gsc_ctr": clicks / impressions if impressions else None,
        "stored_submissions": _whole(metrics.get("stored_submissions", 0)),
        "accepted_inquiries": _whole(metrics.get("accepted_inquiries", 0)),
        "included_pages": _whole(metrics.get("included_pages", 0)),
        "sessions_delta": _metric_delta(metrics, comparison, "ga4_sessions"),
        "clicks_delta": _metric_delta(metrics, comparison, "gsc_clicks"),
        "inquiries_delta": _metric_delta(
            metrics, comparison, "accepted_inquiries"
        ),
    }


def _empty_metrics() -> dict[str, float]:
    return {metric: 0.0 for metric in _METRICS}


def _empty_summary() -> dict[str, float | int]:
    return {**_empty_metrics(), "included_pages": 0}


def _add_summary(target: dict[str, float | int], metrics: Mapping[str, Any]) -> None:
    target["included_pages"] = int(target["included_pages"]) + 1
    for metric in _METRICS:
        target[metric] = _number(target[metric]) + _number(metrics.get(metric, 0))


def _has_activity(metrics: Mapping[str, Any]) -> bool:
    return bool(
        metrics
        and (
            _number(metrics.get("included_pages", 0))
            or any(_number(metrics.get(metric, 0)) for metric in _METRICS)
        )
    )


def _number(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("metric must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("metric must be finite")
    return number


def _whole(value: object) -> int:
    return int(_number(value))


def _metric_delta(
    current: Mapping[str, Any], previous: Mapping[str, Any] | None, metric: str
) -> int | None:
    if previous is None:
        return None
    return _whole(_number(current.get(metric, 0)) - _number(previous.get(metric, 0)))


def _period_key(site: str, period: _PeriodData) -> str:
    return f"{site}|week|{period.start.isoformat()}|{period.end.isoformat()}"


def _overview_hint(
    current: Mapping[str, Any], previous: Mapping[str, Any] | None
) -> str:
    if previous is None:
        return "历史对照周期；本行不计算环比。"
    sessions_delta = _metric_delta(current, previous, "ga4_sessions") or 0
    clicks_delta = _metric_delta(current, previous, "gsc_clicks") or 0
    inquiries_delta = _metric_delta(current, previous, "accepted_inquiries") or 0
    return (
        f"访问较上期{_signed_text(sessions_delta, '次')}；"
        f"自然搜索点击较上期{_signed_text(clicks_delta, '次')}；"
        f"官网入库询盘较上期{_signed_text(inquiries_delta, '条')}。"
    )


def _row_hint(metrics: Mapping[str, Any]) -> str:
    sessions = _number(metrics.get("ga4_sessions", 0))
    clicks = _number(metrics.get("gsc_clicks", 0))
    impressions = _number(metrics.get("gsc_impressions", 0))
    inquiries = _number(metrics.get("accepted_inquiries", 0))
    if inquiries:
        return f"本期产生 {int(inquiries)} 条官网入库询盘，继续观察连续周期。"
    if impressions >= 100 and clicks / impressions < 0.01:
        return "曝光较高但点击率不足 1%，优先检查标题、摘要与搜索意图。"
    if sessions >= 50:
        return "访问较高但暂无入库询盘，检查页面行动按钮和表单路径。"
    return "当前样本较小，建议结合连续周期判断。"


def _signed_text(value: int, unit: str) -> str:
    if value > 0:
        return f"增加 {value}{unit}"
    if value < 0:
        return f"减少 {abs(value)}{unit}"
    return "持平"
