"""Weekly and monthly period facts with history baselines, built from daily facts.

Input is the daily product-funnel and overview records produced by
``history_recompute`` / the daily sync. Output is three record sets:

* ``product_period``: one row per grain x period x scope x comparison mode.
* ``site_period``: the same for the whole site, from the overview table.
* ``product_change``: the latest complete week in long form (one row per
  scope x comparison mode x observed item), keyed without the period so it is
  updated in place every week.

Rules (see the period-table design notes):

* Weeks are ISO weeks (Monday-Sunday); months are calendar months. A period
  that starts before the first available day is not emitted.
* Status: ``完整`` when every day is present and final, ``进行中`` when the
  period reaches past the last final day, ``有缺日`` otherwise.
* Ratios are sum(numerator) / sum(denominator), never an average of ratios.
* Rolling baselines take the most recent N eligible periods (complete and not
  excluded), looking back at most N + extra periods; fewer than N -> empty.
* Year-over-year uses the same ISO week / month of the previous year, empty
  when that period is not eligible.
* Change and anomaly are computed only for complete periods; anomaly also
  requires the period not to be excluded and the baseline to reach a minimum
  volume.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from website_analytics.product_mapping import ProductMapping

WEEK = "周"
MONTH = "月"
COMPLETE = "完整"
IN_PROGRESS = "进行中"
MISSING_DAYS = "有缺日"

ALL_PRODUCTS = "ALL_PRODUCTS"
SITE_SCOPE = "SITE"


@dataclass(frozen=True)
class Mode:
    name: str
    count: int = 0  # 0 means year-over-year
    extra: int = 0


MODES: dict[str, tuple[Mode, ...]] = {
    WEEK: (Mode("前8周均值", 8, 4), Mode("前12周均值", 12, 4), Mode("去年同期")),
    MONTH: (Mode("前3月均值", 3, 2), Mode("前6月均值", 6, 2), Mode("去年同月")),
}
ALERT_MODE = {WEEK: "前8周均值", MONTH: "前3月均值"}

ANOMALY_CHANGE = 0.30


@dataclass(frozen=True)
class Quantity:
    key: str
    label: str
    source: str
    min_volume: float
    alert: bool = True


@dataclass(frozen=True)
class Ratio:
    key: str
    label: str
    numerator: str
    denominator: str
    note: str = ""
    alert: bool = True


@dataclass(frozen=True)
class MetricSet:
    quantities: tuple[Quantity, ...]
    ratios: tuple[Ratio, ...]
    hidden_sums: tuple[tuple[str, str], ...] = ()

    def by_key(self) -> dict[str, Quantity]:
        return {quantity.key: quantity for quantity in self.quantities}

    def alerting(self) -> set[str]:
        return {item.key for item in (*self.quantities, *self.ratios) if item.alert}


# GA4 session counts on genemedi.net are dominated by bot traffic in many weeks,
# while engaged sessions, Search Console and stored inquiries stay stable.
# Session-based items are shown with baselines but never raise an anomaly.
BOT_NOTE = "受爬虫流量影响，仅展示，不触发异常提醒"


PRODUCT_METRICS = MetricSet(
    quantities=(
        Quantity("impressions", "Google 搜索曝光", "gsc_impressions", 500),
        Quantity("clicks", "Google 自然搜索点击", "gsc_clicks", 20),
        Quantity("landing_sessions", "产品落地会话（全部渠道）", "ga4_product_landing_sessions_all_channels", 20, alert=False),
        Quantity("engaged_sessions", "产品落地有效会话", "ga4_product_landing_engaged_sessions_all_channels", 20),
        Quantity("accepted_inquiries", "官网表单入库数（非隔离）", "db_accepted_inquiries", 5),
    ),
    ratios=(
        Ratio("click_rate", "搜索点击率", "clicks", "impressions"),
        Ratio("arrival_ratio", "到站转化率（规模对照）", "landing_sessions", "clicks", "跨来源规模对照，可能大于100%，不是严格转化率；" + BOT_NOTE, alert=False),
        Ratio("engaged_rate", "有效会话率", "engaged_sessions", "landing_sessions", "可用来判断爬虫多少；" + BOT_NOTE, alert=False),
        Ratio("inquiry_ratio", "询盘转化率（规模对照）", "accepted_inquiries", "engaged_sessions", "跨来源规模对照，不能证明询盘来自这些会话"),
    ),
    hidden_sums=(("position_weight", "gsc_position_impression_sum"),),
)

SITE_METRICS = MetricSet(
    quantities=(
        Quantity("sessions", "官网访问次数", "ga4_sessions", 20, alert=False),
        Quantity("product_page_sessions", "产品页访问次数", "product_page_sessions", 20, alert=False),
        Quantity("information_page_sessions", "信息页访问次数", "information_page_sessions", 20, alert=False),
        Quantity("other_page_sessions", "其他页面访问次数", "other_page_sessions", 20, alert=False),
        Quantity("clicks", "Google 自然搜索点击", "gsc_clicks", 20),
        Quantity("impressions", "Google 搜索曝光", "gsc_impressions", 500),
        Quantity("accepted_inquiries", "官网表单入库数（非隔离）", "accepted_inquiries", 5),
    ),
    ratios=(Ratio("click_rate", "搜索点击率", "clicks", "impressions"),),
)


@dataclass(frozen=True)
class Period:
    grain: str
    key: str
    start: date
    end: date
    year: int
    sequence: int

    def days(self) -> list[date]:
        return [self.start + timedelta(days=offset) for offset in range((self.end - self.start).days + 1)]

    def previous_year_key(self) -> str:
        if self.grain == WEEK:
            return f"{self.year - 1}-W{self.sequence:02d}"
        return f"{self.year - 1}-{self.sequence:02d}"


@dataclass(frozen=True)
class Scope:
    identifier: str
    name: str
    level: str
    category_l1: str
    category_l2: str
    members: frozenset[str]


@dataclass(frozen=True)
class Exclusion:
    start: date
    end: date
    scopes: frozenset[str] | None  # None means every scope
    reason: str

    def applies(self, period: Period, scope_id: str) -> bool:
        overlaps = self.start <= period.end and self.end >= period.start
        return overlaps and (self.scopes is None or scope_id in self.scopes)


@dataclass
class _Cell:
    period: Period
    scope: Scope
    status: str
    days_present: int
    sums: dict[str, float]
    excluded_reason: str = ""

    @property
    def eligible(self) -> bool:
        return self.status == COMPLETE and not self.excluded_reason


@dataclass(frozen=True)
class PeriodBuild:
    records: dict[str, list[dict[str, Any]]]
    candidates: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- periods


def week_of(day: date) -> Period:
    year, week, weekday = day.isocalendar()
    start = day - timedelta(days=weekday - 1)
    return Period(WEEK, f"{year}-W{week:02d}", start, start + timedelta(days=6), year, week)


def month_of(day: date) -> Period:
    start = day.replace(day=1)
    following = (start + timedelta(days=32)).replace(day=1)
    return Period(MONTH, f"{day.year}-{day.month:02d}", start, following - timedelta(days=1), day.year, day.month)


def periods_between(grain: str, first_day: date, last_day: date) -> list[Period]:
    """Periods that start on or after ``first_day`` and contain a day <= ``last_day``."""
    of = week_of if grain == WEEK else month_of
    periods: list[Period] = []
    period = of(first_day)
    if period.start < first_day:
        period = of(period.end + timedelta(days=1))
    while period.start <= last_day:
        periods.append(period)
        period = of(period.end + timedelta(days=1))
    return periods


# ---------------------------------------------------------------- scopes


def product_scopes(mapping: ProductMapping) -> list[Scope]:
    """All products, every L1, L2 groups with 2+ leaves, and every leaf."""
    leaves = list(mapping.report_lines)
    scopes = [Scope(ALL_PRODUCTS, "全部产品", "全部产品", "", "", frozenset(line.identifier for line in leaves))]
    by_l1: dict[str, list[str]] = defaultdict(list)
    by_l2: dict[tuple[str, str], list[str]] = defaultdict(list)
    for line in leaves:
        by_l1[line.category_l1].append(line.identifier)
        if line.category_l2:
            by_l2[(line.category_l1, line.category_l2)].append(line.identifier)
    for l1, members in by_l1.items():
        scopes.append(Scope(f"L1:{l1}", l1, "一级分类", l1, "", frozenset(members)))
    for (l1, l2), members in by_l2.items():
        if len(members) >= 2:
            scopes.append(Scope(f"L2:{l1}/{l2}", l2, "二级分类", l1, l2, frozenset(members)))
    for line in leaves:
        scopes.append(
            Scope(line.identifier, line.name, "末级产品", line.category_l1, line.category_l2, frozenset({line.identifier}))
        )
    return scopes


SITE_SCOPES = [Scope(SITE_SCOPE, "全站", "全站", "", "", frozenset())]


# ---------------------------------------------------------------- exclusions


def load_exclusions(path: Path, site: str) -> list[Exclusion]:
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if document.get("site") != site:
        raise ValueError("baseline exclusions belong to another site")
    exclusions: list[Exclusion] = []
    for entry in document.get("exclusions") or []:
        start, end = _as_date(entry["start"]), _as_date(entry["end"])
        if end < start:
            raise ValueError("baseline exclusion ends before it starts")
        scope = entry.get("scope", "all")
        scopes = None if scope == "all" else frozenset(str(item) for item in scope)
        reason = str(entry.get("reason") or "").strip()
        if not reason:
            raise ValueError("baseline exclusion needs a reason")
        exclusions.append(Exclusion(start, end, scopes, reason))
    return exclusions


def _as_date(value: object) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


# ---------------------------------------------------------------- build


def build_period_facts(
    *,
    site: str,
    mapping: ProductMapping,
    funnel_daily: Sequence[Mapping[str, Any]],
    overview_daily: Sequence[Mapping[str, Any]],
    last_final: date,
    exclusions: Sequence[Exclusion] = (),
    grains: Sequence[str] = (WEEK, MONTH),
) -> PeriodBuild:
    funnel_by_day = _funnel_by_day(funnel_daily, mapping, last_final)
    overview_by_day = _overview_by_day(overview_daily, last_final)
    if set(funnel_by_day) != set(overview_by_day):
        raise ValueError("funnel and overview daily facts cover different days")
    if not funnel_by_day:
        raise ValueError("no daily facts to aggregate")
    first_day = min(funnel_by_day)
    versions = _versions(funnel_daily, overview_daily)

    records: dict[str, list[dict[str, Any]]] = {"product_period": [], "site_period": [], "product_change": []}
    candidates: list[dict[str, Any]] = []
    summary: dict[str, Any] = {"first_day": first_day.isoformat(), "last_final": last_final.isoformat(), "periods": {}}

    for grain in grains:
        periods = periods_between(grain, first_day, last_final)
        summary["periods"][grain] = {
            "first": periods[0].key if periods else None,
            "last": periods[-1].key if periods else None,
            "count": len(periods),
        }
        product_cells = _cells(periods, product_scopes(mapping), funnel_by_day, PRODUCT_METRICS, last_final, exclusions, _product_sum)
        site_cells = _cells(periods, SITE_SCOPES, overview_by_day, SITE_METRICS, last_final, exclusions, _site_sum)
        records["product_period"] += _period_rows(site, grain, product_cells, PRODUCT_METRICS, versions)
        records["site_period"] += _period_rows(site, grain, site_cells, SITE_METRICS, versions)
        if grain == WEEK:
            candidates += _candidates(product_cells, "landing_sessions", "engaged_sessions")
            candidates += _candidates(site_cells, "sessions", None)
            candidates += _candidates(site_cells, "other_page_sessions", None, click_guard=False)
            records["product_change"] = _change_rows(site, product_cells, versions)

    summary["record_counts"] = {name: len(rows) for name, rows in records.items()}
    summary["candidate_count"] = len(candidates)
    return PeriodBuild(records, candidates, summary)


def _funnel_by_day(rows: Iterable[Mapping[str, Any]], mapping: ProductMapping, last_final: date) -> dict[date, dict[str, Mapping[str, Any]]]:
    expected = {line.identifier for line in mapping.report_lines}
    by_day: dict[date, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in rows:
        day = date.fromisoformat(str(row["data_date"])[:10])
        if day > last_final:
            continue
        line = str(row["product_line_id"])
        if line not in expected:
            raise ValueError(f"funnel row has an unknown product line: {line}")
        if line in by_day[day]:
            raise ValueError(f"duplicate funnel row for {day} {line}")
        by_day[day][line] = row
    for day, lines in by_day.items():
        if set(lines) != expected:
            raise ValueError(f"funnel day {day} does not cover every product line")
    return dict(by_day)


def _overview_by_day(rows: Iterable[Mapping[str, Any]], last_final: date) -> dict[date, Mapping[str, Any]]:
    by_day: dict[date, Mapping[str, Any]] = {}
    for row in rows:
        day = date.fromisoformat(str(row["data_date"])[:10])
        if day > last_final:
            continue
        if day in by_day:
            raise ValueError(f"duplicate overview row for {day}")
        by_day[day] = row
    return by_day


def _versions(funnel: Sequence[Mapping[str, Any]], overview: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    def single(rows: Sequence[Mapping[str, Any]], key: str) -> str:
        values = {str(row.get(key, "")) for row in rows}
        if len(values) != 1:
            raise ValueError(f"daily facts mix {key} values: {sorted(values)}")
        return values.pop()

    return {
        "product_mapping_version": single(funnel, "mapping_version"),
        "page_classification_version": single(overview, "page_classification_version"),
    }


def _product_sum(day_rows: Mapping[str, Mapping[str, Any]], scope: Scope, source: str) -> float:
    return sum(_number(day_rows[line].get(source)) for line in scope.members)


def _site_sum(day_row: Mapping[str, Any], scope: Scope, source: str) -> float:
    return _number(day_row.get(source))


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("daily metric is not a number")
    return float(value)


def _cells(periods, scopes, by_day, metrics: MetricSet, last_final, exclusions, summer) -> dict[str, list[_Cell]]:
    sources = [(quantity.key, quantity.source) for quantity in metrics.quantities] + list(metrics.hidden_sums)
    cells: dict[str, list[_Cell]] = {}
    for scope in scopes:
        series: list[_Cell] = []
        for period in periods:
            present = [day for day in period.days() if day in by_day]
            sums = {key: sum(summer(by_day[day], scope, source) for day in present) for key, source in sources}
            if period.end > last_final:
                status = IN_PROGRESS
            elif len(present) < len(period.days()):
                status = MISSING_DAYS
            else:
                status = COMPLETE
            reasons = [item.reason for item in exclusions if item.applies(period, scope.identifier)]
            series.append(_Cell(period, scope, status, len(present), sums, "；".join(dict.fromkeys(reasons))))
        cells[scope.identifier] = series
    return cells


def _ratio(sums: Mapping[str, float], ratio: Ratio) -> float | None:
    denominator = sums[ratio.denominator]
    return sums[ratio.numerator] / denominator if denominator > 0 else None


@dataclass(frozen=True)
class _Baseline:
    values: dict[str, float | None]
    used: list[str]
    note: str


def _baseline(series: Sequence[_Cell], index: int, mode: Mode, metrics: MetricSet) -> _Baseline:
    keys = [quantity.key for quantity in metrics.quantities] + [key for key, _ in metrics.hidden_sums]
    empty = {key: None for key in keys} | {ratio.key: None for ratio in metrics.ratios}
    if mode.count == 0:
        target = series[index].period.previous_year_key()
        match = next((cell for cell in series if cell.period.key == target), None)
        if match is None:
            return _Baseline(empty, [], f"无{target}数据")
        if not match.eligible:
            why = match.excluded_reason and "已排除" or match.status
            return _Baseline(empty, [], f"{target}{why}，不作对照")
        values = dict(match.sums) | {ratio.key: _ratio(match.sums, ratio) for ratio in metrics.ratios}
        return _Baseline(values, [target], target)

    chosen: list[_Cell] = []
    skipped: list[str] = []
    for offset in range(1, mode.count + mode.extra + 1):
        if index - offset < 0 or len(chosen) == mode.count:
            break
        cell = series[index - offset]
        if cell.eligible:
            chosen.append(cell)
        else:
            skipped.append(f"{cell.period.key}（{'排除' if cell.excluded_reason else cell.status}）")
    if len(chosen) < mode.count:
        return _Baseline(empty, [], f"合格周期不足{mode.count}期")
    totals = {key: sum(cell.sums[key] for cell in chosen) for key in keys}
    values: dict[str, float | None] = {key: totals[key] / len(chosen) for key in keys}
    values |= {ratio.key: _ratio(totals, ratio) for ratio in metrics.ratios}
    used = sorted(cell.period.key for cell in chosen)
    note = f"{used[0]}…{used[-1]}，共{len(used)}期"
    if skipped:
        note += "；跳过 " + "、".join(reversed(skipped))
    return _Baseline(values, used, note)


def _observations(cell: _Cell, metrics: MetricSet) -> dict[str, float | None]:
    values: dict[str, float | None] = {quantity.key: cell.sums[quantity.key] for quantity in metrics.quantities}
    values |= {ratio.key: _ratio(cell.sums, ratio) for ratio in metrics.ratios}
    return values


def _assess(cell: _Cell, current: Mapping[str, float | None], base: _Baseline, metrics: MetricSet):
    """Return (change by item, anomalous item labels, min-volume flags)."""
    quantities = metrics.by_key()
    changes: dict[str, float | None] = {}
    volume_ok: dict[str, bool] = {}
    anomalies: list[str] = []
    items = [(quantity.key, quantity.label, (quantity.key,)) for quantity in metrics.quantities]
    items += [(ratio.key, ratio.label, (ratio.numerator, ratio.denominator)) for ratio in metrics.ratios]
    for key, label, volume_keys in items:
        now, before = current.get(key), base.values.get(key)
        change = None
        if cell.status == COMPLETE and now is not None and before not in (None, 0):
            change = (now - before) / before
        changes[key] = change
        # A ratio needs both its numerator and denominator baselines to reach
        # their minimum volume, so 1 click or 1 inquiry cannot swing it.
        volume_ok[key] = all(
            base.values.get(volume_key) is not None and base.values[volume_key] >= quantities[volume_key].min_volume
            for volume_key in volume_keys
        )
        alerting = key in metrics.alerting()
        if alerting and change is not None and cell.eligible and volume_ok[key] and abs(change) >= ANOMALY_CHANGE:
            anomalies.append(label)
    return changes, anomalies, volume_ok


def _period_rows(site, grain, cells, metrics: MetricSet, versions) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for series in cells.values():
        for index, cell in enumerate(series):
            current = _observations(cell, metrics)
            position = None
            if "position_weight" in cell.sums and cell.sums.get("impressions", 0) > 0:
                position = cell.sums["position_weight"] / cell.sums["impressions"]
            for mode in MODES[grain]:
                base = _baseline(series, index, mode, metrics)
                changes, anomalies, _ = _assess(cell, current, base, metrics)
                row: dict[str, Any] = {
                    "period_key": f"{site}|{grain}|{cell.period.key}|{cell.scope.identifier}|{mode.name}",
                    "site": site,
                    "grain": grain,
                    "period": cell.period.key,
                    "period_start": cell.period.start.isoformat(),
                    "period_end": cell.period.end.isoformat(),
                    "iso_year": cell.period.year,
                    "sequence": cell.period.sequence,
                    "scope_level": cell.scope.level,
                    "scope_id": cell.scope.identifier,
                    "scope_name": cell.scope.name,
                    "category_l1": cell.scope.category_l1,
                    "category_l2": cell.scope.category_l2,
                    "period_status": cell.status,
                    "days_present": cell.days_present,
                    "baseline_excluded": bool(cell.excluded_reason),
                    "exclusion_reason": cell.excluded_reason,
                    "comparison_mode": mode.name,
                    "baseline_periods": len(base.used),
                    "baseline_note": base.note,
                    "is_anomaly": bool(anomalies),
                    "anomaly_items": "、".join(anomalies),
                    **versions,
                }
                for key, value in current.items():
                    row[key] = _round(value)
                    row[f"baseline_{key}"] = _round(base.values.get(key))
                    row[f"change_{key}"] = _round(changes[key])
                if "position_weight" in cell.sums:
                    row["avg_position"] = _round(position)
                    weight, impressions = base.values.get("position_weight"), base.values.get("impressions")
                    row["baseline_avg_position"] = _round(weight / impressions) if weight is not None and impressions else None
                rows.append(row)
    return rows


def _change_rows(site, cells, versions) -> list[dict[str, Any]]:
    """Latest complete week, one row per scope x mode x observed item."""
    complete = [cell.period for series in cells.values() for cell in series if cell.status == COMPLETE]
    if not complete:
        return []
    latest = max(complete, key=lambda period: period.start)
    # Counts and ratios share one number column here, so ratios are stored in
    # percent points and their item name says so.
    items = [(quantity.key, quantity.label, "" if quantity.alert else BOT_NOTE, 1) for quantity in PRODUCT_METRICS.quantities]
    items += [(ratio.key, ratio.label, ratio.note, 100) for ratio in PRODUCT_METRICS.ratios]
    order = {"impressions": 1, "click_rate": 2, "clicks": 3, "arrival_ratio": 4, "landing_sessions": 5,
             "engaged_rate": 6, "engaged_sessions": 7, "inquiry_ratio": 8, "accepted_inquiries": 9}
    rows: list[dict[str, Any]] = []
    for series in cells.values():
        index = next(i for i, cell in enumerate(series) if cell.period == latest)
        cell = series[index]
        current = _observations(cell, PRODUCT_METRICS)
        for mode in MODES[WEEK]:
            base = _baseline(series, index, mode, PRODUCT_METRICS)
            changes, anomalies, volume_ok = _assess(cell, current, base, PRODUCT_METRICS)
            for key, label, note, scale in items:
                rows.append({
                    "change_key": f"{site}|{cell.scope.identifier}|{mode.name}|{key}",
                    "site": site,
                    "period": latest.key,
                    "period_start": latest.start.isoformat(),
                    "period_end": latest.end.isoformat(),
                    "scope_level": cell.scope.level,
                    "scope_id": cell.scope.identifier,
                    "scope_name": cell.scope.name,
                    "category_l1": cell.scope.category_l1,
                    "category_l2": cell.scope.category_l2,
                    "comparison_mode": mode.name,
                    "item_order": order[key],
                    "item": label if scale == 1 else f"{label}（%）",
                    "current": _scaled(current[key], scale),
                    "baseline": _scaled(base.values.get(key), scale),
                    "change": _round(changes[key]),
                    "is_anomaly": label in anomalies,
                    "alert_enabled": key in PRODUCT_METRICS.alerting(),
                    "volume_ok": volume_ok[key],
                    "baseline_excluded": bool(cell.excluded_reason),
                    "note": note,
                    **versions,
                })
    return rows


def _candidates(cells, volume_key: str, engaged_key: str | None, *, click_guard: bool = True) -> list[dict[str, Any]]:
    """Complete weeks that look like bot traffic, for human review only.

    Volume at least 3x the median of the previous 8 complete weeks, and either
    the engaged rate fell below half of its median (products), or search clicks
    stayed below 1.5x their median (site sessions, where engagement is not
    stored). Other-page sessions need no click guard: search rarely lands there.
    """
    found: list[dict[str, Any]] = []
    for series in cells.values():
        for index, cell in enumerate(series):
            if cell.status != COMPLETE:
                continue
            history = [item for item in series[max(0, index - 12):index] if item.status == COMPLETE][-8:]
            if len(history) < 4:
                continue
            median_volume = statistics.median(item.sums[volume_key] for item in history)
            volume = cell.sums[volume_key]
            if median_volume <= 0 or volume < 3 * median_volume or volume - median_volume < 200:
                continue
            if engaged_key:
                rates = [item.sums[engaged_key] / item.sums[volume_key] for item in history if item.sums[volume_key] > 0]
                rate_now = cell.sums[engaged_key] / volume
                if not rates or rate_now >= 0.5 * statistics.median(rates):
                    continue
                evidence = f"有效会话率 {rate_now:.0%}，前期中位 {statistics.median(rates):.0%}"
            elif click_guard:
                median_clicks = statistics.median(item.sums["clicks"] for item in history)
                if cell.sums["clicks"] >= 1.5 * median_clicks:
                    continue
                evidence = f"搜索点击 {cell.sums['clicks']:.0f}，前期中位 {median_clicks:.0f}"
            else:
                evidence = f"{volume_key} 前期中位 {median_volume:.0f}"
            found.append({
                "period": cell.period.key,
                "period_start": cell.period.start.isoformat(),
                "period_end": cell.period.end.isoformat(),
                "scope_id": cell.scope.identifier,
                "scope_name": cell.scope.name,
                "metric": volume_key,
                "volume": volume,
                "median_volume": median_volume,
                "multiple": round(volume / median_volume, 1),
                "evidence": evidence,
                "already_excluded": bool(cell.excluded_reason),
            })
    return found


def _scaled(value: float | None, scale: int) -> float | int | None:
    return None if value is None else _round(value * scale)


def _round(value: float | None) -> float | int | None:
    if value is None:
        return None
    rounded = round(value, 6)
    return int(rounded) if float(rounded).is_integer() else rounded
