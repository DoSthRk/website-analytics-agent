"""Read back Feishu dashboard v2 and verify the operator-facing metrics."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from website_analytics.feishu_records import is_retryable_lark_failure, lark_cli_environment


BLOCKS = {
    "overview_visit_stat": "chtcnAhViGlSX7Ff0bJQZAB0CUe",
    "overview_search_stat": "chtcn4CvOHAQC4vjsJW28AsBsje",
    "overview_event_stat": "chtcnu5LyJk4eCZhGBGb1wn56lz",
    "overview_inquiry_stat": "chtcnxyBioVqRHVyh7CQf5tWi6e",
    "overview_visit_line": "chtcnC5uQQaXTsGriXPP0Pxgs5c",
    "overview_search_line": "chtcnjbCZhM8NS7wStUVC7k4Zpg",
    "overview_inquiry_line": "chtcnxfI6EapRqejzUIGI2TgH2b",
    "product_visit_line": "chtcnXfPNOxj30Trs6cmsrb9AMh",
    "product_search_line": "chtcnAP8jfq9RCkfXvNbxtXxbrc",
    "product_inquiry_line": "chtcn7pG8S4Ei4jGym3s13Xe8nd",
    "product_visit_bar": "chtcnzp0riPnhNZEOZJorvXhSWf",
    "product_search_bar": "chtcnoSmnmaaubH3t2Dl1uiEfOf",
    "product_inquiry_bar": "chtcnRhaptCNaUAbRN0vcEkx6qg",
}

DASHBOARDS = {
    "官网运营驾驶舱": ("blkLiB6bavgglOdp", 9),
    "产品趋势与行动": ("blkMWyCmzTFZBw4Z", 7),
}

TEXT_BLOCKS = {
    "官网运营驾驶舱": {
        "chtcnJEo0Ou2BzCPFPNcBrvy4Se": "看板阅读说明",
        "chtcnT3N0FTWv8q6nKgHjmQvWbY": "运营判断顺序",
    },
    "产品趋势与行动": {
        "chtcnE7XrH4K9JPLQusSSh8vNuf": "产品行动指南",
    },
}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-token", required=True)
    parser.add_argument("--overview-payload", type=Path, required=True)
    parser.add_argument("--product-payload", type=Path, required=True)
    return parser.parse_args()


def _runtime() -> list[str]:
    wrapper = shutil.which("lark-cli")
    if wrapper is None:
        raise RuntimeError("lark-cli is unavailable")
    if os.name != "nt":
        return [wrapper]
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("node is unavailable")
    script = Path(wrapper).parent / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js"
    if not script.is_file():
        raise RuntimeError("lark-cli runtime script is unavailable")
    return [node, str(script)]


def _run(command: list[str]) -> dict[str, Any]:
    environment = lark_cli_environment()
    for attempt in range(6):
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            check=False,
        )
        try:
            result = json.loads(completed.stdout or completed.stderr)
        except json.JSONDecodeError as error:
            raw = completed.stdout or completed.stderr
            if attempt < 5 and is_retryable_lark_failure(code=None, message=None, raw=raw):
                time.sleep(min(3 * (attempt + 1), 15))
                continue
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip()) from error
        error_data = result.get("error") if isinstance(result, dict) else None
        code = error_data.get("code") if isinstance(error_data, dict) else None
        message = error_data.get("message") if isinstance(error_data, dict) else None
        failed = completed.returncode != 0 or result.get("ok") is not True
        if failed and attempt < 5 and is_retryable_lark_failure(
            code=code,
            message=message,
            raw=completed.stdout or completed.stderr,
        ):
            time.sleep(min(3 * (attempt + 1), 15))
            continue
        if failed:
            raise RuntimeError(json.dumps(result, ensure_ascii=False))
        return result
    raise RuntimeError("lark-cli retry limit exceeded")


def _chart_rows(result: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    data = result.get("data", {})
    rows = data.get("main_data")
    measures = data.get("measures")
    dimensions = data.get("dimensions") or []
    if not isinstance(rows, list) or not isinstance(measures, list) or len(measures) != 1:
        raise AssertionError("unexpected chart response")
    return rows, measures, dimensions


def _values(result: dict[str, Any]) -> tuple[str, list[dict[str, Any]], list[Any]]:
    rows, measures, dimensions = _chart_rows(result)
    measure = measures[0]
    alias = measure["alias"]
    values = [row[alias]["value"] for row in rows]
    return measure["field_name"], dimensions, values


def _dimension_value(row: dict[str, Any], dimensions: list[dict[str, Any]], field: str) -> Any:
    alias = next(item["alias"] for item in dimensions if item["field_name"] == field)
    return row[alias]["value"]


def _series(result: dict[str, Any], dimensions_wanted: tuple[str, ...]) -> dict[Any, Any]:
    rows, measures, dimensions = _chart_rows(result)
    measure_alias = measures[0]["alias"]
    output: dict[Any, Any] = {}
    for row in rows:
        key_values = tuple(_dimension_value(row, dimensions, field) for field in dimensions_wanted)
        key: Any = key_values[0] if len(key_values) == 1 else key_values
        output[key] = row[measure_alias]["value"]
    return output


def _load_rows(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError(f"expected a JSON row array: {path}")
    return value


def _week_start(row: dict[str, Any]) -> str:
    return str(row["周期开始"]).split(" ", 1)[0]


def _is_dashboard_week(row: dict[str, Any]) -> bool:
    windows = row.get("看板窗口") or []
    return row.get("统计粒度") == "周" and isinstance(windows, list) and "近4周" in windows


def _assert_expected_subset(
    actual: dict[Any, Any],
    expected: dict[Any, Any],
    label: str,
) -> None:
    mismatches = {
        key: {"expected": value, "actual": actual.get(key)}
        for key, value in expected.items()
        if actual.get(key) != value
    }
    if mismatches:
        raise AssertionError((label, mismatches))


def main() -> int:
    args = _arguments()
    overview_rows = _load_rows(args.overview_payload)
    product_rows = _load_rows(args.product_payload)
    current_overview = [
        row
        for row in overview_rows
        if row.get("统计粒度") == "周" and row.get("当前周期") is True
    ]
    if len(current_overview) != 1:
        raise AssertionError("overview payload must contain exactly one current week")
    current_week = current_overview[0]
    current_products = {
        str(row["产品大类"]): row
        for row in product_rows
        if row.get("统计粒度") == "周" and row.get("当前周期") is True
    }
    if len(current_products) != 3:
        raise AssertionError("product payload must contain three current-week categories")
    runtime = _runtime()
    common = ["--as", "user", "--base-token", args.base_token, "--format", "json"]
    results = {
        name: _run([*runtime, "base", "+dashboard-block-get-data", *common, "--block-id", block_id])
        for name, block_id in BLOCKS.items()
    }

    expected_stats = {
        "overview_visit_stat": ("官网访问次数", current_week["官网访问次数"]),
        "overview_search_stat": ("Google自然搜索点击", current_week["Google自然搜索点击"]),
        "overview_event_stat": ("表单提交事件（GA4）", current_week["表单提交事件（GA4）"]),
        "overview_inquiry_stat": ("官网入库询盘", current_week["官网入库询盘"]),
    }
    for name, (field, expected) in expected_stats.items():
        actual_field, _, values = _values(results[name])
        assert actual_field == field and values == [expected], (name, actual_field, values)

    overview_weeks = [row for row in overview_rows if _is_dashboard_week(row)]
    for block_name, field_name in {
        "overview_visit_line": "官网访问次数",
        "overview_search_line": "Google自然搜索点击",
        "overview_inquiry_line": "官网入库询盘",
    }.items():
        expected = {
            _week_start(row): row[field_name]
            for row in overview_weeks
            if row.get(field_name) is not None
        }
        _assert_expected_subset(
            _series(results[block_name], ("周期开始",)),
            expected,
            block_name,
        )

    product_visits = _series(results["product_visit_line"], ("周期开始", "产品大类"))
    product_search = _series(results["product_search_line"], ("周期开始", "产品大类"))
    product_inquiry = _series(results["product_inquiry_line"], ("周期开始", "产品大类"))
    product_weeks = [row for row in product_rows if _is_dashboard_week(row)]
    for actual, field_name in (
        (product_visits, "官网访问次数"),
        (product_search, "Google自然搜索点击"),
        (product_inquiry, "官网入库询盘"),
    ):
        expected = {
            (_week_start(row), row["产品大类"]): row[field_name]
            for row in product_weeks
            if row.get(field_name) is not None
        }
        _assert_expected_subset(actual, expected, field_name)

    for name, field_name in {
        "product_visit_bar": "官网访问次数",
        "product_search_bar": "Google自然搜索点击",
        "product_inquiry_bar": "官网入库询盘",
    }.items():
        expected = {
            category: row[field_name]
            for category, row in current_products.items()
            if row.get(field_name) is not None
        }
        assert _series(results[name], ("产品大类",)) == expected, name

    block_counts: dict[str, int] = {}
    for name, (dashboard_id, expected_count) in DASHBOARDS.items():
        response = _run(
            [*runtime, "base", "+dashboard-block-list", *common, "--dashboard-id", dashboard_id]
        )
        data = response.get("data", {})
        blocks = data.get("blocks") or data.get("items") or data.get("data")
        if not isinstance(blocks, list):
            raise AssertionError(f"unexpected block list for {name}")
        assert len(blocks) == expected_count, (name, len(blocks), expected_count)
        block_counts[name] = len(blocks)

        actual_text = {
            block["block_id"]: block
            for block in blocks
            if block.get("type") == "text"
        }
        for block_id, expected_name in TEXT_BLOCKS[name].items():
            block = actual_text[block_id]
            assert block.get("name") == expected_name
            detail_response = _run(
                [
                    *runtime,
                    "base",
                    "+dashboard-block-get",
                    *common,
                    "--dashboard-id",
                    dashboard_id,
                    "--block-id",
                    block_id,
                ]
            )
            detail = detail_response.get("data", {}).get("block", {})
            assert detail.get("name") == expected_name
            text_config = detail.get("data_config", {}).get("text")
            assert isinstance(text_config, (dict, str)), (block_id, type(text_config).__name__)
            if isinstance(text_config, str):
                assert text_config.strip(), block_id
                assert "\\n" not in text_config and '\\"' not in text_config, block_id

    print(
        json.dumps(
            {
                "status": "ok",
                "verified_chart_blocks": len(results),
                "dashboard_block_counts": block_counts,
                "current_week": {
                    "website_sessions": current_week["官网访问次数"],
                    "google_search_clicks": current_week["Google自然搜索点击"],
                    "ga4_form_events": current_week["表单提交事件（GA4）"],
                    "website_inquiries": current_week["官网入库询盘"],
                },
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
