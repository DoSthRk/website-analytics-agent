import json
from datetime import date

import pytest

from website_analytics.sync_plan import build_sync_plan, load_sync_profile, select_sync_periods


def _profile(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps(
            {
                "site": "genemedi-net",
                "selection_timezone": "America/Los_Angeles",
                "calendar_periods": {
                    "day": 14,
                    "week": 12,
                    "month": 12,
                    "quarter": 8,
                    "year": 3,
                },
                "rolling_windows_days": [7, 28, 90],
                "source_finality_lag_days": {"ga4": 2, "gsc": 3, "inquiry": 1},
            }
        ),
        encoding="utf-8",
    )
    return load_sync_profile(path)


def test_sync_plan_materializes_extensible_periods_and_source_freshness(tmp_path) -> None:
    result = build_sync_plan(_profile(tmp_path), date(2026, 8, 19))

    assert result["site"] == "genemedi-net"
    assert result["selectionTimezone"] == "America/Los_Angeles"
    assert len(result["periods"]) == 52
    keys = {period["periodKey"] for period in result["periods"]}
    assert len(keys) == 52
    assert "genemedi-net|rolling-28d|2026-07-23|2026-08-19" in keys
    current_week = next(
        period
        for period in result["periods"]
        if period["periodKey"] == "genemedi-net|week|2026-08-17|2026-08-23"
    )
    assert current_week["status"] == "preliminary"
    assert current_week["isFinal"] is False
    assert current_week["dashboardWindows"] == ["当前周期", "近4周", "近12周"]
    assert current_week["sourceAvailableThrough"] == {
        "ga4": "2026-08-17",
        "gsc": "2026-08-16",
        "inquiry": "2026-08-18",
    }
    complete_week = next(
        period
        for period in result["periods"]
        if period["periodKey"] == "genemedi-net|week|2026-08-10|2026-08-16"
    )
    assert complete_week["status"] == "complete"
    assert complete_week["isFinal"] is True


def test_sync_profile_rejects_missing_source_lag(tmp_path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(
        json.dumps(
            {
                "site": "demo",
                "selection_timezone": "UTC",
                "calendar_periods": {
                    "day": 1,
                    "week": 1,
                    "month": 1,
                    "quarter": 1,
                    "year": 1,
                },
                "rolling_windows_days": [7],
                "source_finality_lag_days": {"ga4": 2, "gsc": 3},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ga4, gsc, and inquiry"):
        load_sync_profile(path)


def test_intraday_scope_keeps_current_recent_and_rolling_periods(tmp_path) -> None:
    profile = _profile(tmp_path)
    rows = select_sync_periods(profile, date(2026, 8, 19), "intraday")
    keys = {row["periodKey"] for row in rows}

    assert "genemedi-net|day|2026-08-19|2026-08-19" in keys
    assert "genemedi-net|day|2026-08-15|2026-08-15" in keys
    assert "genemedi-net|day|2026-08-14|2026-08-14" not in keys
    assert "genemedi-net|week|2026-08-17|2026-08-23" in keys
    assert "genemedi-net|month|2026-08-01|2026-08-31" in keys
    assert "genemedi-net|rolling-28d|2026-07-23|2026-08-19" in keys
    assert "genemedi-net|rolling-90d|2026-05-22|2026-08-19" not in keys
    assert "genemedi-net|quarter|2026-07-01|2026-09-30" not in keys
    assert "genemedi-net|year|2026-01-01|2026-12-31" not in keys
    assert len(rows) < len(build_sync_plan(profile, date(2026, 8, 19))["periods"])
