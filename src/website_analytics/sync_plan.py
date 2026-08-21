"""Build deterministic, source-aware synchronization plans without calling APIs."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from website_analytics.periods import (
    AnalyticsPeriod,
    CalendarPeriodKind,
    SourceCoverage,
    dashboard_windows,
    period_key,
    recent_periods,
    rolling_period,
    snapshot_status,
)


_CALENDAR_KINDS: tuple[CalendarPeriodKind, ...] = (
    "day",
    "week",
    "month",
    "quarter",
    "year",
)
_SOURCES = ("ga4", "gsc", "inquiry")


@dataclass(frozen=True)
class SyncProfile:
    site: str
    selection_timezone: str
    calendar_periods: Mapping[CalendarPeriodKind, int]
    rolling_windows_days: tuple[int, ...]
    source_finality_lag_days: Mapping[str, int]


def load_sync_profile(path: Path) -> SyncProfile:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("sync profile is unreadable or invalid JSON") from error
    if not isinstance(document, Mapping):
        raise ValueError("sync profile must contain an object")
    site = _required_text(document, "site")
    selection_timezone = _required_text(document, "selection_timezone")
    calendar_raw = document.get("calendar_periods")
    if not isinstance(calendar_raw, Mapping) or set(calendar_raw) != set(_CALENDAR_KINDS):
        raise ValueError("calendar_periods must define day, week, month, quarter, and year")
    calendar_periods = {
        kind: _positive_integer(calendar_raw.get(kind), f"calendar_periods.{kind}")
        for kind in _CALENDAR_KINDS
    }
    rolling_raw = document.get("rolling_windows_days")
    if not isinstance(rolling_raw, Sequence) or isinstance(rolling_raw, (str, bytes)):
        raise ValueError("rolling_windows_days must be an array")
    rolling_windows = tuple(
        _positive_integer(value, "rolling_windows_days") for value in rolling_raw
    )
    if not rolling_windows or len(set(rolling_windows)) != len(rolling_windows):
        raise ValueError("rolling_windows_days must contain unique positive values")
    lag_raw = document.get("source_finality_lag_days")
    if not isinstance(lag_raw, Mapping) or set(lag_raw) != set(_SOURCES):
        raise ValueError("source_finality_lag_days must define ga4, gsc, and inquiry")
    lags = {source: _non_negative_integer(lag_raw[source], source) for source in _SOURCES}
    return SyncProfile(
        site=site,
        selection_timezone=selection_timezone,
        calendar_periods=calendar_periods,
        rolling_windows_days=rolling_windows,
        source_finality_lag_days=lags,
    )


def build_sync_plan(profile: SyncProfile, anchor: date) -> dict[str, Any]:
    """Return materialized periods and conservative source coverage dates."""
    periods: list[AnalyticsPeriod] = []
    for kind in _CALENDAR_KINDS:
        periods.extend(recent_periods(kind, profile.calendar_periods[kind], anchor))
    periods.extend(rolling_period(days, anchor) for days in profile.rolling_windows_days)
    periods.sort(key=lambda period: (period.start, period.end, period.storage_kind))

    rows = [build_period_plan(profile, anchor, period) for period in periods]
    return {
        "site": profile.site,
        "selectionTimezone": profile.selection_timezone,
        "anchor": anchor.isoformat(),
        "periods": rows,
    }


def build_period_plan(
    profile: SyncProfile,
    anchor: date,
    period: AnalyticsPeriod,
) -> dict[str, Any]:
    """Build one source-freshness-aware row for a materialized period."""
    available_through = {
        source: anchor - timedelta(days=profile.source_finality_lag_days[source])
        for source in _SOURCES
    }
    coverages = tuple(
        SourceCoverage(source=source, status="ok", available_through=available_through[source])
        for source in _SOURCES
    )
    status = snapshot_status(period, coverages)
    return {
        "periodKey": period_key(profile.site, period),
        "kind": period.kind,
        "kindLabel": period.kind_label,
        "storageKind": period.storage_kind,
        "start": period.start.isoformat(),
        "end": period.end.isoformat(),
        "windowDays": period.window_days,
        "label": period.label,
        "dashboardWindows": list(dashboard_windows(period, anchor)),
        "status": status,
        "isFinal": status == "complete",
        "sourceAvailableThrough": {
            source: value.isoformat() for source, value in available_through.items()
        },
    }


def select_sync_periods(
    profile: SyncProfile,
    anchor: date,
    scope: str,
) -> list[dict[str, Any]]:
    """Select a bounded intraday refresh set or the entire retention plan."""
    plan = build_sync_plan(profile, anchor)
    rows = plan["periods"]
    if not isinstance(rows, list):  # pragma: no cover - internal contract
        raise ValueError("sync plan periods are invalid")
    if scope == "full":
        return rows
    if scope != "intraday":
        raise ValueError("sync scope must be intraday or full")
    recent_cutoff = anchor - timedelta(days=max(profile.source_finality_lag_days.values()) + 1)
    return [
        row
        for row in rows
        if (
            row.get("kind") == "rolling"
            and isinstance(row.get("windowDays"), int)
            and int(row["windowDays"]) <= 28
        )
        or (
            row.get("kind") in {"day", "week", "month"}
            and (
                "当前周期" in row.get("dashboardWindows", [])
                or date.fromisoformat(str(row["end"])) >= recent_cutoff
            )
        )
    ]


def _required_text(document: Mapping[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _positive_integer(value: object, field: str) -> int:
    number = _non_negative_integer(value, field)
    if number == 0:
        raise ValueError(f"{field} must be positive")
    return number


def _non_negative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value
