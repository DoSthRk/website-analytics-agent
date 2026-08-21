"""Build source-separated Feishu records from approved CLI fetch artifacts."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from website_analytics.periods import AnalyticsPeriod
from website_analytics.page_classification import PageDimension
from website_analytics.product_mapping import ProductMapping, build_product_report


DATE_BOUNDARY_NOTE = (
    "GA4按属性时区统计；GSC按Pacific Time统计；"
    "询盘按网站服务器日历统计。三者是独立数据源，不直接视为转化漏斗。"
)


def cache_path_for_period(
    root: Path,
    site: str,
    source: str,
    period: AnalyticsPeriod,
) -> Path:
    """Return the deterministic cache path written by the approved CLI."""
    request = {
        "source": source,
        "start": period.start.isoformat(),
        "end": period.end.isoformat(),
    }
    canonical = json.dumps(
        request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return root / site / source / f"{digest}.json"


def load_period_details(root: Path, site: str, period: AnalyticsPeriod) -> dict[str, list[dict[str, Any]]]:
    """Load only redacted cache files produced by one successful CLI fetch."""
    details: dict[str, list[dict[str, Any]]] = {}
    for source in ("ga4", "gsc", "inquiry"):
        path = cache_path_for_period(root, site, source, period)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"approved {source} cache is unavailable") from error
        if not isinstance(document, Mapping):
            raise ValueError(f"approved {source} cache must contain an object")
        for name, rows in document.items():
            if not isinstance(name, str) or not isinstance(rows, list) or not all(
                isinstance(row, Mapping) for row in rows
            ):
                raise ValueError(f"approved {source} cache has an invalid collection")
            details[name] = [dict(row) for row in rows]
    return details


def build_dashboard_records(
    *,
    site: str,
    period: AnalyticsPeriod,
    plan: Mapping[str, Any],
    current_result: Mapping[str, Any],
    current_details: Mapping[str, Sequence[Mapping[str, Any]]],
    previous_result: Mapping[str, Any] | None,
    previous_details: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    mapping: ProductMapping,
    page_dimension: PageDimension,
    sync_batch: str,
) -> dict[str, Any]:
    """Return one overview row and one row per approved product report line."""
    _require_complete_fetch(current_result)
    if previous_result is not None:
        _require_complete_fetch(previous_result)
    expected_key = str(plan.get("periodKey", ""))
    if not expected_key:
        raise ValueError("period plan is missing its stable key")

    current_totals = _totals(current_result)
    previous_totals = _totals(previous_result) if previous_result is not None else {}
    refreshed_at = _freshness(current_result)
    common = _common_fields(
        site=site,
        period=period,
        plan=plan,
        refreshed_at=refreshed_at,
        sync_batch=sync_batch,
    )
    inquiries = _source_metric(current_totals, "inquiry", "nonQuarantinedSubmissions")
    previous_inquiries = _optional_source_metric(
        previous_totals, "inquiry", "nonQuarantinedSubmissions"
    )
    product_report = build_product_report(
        mapping,
        current_details,
        previous_details or {},
        page_dimension,
    )
    page_types = _index(product_report.get("pageTypeLines"), "pageTypeId")
    coverage = product_report.get("classificationCoverage")
    if not isinstance(coverage, Mapping):
        raise ValueError("product report is missing classification coverage")
    product_type = _required_page_type(page_types, "product_page")
    information_type = _required_page_type(page_types, "information_page")
    unclassified_types = [
        _required_page_type(page_types, page_type)
        for page_type in (
            "technical_page",
            "unknown_unmapped",
            "invalid_broken",
            "pdf_asset",
        )
    ]
    overview = {
        "周期": period.label,
        "周期键": expected_key,
        **common,
        "数据状态": plan["status"],
        "数据口径说明": DATE_BOUNDARY_NOTE,
        "官网访问次数": _whole(_source_metric(current_totals, "ga4", "sessions")),
        "官网访客数": _whole(_source_metric(current_totals, "ga4", "activeUsers")),
        "表单提交事件（GA4）": _whole(_source_metric(current_totals, "ga4", "keyEvents")),
        "Google自然搜索点击": _whole(_source_metric(current_totals, "gsc", "clicks")),
        "Google搜索曝光": _whole(_source_metric(current_totals, "gsc", "impressions")),
        "搜索点击率": _nullable_source_metric(current_totals, "gsc", "ctr"),
        "平均搜索排名": _nullable_source_metric(current_totals, "gsc", "position"),
        "表单入库总数": _whole(
            _source_metric(current_totals, "inquiry", "storedSubmissions")
        ),
        "官网入库询盘": _whole(inquiries),
        "页面分类版本": page_dimension.version,
        "产品页页面数": _whole(_number(product_type.get("currentCanonicalPages"))),
        "信息页页面数": _whole(_number(information_type.get("currentCanonicalPages"))),
        "未分类页面数": _whole(
            sum(_number(row.get("currentCanonicalPages")) for row in unclassified_types)
        ),
        "产品页访问次数": _whole(_number(product_type.get("ga4SessionsCurrent"))),
        "信息页访问次数": _whole(_number(information_type.get("ga4SessionsCurrent"))),
        "未分类页面访问次数": _whole(
            sum(_number(row.get("ga4SessionsCurrent")) for row in unclassified_types)
        ),
        "产品页Google搜索点击": _whole(_number(product_type.get("gscClicksCurrent"))),
        "信息页Google搜索点击": _whole(_number(information_type.get("gscClicksCurrent"))),
        "未分类页面Google搜索点击": _whole(
            sum(_number(row.get("gscClicksCurrent")) for row in unclassified_types)
        ),
        "产品页官网入库询盘": _whole(
            _number(product_type.get("nonQuarantinedSubmissionsCurrent"))
        ),
        "信息页官网入库询盘": _whole(
            _number(information_type.get("nonQuarantinedSubmissionsCurrent"))
        ),
        "未分类页面官网入库询盘": _whole(
            sum(
                _number(row.get("nonQuarantinedSubmissionsCurrent"))
                for row in unclassified_types
            )
        ),
        "页面访问分类覆盖率": _nullable_number(coverage.get("ga4ClassifiedRate")),
        "访问较上周": _whole(
            _delta(
                _source_metric(current_totals, "ga4", "sessions"),
                _optional_source_metric(previous_totals, "ga4", "sessions"),
            )
        ),
        "搜索点击较上周": _whole(
            _delta(
                _source_metric(current_totals, "gsc", "clicks"),
                _optional_source_metric(previous_totals, "gsc", "clicks"),
            )
        ),
        "表单事件较上周": _whole(
            _delta(
                _source_metric(current_totals, "ga4", "keyEvents"),
                _optional_source_metric(previous_totals, "ga4", "keyEvents"),
            )
        ),
        "入库询盘较上周": _whole(_delta(inquiries, previous_inquiries)),
        "运营摘要": _overall_summary(current_totals, previous_totals),
    }

    current_lines = _index(product_report.get("reportLines"), "reportLineId")
    inquiry_lines = _index(product_report.get("inquiryReportLines"), "reportLineId")
    products: list[dict[str, Any]] = []
    for report_line in mapping.report_lines:
        line = current_lines.get(report_line.identifier)
        inquiry = inquiry_lines.get(report_line.identifier)
        if line is None or inquiry is None:
            raise ValueError("product report is missing an approved report line")
        sessions = _number(line.get("ga4SessionsCurrent"))
        clicks = _number(line.get("gscClicksCurrent"))
        impressions = _number(line.get("gscImpressionsCurrent"))
        product_inquiries = _number(inquiry.get("nonQuarantinedSubmissionsCurrent"))
        products.append(
            {
                "产品周期键": f"{expected_key}|{report_line.identifier}",
                "周期键": expected_key,
                "产品大类": report_line.name,
                **common,
                "周期标签": period.label,
                "数据完整性": (
                    "完整" if plan["status"] == "complete" else "初步数据"
                ),
                "官网访问次数": _whole(sessions),
                "Google自然搜索点击": _whole(clicks),
                "Google搜索曝光": _whole(impressions),
                "搜索点击率": clicks / impressions if impressions else None,
                "表单入库总数": _whole(_number(inquiry.get("storedSubmissionsCurrent"))),
                "官网入库询盘": _whole(product_inquiries),
                "纳入统计页面数": _whole(_number(line.get("currentCanonicalPages"))),
                "统计状态": "mapped",
                "访问较上周": _whole(_number(line.get("ga4SessionsDelta"))),
                "搜索点击较上周": _whole(_number(line.get("gscClicksDelta"))),
                "搜索曝光较上周": _whole(_number(line.get("gscImpressionsDelta"))),
                "入库询盘较上周": _whole(
                    _number(inquiry.get("nonQuarantinedSubmissionsDelta"))
                ),
                "运营提示": _product_hint(sessions, clicks, product_inquiries),
            }
        )
    return {"overview": overview, "products": products}


def _common_fields(
    *,
    site: str,
    period: AnalyticsPeriod,
    plan: Mapping[str, Any],
    refreshed_at: datetime,
    sync_batch: str,
) -> dict[str, Any]:
    source_through = plan.get("sourceAvailableThrough")
    if not isinstance(source_through, Mapping):
        raise ValueError("period plan is missing source coverage")
    windows = plan.get("dashboardWindows")
    if not isinstance(windows, list) or not all(isinstance(value, str) for value in windows):
        raise ValueError("period plan has invalid dashboard windows")
    status = plan.get("status")
    if status not in {"complete", "preliminary", "partial"}:
        raise ValueError("period plan has invalid status")
    return {
        "站点": site,
        "周期开始": _date_cell(period.start),
        "周期结束": _date_cell(period.end),
        "数据更新时间": refreshed_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "当前周期": "当前周期" in windows,
        "统计粒度": period.kind_label,
        "滚动天数": period.window_days,
        "GA4数据截至": _coverage_cell(period, source_through.get("ga4")),
        "GSC数据截至": _coverage_cell(period, source_through.get("gsc")),
        "询盘数据截至": _coverage_cell(period, source_through.get("inquiry")),
        "是否最终值": bool(plan.get("isFinal")),
        "看板窗口": windows,
        "同步批次": sync_batch,
    }


def _coverage_cell(period: AnalyticsPeriod, value: object) -> str | None:
    if not isinstance(value, str):
        raise ValueError("source coverage date is missing")
    available = date.fromisoformat(value)
    if available < period.start:
        return None
    return _date_cell(min(period.end, available))


def _date_cell(value: date) -> str:
    return f"{value.isoformat()} 00:00:00"


def _freshness(result: Mapping[str, Any]) -> datetime:
    value = result.get("freshness")
    if not isinstance(value, str):
        raise ValueError("fetch freshness is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("fetch freshness must include a timezone")
    return parsed


def _require_complete_fetch(result: Mapping[str, Any]) -> None:
    if result.get("status") != "ok" or result.get("complete") is not True:
        raise ValueError("dashboard records require a complete three-source fetch")
    sources = result.get("sources")
    if not isinstance(sources, Mapping) or set(sources) != {"ga4", "gsc", "inquiry"}:
        raise ValueError("fetch must contain GA4, GSC, and inquiry statuses")
    if any(not isinstance(value, Mapping) or value.get("status") != "ok" for value in sources.values()):
        raise ValueError("dashboard records require all three sources to be ok")


def _totals(result: Mapping[str, Any] | None) -> Mapping[str, Mapping[str, Any]]:
    if result is None:
        return {}
    totals = result.get("totals")
    if not isinstance(totals, Mapping):
        raise ValueError("fetch totals are missing")
    normalized: dict[str, Mapping[str, Any]] = {}
    for source, values in totals.items():
        if not isinstance(source, str) or not isinstance(values, Mapping):
            raise ValueError("fetch totals have an invalid source")
        normalized[source] = values
    return normalized


def _source_metric(totals: Mapping[str, Mapping[str, Any]], source: str, metric: str) -> float:
    values = totals.get(source)
    if not isinstance(values, Mapping) or metric not in values:
        raise ValueError(f"fetch totals are missing {source}.{metric}")
    return _number(values[metric])


def _optional_source_metric(
    totals: Mapping[str, Mapping[str, Any]], source: str, metric: str
) -> float | None:
    if not totals:
        return None
    return _source_metric(totals, source, metric)


def _nullable_source_metric(
    totals: Mapping[str, Mapping[str, Any]], source: str, metric: str
) -> float | None:
    values = totals.get(source)
    if not isinstance(values, Mapping):
        raise ValueError(f"fetch totals are missing source {source}")
    if metric not in values:
        return None
    return _number(values[metric])


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("metric must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("metric must be finite")
    return number


def _nullable_number(value: object) -> float | None:
    if value is None:
        return None
    return _number(value)


def _required_page_type(
    page_types: Mapping[str, Mapping[str, Any]], page_type: str
) -> Mapping[str, Any]:
    row = page_types.get(page_type)
    if row is None:
        raise ValueError(f"product report is missing page type {page_type}")
    return row


def _whole(value: float | None) -> int | None:
    return int(value) if value is not None else None


def _delta(current: float, previous: float | None) -> float | None:
    return current - previous if previous is not None else None


def _change_text(value: float | None, unit: str) -> str:
    if value is None:
        return "暂无可比数据"
    whole = int(value)
    if whole > 0:
        return f"增加 {whole}{unit}"
    if whole < 0:
        return f"减少 {abs(whole)}{unit}"
    return "持平"


def _overall_summary(
    current: Mapping[str, Mapping[str, Any]],
    previous: Mapping[str, Mapping[str, Any]],
) -> str:
    if not previous:
        return "首个可用周期，暂无上一周期对比。"
    return (
        "官网访问较上一周期"
        + _change_text(
            _source_metric(current, "ga4", "sessions")
            - _source_metric(previous, "ga4", "sessions"),
            "次",
        )
        + "；Google自然搜索点击较上一周期"
        + _change_text(
            _source_metric(current, "gsc", "clicks")
            - _source_metric(previous, "gsc", "clicks"),
            "次",
        )
        + "；GA4表单提交事件较上一周期"
        + _change_text(
            _source_metric(current, "ga4", "keyEvents")
            - _source_metric(previous, "ga4", "keyEvents"),
            "次",
        )
        + "；官网入库询盘较上一周期"
        + _change_text(
            _source_metric(current, "inquiry", "nonQuarantinedSubmissions")
            - _source_metric(previous, "inquiry", "nonQuarantinedSubmissions"),
            "条",
        )
        + "。"
    )


def _product_hint(sessions: float, clicks: float, inquiries: float) -> str:
    if inquiries > 0:
        return f"本周期产生 {int(inquiries)} 条官网入库询盘，建议继续观察连续周期表现。"
    if sessions >= 100:
        return "访问较多但暂未出现官网入库询盘，建议检查产品页行动按钮与表单路径。"
    if clicks < 5:
        return "自然搜索点击较少，优先检查内容覆盖、搜索曝光和标题相关性。"
    return "暂未出现官网入库询盘，当前样本较小，建议继续观察。"


def _index(value: object, key: str) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("product report collection is invalid")
    result: dict[str, Mapping[str, Any]] = {}
    for row in value:
        if not isinstance(row, Mapping) or not isinstance(row.get(key), str):
            raise ValueError("product report row is invalid")
        result[str(row[key])] = row
    return result
