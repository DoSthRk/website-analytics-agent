"""Idempotent incremental synchronization for Feishu V3 daily facts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from website_analytics.feishu_records import FeishuTarget, RecordClient
from website_analytics.feishu_v3 import (
    TABLE_KEYS,
    build_feishu_rows,
    feishu_record_to_key,
    load_json_object,
    validate_backfill,
    validate_contract,
)


@dataclass(frozen=True)
class FeishuV3Target:
    """Registered Base and table IDs for the three V3 daily fact tables."""

    base_token: str
    identity: str
    tables: Mapping[str, str]

    def record_client_target(self) -> FeishuTarget:
        """Reuse the generic Lark record client without weakening V3 validation."""

        return FeishuTarget(
            base_token=self.base_token,
            overview_table=self.tables["overview_daily"],
            product_table=self.tables["product_daily"],
            identity=self.identity,
        )


@dataclass(frozen=True)
class _TablePlan:
    logical_name: str
    table_id: str
    creates: tuple[Mapping[str, Any], ...]
    updates: tuple[tuple[str, Mapping[str, Any]], ...]
    desired: int
    existing: int
    unchanged: int
    stable_name: str
    definitions: tuple[Mapping[str, Any], ...]
    desired_by_key: Mapping[str, Mapping[str, Any]]


def load_feishu_v3_target(path: Path) -> FeishuV3Target:
    """Load a strict V3 target; table IDs remain repository metadata, not input."""

    document = load_json_object(path)
    if set(document) != {"version", "base_token", "identity", "tables"}:
        raise ValueError("Feishu V3 sync target has unexpected fields")
    if str(document.get("version")) != "3":
        raise ValueError("Feishu V3 sync target must use version 3")
    identity = document.get("identity")
    if identity not in {"user", "bot"}:
        raise ValueError("Feishu V3 identity must be user or bot")
    base_token = document.get("base_token")
    if not isinstance(base_token, str) or not base_token.strip():
        raise ValueError("Feishu V3 sync target is missing base_token")
    raw_tables = document.get("tables")
    if not isinstance(raw_tables, Mapping) or set(raw_tables) != set(TABLE_KEYS):
        raise ValueError("Feishu V3 target tables do not match the contract")
    tables = {str(key): str(value) for key, value in raw_tables.items()}
    if any(not value.startswith("tbl") for value in tables.values()):
        raise ValueError("Feishu V3 sync target contains an invalid table ID")
    return FeishuV3Target(
        base_token=base_token.strip(),
        identity=str(identity),
        tables=tables,
    )


def sync_v3_record_sets(
    client: RecordClient,
    target: FeishuV3Target,
    contract: Mapping[str, Any],
    document: Mapping[str, Any],
) -> dict[str, dict[str, int]]:
    """Create missing daily keys and update only materially changed rows.

    Existing rows outside the incremental document are retained. All three
    tables are inspected and validated before the first write, so duplicate
    stable keys fail closed. ``refreshed_at`` alone does not cause a write;
    it records when a fact last changed instead of generating churn every run.
    """

    tables = validate_contract(contract)
    records = validate_backfill(document, tables)
    plans = tuple(
        _plan_table(
            client=client,
            logical_name=logical_name,
            table_id=target.tables[logical_name],
            table=tables[logical_name],
            desired=records[logical_name],
        )
        for logical_name in TABLE_KEYS
    )

    result: dict[str, dict[str, int]] = {}
    for plan in plans:
        created = client.create_records(plan.table_id, plan.creates) if plan.creates else 0
        for record_id, fields in plan.updates:
            client.update_record(plan.table_id, record_id, fields)
        result[plan.logical_name] = {
            "desired": plan.desired,
            "existing": plan.existing,
            "created": created,
            "updated": len(plan.updates),
            "unchanged": plan.unchanged,
        }
    for plan in plans:
        verified = _verify_table(client, plan)
        result[plan.logical_name]["verified"] = verified
    return result


def _plan_table(
    *,
    client: RecordClient,
    logical_name: str,
    table_id: str,
    table: Mapping[str, Any],
    desired: Sequence[Mapping[str, Any]],
) -> _TablePlan:
    definitions = list(table["fields"])
    payload = build_feishu_rows(table, desired)
    names = [str(value) for value in payload["fields"]]
    desired_rows = [
        dict(zip(names, row, strict=True)) for row in payload["rows"]
    ]
    stable_name = str(definitions[0]["name"])
    desired_by_key: dict[str, Mapping[str, Any]] = {}
    for row in desired_rows:
        key = feishu_record_to_key(row[stable_name], stable_name)
        if key in desired_by_key:
            raise ValueError(f"desired V3 records contain duplicate key: {logical_name}")
        desired_by_key[key] = row

    existing_by_key: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for record_id, row in client.list_records(table_id):
        value = row.get(stable_name)
        if value is None:
            continue
        key = feishu_record_to_key(value, stable_name)
        if key in existing_by_key:
            raise ValueError(f"Feishu V3 table contains duplicate key: {logical_name}")
        existing_by_key[key] = (record_id, row)

    creates: list[Mapping[str, Any]] = []
    updates: list[tuple[str, Mapping[str, Any]]] = []
    unchanged = 0
    for key, desired_row in desired_by_key.items():
        existing = existing_by_key.get(key)
        if existing is None:
            creates.append(desired_row)
        elif _materially_changed(existing[1], desired_row, definitions):
            updates.append((existing[0], desired_row))
        else:
            unchanged += 1
    return _TablePlan(
        logical_name=logical_name,
        table_id=table_id,
        creates=tuple(creates),
        updates=tuple(updates),
        desired=len(desired_by_key),
        existing=len(existing_by_key),
        unchanged=unchanged,
        stable_name=stable_name,
        definitions=tuple(definitions),
        desired_by_key=desired_by_key,
    )


def _verify_table(client: RecordClient, plan: _TablePlan) -> int:
    actual_by_key: dict[str, Mapping[str, Any]] = {}
    for _, row in client.list_records(plan.table_id):
        value = row.get(plan.stable_name)
        if value is None:
            continue
        key = feishu_record_to_key(value, plan.stable_name)
        if key in actual_by_key:
            raise AssertionError(
                f"Feishu V3 readback contains duplicate key: {plan.logical_name}"
            )
        actual_by_key[key] = row
    for key, expected in plan.desired_by_key.items():
        actual = actual_by_key.get(key)
        if actual is None or _materially_changed(
            actual, expected, plan.definitions
        ):
            raise AssertionError(
                f"Feishu V3 readback differs from incremental facts: {plan.logical_name}"
            )
    return len(plan.desired_by_key)


def _materially_changed(
    existing: Mapping[str, Any],
    desired: Mapping[str, Any],
    definitions: Sequence[Mapping[str, Any]],
) -> bool:
    for field in definitions:
        if field["key"] == "refreshed_at":
            continue
        name = str(field["name"])
        field_type = str(field["type"])
        if _normal(existing.get(name), field_type) != _normal(
            desired.get(name), field_type
        ):
            return True
    return False


def _normal(value: Any, field_type: str) -> Any:
    if field_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return value
        return float(value)
    if field_type == "datetime":
        text = _cell_text(value)
        if text is None:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except ValueError:
            return text
    if field_type == "select":
        return _cell_text(value)
    return _cell_text(value)


def _cell_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list) and len(value) == 1:
        return _cell_text(value[0])
    if isinstance(value, Mapping):
        for key in ("text", "name", "value"):
            if key in value:
                return _cell_text(value[key])
    return str(value)
