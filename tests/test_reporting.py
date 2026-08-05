from __future__ import annotations

import pytest

from website_analytics.reporting import compare_totals


def test_compare_totals_calculates_delta_for_a_shared_source_metric() -> None:
    result = compare_totals(
        {"ga4": {"sessions": 42}},
        {"ga4": {"sessions": 40}},
    )

    assert result == {
        "complete": True,
        "metrics": {
            "ga4.sessions": {"current": 42, "previous": 40, "delta": 2},
        },
    }


def test_compare_totals_marks_missing_sources_incomplete_without_zeroes() -> None:
    result = compare_totals(
        {"ga4": {"sessions": 42}},
        {"gsc": {"clicks": 40}},
    )

    assert result == {
        "complete": False,
        "metrics": {
            "ga4.sessions": {"current": 42, "previous": None, "delta": None},
            "gsc.clicks": {"current": None, "previous": 40, "delta": None},
        },
    }


def test_compare_totals_namespaces_metrics_that_share_a_name_across_sources() -> None:
    result = compare_totals(
        {"ga4": {"clicks": 7}, "gsc": {"clicks": 9}},
        {"ga4": {"clicks": 5}, "gsc": {"clicks": 8}},
    )

    assert result["metrics"] == {
        "ga4.clicks": {"current": 7, "previous": 5, "delta": 2},
        "gsc.clicks": {"current": 9, "previous": 8, "delta": 1},
    }


@pytest.mark.parametrize("bad_value", [True, "42"])
def test_compare_totals_rejects_boolean_and_non_numeric_metric_values(
    bad_value: object,
) -> None:
    with pytest.raises(ValueError, match="must be a number"):
        compare_totals(
            {"ga4": {"sessions": bad_value}},
            {"ga4": {"sessions": 40}},
        )
