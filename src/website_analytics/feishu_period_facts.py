"""Feishu contract, row translation and idempotent sync for the period tables.

The physical contract is generated from the metric definitions in
``period_facts`` so field names cannot drift from the computed records.
Rows are matched by a stable key; only changed rows are written, nothing is
deleted, and every desired row is read back after the write.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from website_analytics.feishu_product_funnel import load_json_object
from website_analytics.feishu_records import FeishuTarget, RecordClient
from website_analytics.feishu_v3 import feishu_record_to_key
from website_analytics.period_facts import PRODUCT_METRICS, SITE_METRICS, MetricSet

CONTRACT_VERSION = "1"
TABLE_KEYS = ("product_period", "site_period", "product_change")
DISPLAY_NAMES = {
    "product_period": "产品漏斗周期数据",
    "site_period": "全站周期数据",
    "product_change": "产品变化定位",
}
_SELECT_ORDER = {
    "grain": ("周", "月"),
    "period_status": ("完整", "进行中", "有缺日"),
    "scope_level": ("全站", "全部产品", "一级分类", "二级分类", "末级产品"),
    "comparison_mode": ("前8周均值", "前12周均值", "去年同期", "前3月均值", "前6月均值", "去年同月"),
}
_STATUS_HUES = {"完整": "Green", "进行中": "Blue", "有缺日": "Orange"}
_HUES = ("Blue", "Green", "Purple", "Orange", "Carmine", "Red", "Turquoise", "Wathet", "Yellow", "Gray")
_SUPPORTED_TYPES = {"text", "number", "select", "datetime", "checkbox"}

# Style tags, translated into Feishu number styles when tables are created.
COUNT, AVERAGE, PERCENT, POSITION, PLAIN = "count", "average", "percent", "position", "plain"


def _field(key: str, name: str, field_type: str, *, required: bool = False, style: str | None = None) -> dict[str, Any]:
    field: dict[str, Any] = {"key": key, "name": name, "type": field_type, "required": required}
    if style:
        field["style"] = style
    return field


def _period_fields(stable_key: str, stable_name: str, metrics: MetricSet, *, position: bool) -> list[dict[str, Any]]:
    fields = [
        _field(stable_key, stable_name, "text", required=True),
        _field("site", "站点", "text", required=True),
        _field("grain", "粒度", "select", required=True),
        _field("period", "周期", "text", required=True),
        _field("period_start", "周期开始", "datetime", required=True),
        _field("period_end", "周期结束", "datetime", required=True),
        _field("iso_year", "年份", "number", required=True, style=PLAIN),
        _field("sequence", "期序号", "number", required=True, style=PLAIN),
        _field("scope_level", "范围层级", "select", required=True),
        _field("scope_id", "范围ID", "text", required=True),
        _field("scope_name", "范围名称", "select", required=True),
        _field("category_l1", "一级分类", "select"),
        _field("category_l2", "二级分类", "select"),
        _field("period_status", "周期状态", "select", required=True),
        _field("days_present", "本期天数", "number", required=True, style=PLAIN),
        _field("baseline_excluded", "基准排除", "checkbox", required=True),
        _field("exclusion_reason", "排除原因", "text"),
        _field("comparison_mode", "对照方式", "select", required=True),
        _field("baseline_periods", "基准周期数", "number", required=True, style=PLAIN),
        _field("baseline_note", "基准周期说明", "text"),
        _field("is_anomaly", "是否异常", "checkbox", required=True),
        _field("anomaly_items", "异常项", "text"),
    ]
    items = [(quantity.key, quantity.label, COUNT, AVERAGE) for quantity in metrics.quantities]
    items += [(ratio.key, ratio.label, PERCENT, PERCENT) for ratio in metrics.ratios]
    fields += [_field(key, label, "number", style=style) for key, label, style, _ in items]
    if position:
        fields.append(_field("avg_position", "平均排名", "number", style=POSITION))
    fields += [_field(f"baseline_{key}", f"基准·{label}", "number", style=style) for key, label, _, style in items]
    if position:
        fields.append(_field("baseline_avg_position", "基准·平均排名", "number", style=POSITION))
    fields += [_field(f"change_{key}", f"变化·{label}", "number", style=PERCENT) for key, label, _, _ in items]
    fields += [
        _field("product_mapping_version", "产品映射版本", "text", required=True),
        _field("page_classification_version", "页面分类版本", "text", required=True),
        _field("refreshed_at", "数据更新时间", "datetime", required=True),
    ]
    return fields


def _change_fields() -> list[dict[str, Any]]:
    return [
        _field("change_key", "变化定位键", "text", required=True),
        _field("site", "站点", "text", required=True),
        _field("period", "周期", "text", required=True),
        _field("period_start", "周期开始", "datetime", required=True),
        _field("period_end", "周期结束", "datetime", required=True),
        _field("scope_level", "范围层级", "select", required=True),
        _field("scope_id", "范围ID", "text", required=True),
        _field("scope_name", "范围名称", "select", required=True),
        _field("category_l1", "一级分类", "select"),
        _field("category_l2", "二级分类", "select"),
        _field("comparison_mode", "对照方式", "select", required=True),
        _field("item_order", "观察项顺序", "number", required=True, style=PLAIN),
        _field("item", "观察项", "select", required=True),
        _field("current", "本期", "number", style=AVERAGE),
        _field("baseline", "基准", "number", style=AVERAGE),
        _field("change", "变化", "number", style=PERCENT),
        _field("is_anomaly", "是否异常", "checkbox", required=True),
        _field("alert_enabled", "参与报警", "checkbox", required=True),
        _field("volume_ok", "基准量达标", "checkbox", required=True),
        _field("baseline_excluded", "基准排除", "checkbox", required=True),
        _field("note", "说明", "text"),
        _field("product_mapping_version", "产品映射版本", "text", required=True),
        _field("page_classification_version", "页面分类版本", "text", required=True),
        _field("refreshed_at", "数据更新时间", "datetime", required=True),
    ]


def period_contract() -> dict[str, Any]:
    """The physical Feishu contract for the three period tables."""
    return {
        "version": CONTRACT_VERSION,
        "tables": [
            {
                "logical_name": "product_period",
                "display_name": DISPLAY_NAMES["product_period"],
                "stable_key": "周期键",
                "fields": _period_fields("period_key", "周期键", PRODUCT_METRICS, position=True),
            },
            {
                "logical_name": "site_period",
                "display_name": DISPLAY_NAMES["site_period"],
                "stable_key": "全站周期键",
                "fields": _period_fields("period_key", "全站周期键", SITE_METRICS, position=False),
            },
            {
                "logical_name": "product_change",
                "display_name": DISPLAY_NAMES["product_change"],
                "stable_key": "变化定位键",
                "fields": _change_fields(),
            },
        ],
    }


def contract_tables(contract: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    if str(contract.get("version")) != CONTRACT_VERSION:
        raise ValueError("period contract version differs")
    tables: dict[str, Mapping[str, Any]] = {}
    for table in contract["tables"]:
        fields = table["fields"]
        names = [str(field["name"]) for field in fields]
        keys = [str(field["key"]) for field in fields]
        if len(set(names)) != len(names) or len(set(keys)) != len(keys):
            raise ValueError(f"period table {table['logical_name']} has duplicate fields")
        if names[0] != table["stable_key"]:
            raise ValueError(f"period table {table['logical_name']} stable key must be first")
        if any(field["type"] not in _SUPPORTED_TYPES for field in fields):
            raise ValueError("period contract has an unsupported field type")
        tables[str(table["logical_name"])] = table
    if set(tables) != set(TABLE_KEYS):
        raise ValueError("period contract tables differ")
    return tables


# ---------------------------------------------------------------- rows


def validate_records(records: Mapping[str, Sequence[Mapping[str, Any]]], tables: Mapping[str, Mapping[str, Any]]) -> None:
    """Every row must carry exactly the contract keys (minus refreshed_at) with valid types."""
    for logical_name in TABLE_KEYS:
        fields = [field for field in tables[logical_name]["fields"] if field["key"] != "refreshed_at"]
        expected = {str(field["key"]) for field in fields}
        stable = str(fields[0]["key"])
        seen: set[str] = set()
        for row in records[logical_name]:
            if set(row) != expected:
                missing, extra = expected - set(row), set(row) - expected
                raise ValueError(f"period row fields differ in {logical_name}: missing {sorted(missing)}, extra {sorted(extra)}")
            key = row[stable]
            if not isinstance(key, str) or not key or key in seen:
                raise ValueError(f"period row key is missing or duplicated in {logical_name}")
            seen.add(key)
            for field in fields:
                _check_type(field, row[str(field["key"])])


def _check_type(field: Mapping[str, Any], value: Any) -> None:
    key, field_type = str(field["key"]), str(field["type"])
    if value is None or value == "":
        if field.get("required") and value is None:
            raise ValueError(f"required period field is empty: {key}")
        return
    if field_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"period field is not a finite number: {key}")
    elif field_type == "checkbox":
        if not isinstance(value, bool):
            raise ValueError(f"period field is not a checkbox value: {key}")
    elif not isinstance(value, str):
        raise ValueError(f"period field is not text: {key}")


def feishu_rows(table: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], refreshed_at: datetime) -> list[dict[str, Any]]:
    """Translate records into {field name: cell} dicts for record writes."""
    stamp = refreshed_at.strftime("%Y-%m-%d %H:%M:%S")
    output = []
    for row in rows:
        cells: dict[str, Any] = {}
        for field in table["fields"]:
            key = str(field["key"])
            value = stamp if key == "refreshed_at" else row[key]
            cells[str(field["name"])] = _cell(field, value)
        output.append(cells)
    return output


def _cell(field: Mapping[str, Any], value: Any) -> Any:
    field_type = str(field["type"])
    if value is None:
        return False if field_type == "checkbox" else None
    if field_type == "number":
        number = float(value)
        return int(number) if number.is_integer() else number
    if field_type == "datetime":
        text = str(value)
        return f"{text} 00:00:00" if len(text) == 10 else text
    if field_type in {"text", "select"} and value == "":
        return None
    return value


def table_field_definitions(table: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """lark-cli field definitions for +table-create."""
    definitions: list[dict[str, Any]] = []
    for field in table["fields"]:
        key, name, field_type = str(field["key"]), str(field["name"]), str(field["type"])
        definition: dict[str, Any] = {"name": name, "type": field_type}
        if field_type == "number":
            definition["style"] = _number_style(str(field.get("style", PLAIN)))
        elif field_type == "datetime":
            definition["style"] = {"format": "yyyy-MM-dd HH:mm" if key == "refreshed_at" else "yyyy-MM-dd"}
        elif field_type == "select":
            present = {str(row[key]) for row in rows if row.get(key) not in (None, "")}
            ordered = list(_SELECT_ORDER.get(key, ()))
            ordered += sorted(present - set(ordered))
            definition["multiple"] = False
            definition["options"] = [
                {"name": value, "hue": _STATUS_HUES.get(value, _HUES[index % len(_HUES)]), "lightness": "Light"}
                for index, value in enumerate(ordered)
            ]
        definitions.append(definition)
    return definitions


def _number_style(style: str) -> dict[str, Any]:
    if style == PERCENT:
        return {"type": "plain", "precision": 1, "percentage": True, "thousands_separator": False}
    precision = {COUNT: 0, AVERAGE: 1, POSITION: 1, PLAIN: 0}[style]
    return {"type": "plain", "precision": precision, "percentage": False, "thousands_separator": style in {COUNT, AVERAGE}}


# ---------------------------------------------------------------- target


@dataclass(frozen=True)
class PeriodTarget:
    base_token: str
    identity: str
    tables: Mapping[str, str]

    def record_client_target(self) -> FeishuTarget:
        return FeishuTarget(
            base_token=self.base_token,
            overview_table=self.tables["product_period"],
            product_table=self.tables["product_change"],
            identity=self.identity,
        )


def load_period_target(path: Path) -> PeriodTarget:
    document = load_json_object(path)
    if set(document) != {"version", "base_token", "identity", "tables"} or str(document["version"]) != CONTRACT_VERSION:
        raise ValueError("period sync target has unexpected fields or version")
    if document["identity"] not in {"user", "bot"}:
        raise ValueError("period sync identity must be user or bot")
    base_token = document["base_token"]
    if not isinstance(base_token, str) or not base_token.strip():
        raise ValueError("period sync target is missing base_token")
    tables = document["tables"]
    if not isinstance(tables, Mapping) or set(tables) != set(TABLE_KEYS):
        raise ValueError("period sync target tables differ from the contract")
    if any(not str(value).startswith("tbl") for value in tables.values()):
        raise ValueError("period sync target contains an invalid table ID")
    return PeriodTarget(base_token.strip(), str(document["identity"]), {str(k): str(v) for k, v in tables.items()})


# ---------------------------------------------------------------- sync


@dataclass(frozen=True)
class TablePlan:
    logical_name: str
    table_id: str
    stable_name: str
    fields: tuple[Mapping[str, Any], ...]
    desired: Mapping[str, Mapping[str, Any]]
    creates: tuple[Mapping[str, Any], ...]
    updates: tuple[tuple[str, Mapping[str, Any]], ...]
    existing: int
    unchanged: int

    def counts(self) -> dict[str, int]:
        return {
            "desired": len(self.desired),
            "existing": self.existing,
            "create": len(self.creates),
            "update": len(self.updates),
            "unchanged": self.unchanged,
        }


def plan_period_sync(
    client: RecordClient,
    target: PeriodTarget,
    contract: Mapping[str, Any],
    records: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    refreshed_at: datetime,
    tables: Sequence[str] = TABLE_KEYS,
) -> list[TablePlan]:
    """Read current Feishu rows and work out creates and updates; no writes."""
    by_name = contract_tables(contract)
    validate_records(records, by_name)
    plans = []
    for logical_name in tables:
        table = by_name[logical_name]
        fields = tuple(table["fields"])
        stable_name = str(fields[0]["name"])
        desired: dict[str, Mapping[str, Any]] = {}
        for row in feishu_rows(table, records[logical_name], refreshed_at):
            desired[feishu_record_to_key(row[stable_name], stable_name)] = row
        existing = _rows_by_key(client, target.tables[logical_name], stable_name, logical_name)
        creates, updates, unchanged = [], [], 0
        for key, row in desired.items():
            current = existing.get(key)
            if current is None:
                creates.append(row)
            elif _changed(current[1], row, fields):
                updates.append((current[0], row))
            else:
                unchanged += 1
        plans.append(
            TablePlan(logical_name, target.tables[logical_name], stable_name, fields, desired,
                      tuple(creates), tuple(updates), len(existing), unchanged)
        )
    return plans


def apply_period_plans(client: RecordClient, plans: Sequence[TablePlan]) -> dict[str, dict[str, int]]:
    """Write planned creates and updates, then read every desired row back."""
    result: dict[str, dict[str, int]] = {}
    for plan in plans:
        created = client.create_records(plan.table_id, plan.creates) if plan.creates else 0
        if created != len(plan.creates):
            raise AssertionError(f"period create count differs: {plan.logical_name}")
        if plan.updates:
            datetime_fields = frozenset(str(f["name"]) for f in plan.fields if f["type"] == "datetime")
            client.update_records(plan.table_id, plan.updates, datetime_fields=datetime_fields)
        result[plan.logical_name] = plan.counts()
    for plan in plans:
        actual = _rows_by_key(client, plan.table_id, plan.stable_name, plan.logical_name)
        for key, expected in plan.desired.items():
            row = actual.get(key)
            if row is None or _changed(row[1], expected, plan.fields):
                raise AssertionError(f"period readback differs: {plan.logical_name} {key}")
        result[plan.logical_name]["verified"] = len(plan.desired)
    return result


def _rows_by_key(client: RecordClient, table_id: str, stable_name: str, logical_name: str) -> dict[str, tuple[str, Mapping[str, Any]]]:
    rows: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for record_id, row in client.list_records(table_id):
        value = row.get(stable_name)
        if value is None:
            continue
        key = feishu_record_to_key(value, stable_name)
        if key in rows:
            raise ValueError(f"Feishu period table has a duplicate key: {logical_name} {key}")
        rows[key] = (record_id, row)
    return rows


def _changed(actual: Mapping[str, Any], expected: Mapping[str, Any], fields: Sequence[Mapping[str, Any]]) -> bool:
    for field in fields:
        if field["key"] == "refreshed_at":
            continue
        name, field_type = str(field["name"]), str(field["type"])
        if not _equal(_normal(actual.get(name), field_type), _normal(expected.get(name), field_type)):
            return True
    return False


def _equal(left: Any, right: Any) -> bool:
    if isinstance(left, float) and isinstance(right, float):
        return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)
    return left == right


def _normal(value: Any, field_type: str) -> Any:
    if field_type == "checkbox":
        return bool(value) if value is not None else False
    if field_type == "number":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return None if value in (None, "") else value
    text = _text(value)
    if text in (None, ""):
        return None
    if field_type == "datetime":
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return text
    return text


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, list) and len(value) == 1:
        return _text(value[0])
    if isinstance(value, Mapping):
        for key in ("text", "name", "value"):
            if key in value:
                return _text(value[key])
    return str(value)
