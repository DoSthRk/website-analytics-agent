"""Build aggregate-only Feishu dashboard payloads from redacted local caches.

The script never calls GA4, GSC, Feishu, or the inquiry database. It reads the
approved cache artifacts produced by ``python -m website_analytics`` and emits
only weekly, product-level aggregates suitable for controlled Base writes.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from website_analytics.product_mapping import build_product_report, load_product_mapping
from website_analytics.page_classification import build_page_dimension, load_page_classification


CHINA_STANDARD_TIME = timezone(timedelta(hours=8), name="Asia/Shanghai")
DATE_BOUNDARY_NOTE = (
    "GA4按属性时区统计；GSC按Pacific Time统计；询盘按网站服务器日历统计。"
)


@dataclass(frozen=True)
class CachedWeek:
    start: date
    end: date
    ga4: Mapping[str, Any]
    gsc: Mapping[str, Any]
    inquiry: Mapping[str, Any] | None
    refreshed_at: datetime


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--page-classification", type=Path, required=True)
    parser.add_argument("--page-dimension", type=Path, required=True)
    parser.add_argument("--interval-totals", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def _read_json(path: Path) -> Mapping[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise ValueError(f"cache must contain an object: {path.name}")
    return document


def _daily_dates(document: Mapping[str, Any], key: str) -> list[date]:
    rows = document.get(key)
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ValueError(f"cache is missing {key}")
    values: list[date] = []
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("date"), str):
            raise ValueError(f"{key} contains an invalid date row")
        values.append(date.fromisoformat(str(row["date"])))
    if not values:
        raise ValueError(f"{key} must not be empty")
    return sorted(values)


def _source_documents(root: Path, source: str, daily_key: str) -> dict[date, tuple[Mapping[str, Any], Path]]:
    documents: dict[date, tuple[Mapping[str, Any], Path]] = {}
    for path in sorted((root / source).glob("*.json")):
        document = _read_json(path)
        dates = _daily_dates(document, daily_key)
        week_start = dates[0] - timedelta(days=dates[0].weekday())
        week_end = week_start + timedelta(days=6)
        if any(value < week_start or value > week_end for value in dates):
            raise ValueError(f"{daily_key} spans more than one calendar week")
        existing = documents.get(week_start)
        if existing is None or path.stat().st_mtime > existing[1].stat().st_mtime:
            documents[week_start] = (document, path)
    return documents


def _weeks(cache_dir: Path) -> list[CachedWeek]:
    ga4 = _source_documents(cache_dir, "ga4", "GA4 Daily")
    gsc = _source_documents(cache_dir, "gsc", "GSC Daily")
    inquiry = _source_documents(cache_dir, "inquiry", "Inquiry Daily")
    shared = sorted(set(ga4) & set(gsc))
    if not shared:
        raise ValueError("no shared GA4/GSC weekly cache ranges were found")
    result: list[CachedWeek] = []
    for week_start in shared:
        sources = [ga4[week_start][1], gsc[week_start][1]]
        inquiry_entry = inquiry.get(week_start)
        if inquiry_entry is not None:
            sources.append(inquiry_entry[1])
        refreshed_at = datetime.fromtimestamp(
            max(path.stat().st_mtime for path in sources), tz=CHINA_STANDARD_TIME
        )
        result.append(
            CachedWeek(
                start=week_start,
                end=week_start + timedelta(days=6),
                ga4=ga4[week_start][0],
                gsc=gsc[week_start][0],
                inquiry=inquiry_entry[0] if inquiry_entry else None,
                refreshed_at=refreshed_at,
            )
        )
    return result


def _rows(document: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    raw = document.get(key, [])
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError(f"invalid row collection: {key}")
    if not all(isinstance(row, Mapping) for row in raw):
        raise ValueError(f"invalid row in collection: {key}")
    return list(raw)


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("metric must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("metric must be finite")
    return number


def _sum(rows: Sequence[Mapping[str, Any]], field: str) -> float:
    return sum(_number(row.get(field, 0)) for row in rows)


def _details(week: CachedWeek) -> dict[str, list[Mapping[str, Any]]]:
    details = {
        "GA4 Daily": _rows(week.ga4, "GA4 Daily"),
        "GA4 Pages": _rows(week.ga4, "GA4 Pages"),
        "GSC Daily": _rows(week.gsc, "GSC Daily"),
        "GSC Pages": _rows(week.gsc, "GSC Pages"),
        "GSC Queries": _rows(week.gsc, "GSC Queries"),
    }
    if week.inquiry is not None:
        details["Inquiry Daily"] = _rows(week.inquiry, "Inquiry Daily")
        details["Inquiry Pages"] = _rows(week.inquiry, "Inquiry Pages")
    return details


def _overall(
    week: CachedWeek, interval_totals: Mapping[str, Mapping[str, Any]]
) -> dict[str, float | None]:
    gsc_daily = _rows(week.gsc, "GSC Daily")
    ga4_totals = interval_totals.get(week.start.isoformat())
    if not isinstance(ga4_totals, Mapping):
        raise ValueError(f"missing approved interval totals for {week.start.isoformat()}")
    clicks = _sum(gsc_daily, "clicks")
    impressions = _sum(gsc_daily, "impressions")
    position = (
        sum(_number(row.get("position", 0)) * _number(row.get("impressions", 0)) for row in gsc_daily)
        / impressions
        if impressions
        else None
    )
    inquiry_daily = _rows(week.inquiry, "Inquiry Daily") if week.inquiry is not None else []
    return {
        "sessions": _number(ga4_totals.get("sessions")),
        "activeUsers": _number(ga4_totals.get("activeUsers")),
        "keyEvents": _number(ga4_totals.get("keyEvents")),
        "clicks": clicks,
        "impressions": impressions,
        "ctr": clicks / impressions if impressions else None,
        "position": position,
        "stored": _sum(inquiry_daily, "storedSubmissions") if week.inquiry is not None else None,
        "inquiries": _sum(inquiry_daily, "nonQuarantinedSubmissions") if week.inquiry is not None else None,
    }


def _delta(current: float | None, previous: float | None) -> float | None:
    return current - previous if current is not None and previous is not None else None


def _whole_number(value: float | None) -> int | None:
    return int(value) if value is not None else None


def _date_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _period_key(week: CachedWeek) -> str:
    return f"genemedi-net|{week.start.isoformat()}|{week.end.isoformat()}"


def _period_label(week: CachedWeek) -> str:
    return f"{week.start.isoformat()} 至 {week.end.isoformat()}"


def _change_text(value: float | None, unit: str = "") -> str:
    if value is None:
        return "无可比数据"
    number = int(value) if value.is_integer() else round(value, 2)
    if number > 0:
        return f"增加 {number}{unit}"
    if number < 0:
        return f"减少 {abs(number)}{unit}"
    return "持平"


def _overall_summary(current: Mapping[str, float | None], previous: Mapping[str, float | None]) -> str:
    return (
        f"官网访问较上周{_change_text(_delta(current['sessions'], previous['sessions']), '次')}；"
        f"Google自然搜索点击较上周{_change_text(_delta(current['clicks'], previous['clicks']), '次')}；"
        f"GA4表单提交事件较上周{_change_text(_delta(current['keyEvents'], previous['keyEvents']), '次')}；"
        f"官网入库询盘较上周{_change_text(_delta(current['inquiries'], previous['inquiries']), '条')}。"
    )


def _product_hint(product: str, sessions: float, clicks: float, inquiries: float | None) -> str:
    if inquiries is None:
        return "询盘数据缺失，本周期仅观察访问和自然搜索表现。"
    if inquiries > 0:
        return f"本周期产生 {_whole_number(inquiries)} 条官网入库询盘，建议继续观察连续周表现。"
    if sessions >= 100:
        return "访问较多但本周期未出现官网入库询盘，建议检查产品页行动按钮与表单路径。"
    if clicks < 5:
        return "自然搜索点击较少，优先检查内容覆盖、搜索曝光和标题相关性。"
    return "本周期未出现官网入库询盘，当前样本较小，建议继续观察。"


def _overview_fields() -> list[str]:
    return [
        "周期", "周期键", "站点", "周期开始", "周期结束", "官网访问次数",
        "官网访客数", "表单提交事件（GA4）", "Google自然搜索点击", "Google搜索曝光", "搜索点击率",
        "平均搜索排名", "表单入库总数", "官网入库询盘", "数据状态", "数据更新时间",
        "数据口径说明", "当前周期", "访问较上周", "搜索点击较上周",
        "表单事件较上周", "入库询盘较上周", "运营摘要",
    ]


def _product_fields() -> list[str]:
    return [
        "产品周期键", "周期键", "站点", "产品大类", "周期开始", "周期结束",
        "官网访问次数", "Google自然搜索点击", "Google搜索曝光", "搜索点击率",
        "表单入库总数", "官网入库询盘", "纳入统计页面数", "统计状态", "数据更新时间",
        "当前周期", "周期标签", "数据完整性", "访问较上周", "搜索点击较上周",
        "搜索曝光较上周", "入库询盘较上周", "运营提示",
    ]


def _overview_row(
    week: CachedWeek,
    metrics: Mapping[str, float | None],
    previous: Mapping[str, float | None] | None,
    *,
    current: bool,
) -> list[Any]:
    previous = previous or {}
    summary = _overall_summary(metrics, previous) if previous else "首个可用周期，暂无上周对比。"
    return [
        _period_label(week), _period_key(week), "genemedi-net",
        f"{week.start.isoformat()} 00:00:00", f"{week.end.isoformat()} 00:00:00",
        _whole_number(metrics["sessions"]), _whole_number(metrics["activeUsers"]),
        _whole_number(metrics["keyEvents"]),
        _whole_number(metrics["clicks"]), _whole_number(metrics["impressions"]), metrics["ctr"],
        metrics["position"], _whole_number(metrics["stored"]), _whole_number(metrics["inquiries"]),
        "complete" if week.inquiry is not None else "partial", _date_time(week.refreshed_at),
        DATE_BOUNDARY_NOTE, current,
        _whole_number(_delta(metrics["sessions"], previous.get("sessions"))),
        _whole_number(_delta(metrics["clicks"], previous.get("clicks"))),
        _whole_number(_delta(metrics["keyEvents"], previous.get("keyEvents"))),
        _whole_number(_delta(metrics["inquiries"], previous.get("inquiries"))), summary,
    ]


def _product_rows(
    week: CachedWeek,
    report: Mapping[str, Any],
    previous_report: Mapping[str, Any] | None,
    product_names: Mapping[str, str],
    *,
    current: bool,
) -> list[list[Any]]:
    current_lines = {
        str(row["reportLineId"]): row for row in report.get("reportLines", []) if isinstance(row, Mapping)
    }
    previous_lines = {
        str(row["reportLineId"]): row
        for row in (previous_report or {}).get("reportLines", [])
        if isinstance(row, Mapping)
    }
    inquiry_lines = {
        str(row["reportLineId"]): row
        for row in report.get("inquiryReportLines", [])
        if isinstance(row, Mapping)
    }
    previous_inquiry_lines = {
        str(row["reportLineId"]): row
        for row in (previous_report or {}).get("inquiryReportLines", [])
        if isinstance(row, Mapping)
    }
    rows: list[list[Any]] = []
    for identifier, product in product_names.items():
        line = current_lines[identifier]
        previous_line = previous_lines.get(identifier, {})
        inquiry = inquiry_lines.get(identifier, {})
        previous_inquiry = previous_inquiry_lines.get(identifier, {})
        sessions = _number(line["ga4SessionsCurrent"])
        clicks = _number(line["gscClicksCurrent"])
        impressions = _number(line["gscImpressionsCurrent"])
        stored = (
            _number(inquiry["storedSubmissionsCurrent"])
            if week.inquiry is not None and "storedSubmissionsCurrent" in inquiry
            else None
        )
        inquiries = (
            _number(inquiry["nonQuarantinedSubmissionsCurrent"])
            if week.inquiry is not None and "nonQuarantinedSubmissionsCurrent" in inquiry
            else None
        )
        previous_sessions = (
            _number(previous_line["ga4SessionsCurrent"])
            if "ga4SessionsCurrent" in previous_line
            else None
        )
        previous_clicks = (
            _number(previous_line["gscClicksCurrent"])
            if "gscClicksCurrent" in previous_line
            else None
        )
        previous_impressions = (
            _number(previous_line["gscImpressionsCurrent"])
            if "gscImpressionsCurrent" in previous_line
            else None
        )
        previous_inquiries = (
            _number(previous_inquiry["nonQuarantinedSubmissionsCurrent"])
            if "nonQuarantinedSubmissionsCurrent" in previous_inquiry
            else None
        )
        rows.append(
            [
                f"{_period_key(week)}|{identifier}", _period_key(week), "genemedi-net", product,
                f"{week.start.isoformat()} 00:00:00", f"{week.end.isoformat()} 00:00:00",
                _whole_number(sessions), _whole_number(clicks), _whole_number(impressions),
                clicks / impressions if impressions else None, _whole_number(stored),
                _whole_number(inquiries), _whole_number(_number(line["currentCanonicalPages"])), "mapped",
                _date_time(week.refreshed_at), current, _period_label(week),
                "完整" if week.inquiry is not None else "仅流量（询盘缺失）",
                _whole_number(_delta(sessions, previous_sessions)),
                _whole_number(_delta(clicks, previous_clicks)),
                _whole_number(_delta(impressions, previous_impressions)),
                _whole_number(_delta(inquiries, previous_inquiries)),
                _product_hint(product, sessions, clicks, inquiries) if current else "",
            ]
        )
    return rows


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def main() -> int:
    args = _arguments()
    mapping = load_product_mapping(args.mapping, "genemedi-net")
    if mapping is None:
        raise ValueError("product mapping is required")
    product_names = {
        line.identifier: line.name for line in mapping.report_lines
    }
    classification = load_page_classification(args.page_classification, "genemedi-net")
    raw_dimension = _read_json(args.page_dimension)
    dimension_rows = raw_dimension.get("rows")
    if not isinstance(dimension_rows, Sequence) or isinstance(dimension_rows, (str, bytes)):
        raise ValueError("page dimension snapshot must contain rows")
    if not all(isinstance(row, Mapping) for row in dimension_rows):
        raise ValueError("page dimension snapshot contains an invalid row")
    page_dimension = build_page_dimension(classification, list(dimension_rows))
    weeks = _weeks(args.cache_dir)
    raw_interval_totals = _read_json(args.interval_totals)
    interval_totals = {
        str(key): value
        for key, value in raw_interval_totals.items()
        if isinstance(value, Mapping)
    }
    metrics = [_overall(week, interval_totals) for week in weeks]
    reports = [
        build_product_report(mapping, _details(week), {}, page_dimension)
        for week in weeks
    ]
    current_index = len(weeks) - 1

    overview_rows = [
        _overview_row(
            week,
            metrics[index],
            metrics[index - 1] if index else None,
            current=index == current_index,
        )
        for index, week in enumerate(weeks)
    ]
    product_rows = [
        row
        for index, week in enumerate(weeks)
        for row in _product_rows(
            week,
            reports[index],
            reports[index - 1] if index else None,
            product_names,
            current=index == current_index,
        )
    ]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write(
        args.output_dir / "overview-history-create.json",
        {"fields": _overview_fields(), "rows": overview_rows[:-1]},
    )
    _write(
        args.output_dir / "overview-current-patch.json",
        dict(zip(_overview_fields(), overview_rows[-1], strict=True)),
    )
    _write(
        args.output_dir / "product-history-create.json",
        {"fields": _product_fields(), "rows": product_rows[:-len(product_names)]},
    )
    _write(
        args.output_dir / "product-current-patches.json",
        {
            row[3]: dict(zip(_product_fields(), row, strict=True))
            for row in product_rows[-len(product_names):]
        },
    )
    _write(
        args.output_dir / "summary.json",
        {
            "weeks": [_period_label(week) for week in weeks],
            "current_period": _period_label(weeks[-1]),
            "current_status": "complete" if weeks[-1].inquiry is not None else "partial",
            "inquiry_complete_weeks": sum(week.inquiry is not None for week in weeks),
            "overview_summary": overview_rows[-1][-1],
            "product_hints": {
                row[3]: row[-1] for row in product_rows[-len(product_names):]
            },
        },
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "weeks": len(weeks),
                "product_rows": len(product_rows),
                "current_period": _period_label(weeks[-1]),
                "output_dir": str(args.output_dir),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
