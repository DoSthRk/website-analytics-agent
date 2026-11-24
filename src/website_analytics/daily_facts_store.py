"""A small on-disk store of finalized daily facts, one JSON file per day.

The period tables need every finalized day since history starts, but the daily
sync only fetches the last few days. The sync saves each day it writes to
Feishu here; history is seeded from ``recompute_history.py`` outputs. Only the
two record sets the period build reads are kept.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

STORED_TABLES = ("overview_daily", "product_funnel_daily")


def save_days(store: Path, records: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[str]:
    """Write one file per data date found in ``records``; replaces that day entirely."""
    by_day: dict[str, dict[str, list[Mapping[str, Any]]]] = {}
    for table in STORED_TABLES:
        for row in records[table]:
            day = _day(row)
            by_day.setdefault(day, {name: [] for name in STORED_TABLES})[table].append(row)
    for day, tables in by_day.items():
        if not tables["overview_daily"] or not tables["product_funnel_daily"]:
            raise ValueError(f"daily facts for {day} are missing a record set")
        if len(tables["overview_daily"]) != 1:
            raise ValueError(f"daily facts for {day} must have exactly one overview row")
    store.mkdir(parents=True, exist_ok=True)
    for day, tables in sorted(by_day.items()):
        _atomic_write(store / f"{day}.json", {"data_date": day, **tables})
    return sorted(by_day)


def load_days(store: Path) -> dict[str, list[dict[str, Any]]]:
    """All stored days, merged into one list per record set, in date order."""
    merged: dict[str, list[dict[str, Any]]] = {table: [] for table in STORED_TABLES}
    for path in sorted(store.glob("????-??-??.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("data_date") != path.stem:
            raise ValueError(f"daily facts file is mislabeled: {path.name}")
        for table in STORED_TABLES:
            rows = document[table]
            if any(_day(row) != path.stem for row in rows):
                raise ValueError(f"daily facts file mixes dates: {path.name}")
            merged[table].extend(rows)
    return merged


def merge_recompute_outputs(paths: Iterable[Path]) -> dict[str, list[dict[str, Any]]]:
    """Merge recompute outputs; for each record set a later file replaces a day entirely."""
    by_table: dict[str, dict[str, list[dict[str, Any]]]] = {table: {} for table in STORED_TABLES}
    for path in paths:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        if document.get("mismatches"):
            raise ValueError(f"{path} has reconciliation mismatches")
        for table in STORED_TABLES:
            days: dict[str, list[dict[str, Any]]] = {}
            for row in document["records"][table]:
                days.setdefault(_day(row), []).append(row)
            by_table[table].update(days)
    return {table: [row for day in sorted(days) for row in days[day]] for table, days in by_table.items()}


def last_day(records: Mapping[str, Sequence[Mapping[str, Any]]]) -> date:
    return max(date.fromisoformat(_day(row)) for row in records["overview_daily"])


def _day(row: Mapping[str, Any]) -> str:
    day = str(row["data_date"])[:10]
    date.fromisoformat(day)
    return day


def _atomic_write(path: Path, value: Any) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
