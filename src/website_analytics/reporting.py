from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def compare_totals(
    current: Mapping[str, Mapping[str, int | float]],
    previous: Mapping[str, Mapping[str, int | float]],
) -> dict[str, Any]:
    """Compare source totals using source-qualified metric names."""
    _validate_metric_values(current, "current")
    _validate_metric_values(previous, "previous")

    metrics: dict[str, dict[str, Any]] = {}
    for source in sorted(set(current) | set(previous)):
        current_metrics = current.get(source, {})
        previous_metrics = previous.get(source, {})
        for metric in sorted(set(current_metrics) | set(previous_metrics)):
            current_value = current_metrics.get(metric)
            previous_value = previous_metrics.get(metric)
            metrics[f"{source}.{metric}"] = {
                "current": current_value,
                "previous": previous_value,
                "delta": (
                    current_value - previous_value
                    if current_value is not None and previous_value is not None
                    else None
                ),
            }
    return {"complete": set(current) == set(previous), "metrics": metrics}


def _validate_metric_values(
    totals: Mapping[str, Mapping[str, int | float]], label: str
) -> None:
    if not isinstance(totals, Mapping):
        raise ValueError(f"{label} totals must be a source-to-metric mapping")

    for source, source_metrics in totals.items():
        if not isinstance(source_metrics, Mapping):
            raise ValueError(f"{label} source {source!r} must map metrics to values")
        for metric, value in source_metrics.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or (isinstance(value, float) and not math.isfinite(value))
            ):
                raise ValueError(
                    f"{label} metric {source!r}.{metric!r} must be a number"
                )
